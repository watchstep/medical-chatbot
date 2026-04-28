from __future__ import annotations

import io
import json
import logging
import re
from dataclasses import asdict, dataclass

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload, MediaIoBaseDownload

from app.config import Settings
from app.schemas import (
    DocumentRegistry,
    DocumentRegistryEntry,
    DriveFile,
    PatientFileMeta,
    PatientIndex,
    PatientIndexEntry,
    PatientMeta,
    PatientRecordContext,
)


FOLDER_MIME = "application/vnd.google-apps.folder"
PDF_FILE_RE = re.compile(r"^(result|chart|image)_(\d{8})\.pdf$")
PATIENT_FOLDER_RE = re.compile(r"^(?P<patient_id>[^_]+)_(?P<name>.+)_(?P<birth>\d{8})$")
logger = logging.getLogger(__name__)


class DriveLookupError(Exception):
    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message)
        self.detail = detail


@dataclass(frozen=True)
class PatientFolderInventoryItem:
    patient_id: str
    name: str
    birth: str
    folder_name: str
    folder_id: str


@dataclass(frozen=True)
class SkippedPatientFolder:
    folder_name: str
    folder_id: str
    reason: str


@dataclass(frozen=True)
class PatientIndexSyncResult:
    patient_index: PatientIndex
    added: list[PatientIndexEntry]
    updated: list[PatientIndexEntry]
    unchanged: list[PatientIndexEntry]
    skipped: list[SkippedPatientFolder]
    missing_in_drive: list[PatientIndexEntry]

    def to_dict(self) -> dict[str, object]:
        return {
            "patient_index": self.patient_index.model_dump(exclude_none=True),
            "added": [item.model_dump(exclude_none=True) for item in self.added],
            "updated": [item.model_dump(exclude_none=True) for item in self.updated],
            "unchanged": [item.model_dump(exclude_none=True) for item in self.unchanged],
            "skipped": [asdict(item) for item in self.skipped],
            "missing_in_drive": [
                item.model_dump(exclude_none=True) for item in self.missing_in_drive
            ],
        }


class DriveGateway:
    def list_files(self, query: str) -> list[dict]:
        raise NotImplementedError

    def download_file_bytes(self, file_id: str) -> bytes:
        raise NotImplementedError

    def get_file(self, file_id: str) -> dict:
        raise NotImplementedError

    def update_file_bytes(self, file_id: str, content: bytes, mime_type: str) -> None:
        raise NotImplementedError

    def create_file_bytes(
        self,
        parent_id: str,
        file_name: str,
        content: bytes,
        mime_type: str,
    ) -> str:
        raise NotImplementedError


class GoogleDriveGateway(DriveGateway):
    def __init__(self, credentials_path: str):
        scopes = ["https://www.googleapis.com/auth/drive"]
        credentials = Credentials.from_service_account_file(
            credentials_path,
            scopes=scopes,
        )
        self.service = build("drive", "v3", credentials=credentials, cache_discovery=False)

    def list_files(self, query: str) -> list[dict]:
        try:
            response = (
                self.service.files()
                .list(
                    q=query,
                    fields="files(id,name,mimeType,modifiedTime,parents)",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            logger.exception("Google Drive list_files failed")
            raise DriveLookupError(
                "Google Drive 파일 목록 조회에 실패했습니다.",
                detail=str(exc),
            ) from exc
        return response.get("files", [])

    def download_file_bytes(self, file_id: str) -> bytes:
        try:
            request = self.service.files().get_media(fileId=file_id)
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        except HttpError as exc:
            logger.exception("Google Drive download_file_bytes failed")
            raise DriveLookupError(
                "Google Drive 파일 다운로드에 실패했습니다.",
                detail=str(exc),
            ) from exc
        return buffer.getvalue()

    def get_file(self, file_id: str) -> dict:
        try:
            return (
                self.service.files()
                .get(
                    fileId=file_id,
                    fields="id,name,mimeType,parents",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            logger.exception("Google Drive get_file failed")
            raise DriveLookupError(
                "Google Drive 파일 조회에 실패했습니다.",
                detail=str(exc),
            ) from exc

    def update_file_bytes(self, file_id: str, content: bytes, mime_type: str) -> None:
        try:
            media = MediaInMemoryUpload(content, mimetype=mime_type, resumable=False)
            (
                self.service.files()
                .update(
                    fileId=file_id,
                    media_body=media,
                    supportsAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            logger.exception("Google Drive update_file_bytes failed")
            raise DriveLookupError(
                "Google Drive 파일 저장에 실패했습니다.",
                detail=str(exc),
            ) from exc

    def create_file_bytes(
        self,
        parent_id: str,
        file_name: str,
        content: bytes,
        mime_type: str,
    ) -> str:
        try:
            media = MediaInMemoryUpload(content, mimetype=mime_type, resumable=False)
            created = (
                self.service.files()
                .create(
                    body={
                        "name": file_name,
                        "parents": [parent_id],
                    },
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            logger.exception("Google Drive create_file_bytes failed")
            raise DriveLookupError(
                "Google Drive 파일 생성에 실패했습니다.",
                detail=str(exc),
            ) from exc
        return created["id"]


@dataclass
class DriveLookupService:
    settings: Settings
    gateway: DriveGateway

    def get_patient_by_kakao_user_id(self, kakao_user_id: str) -> PatientIndexEntry | None:
        patient_index, _ = self.load_patient_index_with_file_id()
        return next(
            (item for item in patient_index.patients if kakao_user_id in item.kakao_user_ids),
            None,
        )

    def authenticate_and_map_patient(
        self,
        kakao_user_id: str,
        name: str,
        birth: str,
    ) -> PatientIndexEntry:
        patient_index, patient_index_file_id = self.load_patient_index_with_file_id()
        existing_patient = self._find_patient_by_kakao_user_id(patient_index, kakao_user_id)
        if existing_patient is not None:
            if existing_patient.name != name or existing_patient.birth != birth:
                raise DriveLookupError("이미 다른 환자에 연결된 사용자입니다.")
            return existing_patient

        patient = self._find_patient_by_identity(patient_index, name, birth)
        if patient is None:
            raise DriveLookupError(
                "등록된 환자 정보를 찾지 못했습니다. 병원에 등록 여부를 확인해 주세요."
            )

        updated_index = self._attach_kakao_user_id(
            patient_index=patient_index,
            target_patient_id=patient.patient_id,
            kakao_user_id=kakao_user_id,
        )
        self._save_patient_index(patient_index_file_id, updated_index)
        return self._find_patient_by_identity(updated_index, name, birth) or patient

    def debug_probe(
        self,
        kakao_user_id: str | None = None,
        name: str | None = None,
        birth: str | None = None,
    ) -> dict:
        result: dict[str, object] = {
            "root_folder_name": self.settings.drive_root_folder_name,
            "credentials_path": str(self.settings.google_service_account_path),
        }

        try:
            root_id = self._find_root_folder_id()
            result["root_folder_id"] = root_id

            system_folder_id = self._find_child_folder_id(
                parent_id=root_id,
                folder_name=self.settings.drive_system_folder_name,
            )
            result["system_folder_id"] = system_folder_id

            patient_index = self.load_patient_index()
            result["patient_count"] = len(patient_index.patients)
            result["patient_folder_names"] = [
                patient.folder_name or patient.folder_id for patient in patient_index.patients
            ]

            patient: PatientIndexEntry | None = None
            if kakao_user_id:
                patient = self.get_patient_by_kakao_user_id(kakao_user_id)
                result["mapping_found"] = patient is not None

            if patient is None and name and birth:
                patient = self._find_patient_by_identity(patient_index, name, birth)
                result["identity_found"] = patient is not None

            if patient is not None:
                if kakao_user_id:
                    result["authenticated_by"] = "kakao_user_id" if kakao_user_id in patient.kakao_user_ids else "identity"
                context = self.get_patient_record_context(
                    patient=patient,
                )
                result["authenticated_patient_id"] = patient.patient_id
                result["authenticated_folder_name"] = patient.folder_name
                result["authenticated_folder_id"] = patient.folder_id
                result["latest_visit_date"] = context.meta.latest_visit_date
                result["latest_result"] = (
                    context.latest_result.name if context.latest_result else None
                )
                result["latest_chart"] = (
                    context.latest_chart.name if context.latest_chart else None
                )
            elif kakao_user_id and name and birth:
                patient = self.authenticate_and_map_patient(
                    kakao_user_id=kakao_user_id,
                    name=name,
                    birth=birth,
                )
                result["mapping_found"] = False
                result["mapping_saved"] = True
                result["authenticated_by"] = "identity"
                context = self.get_patient_record_context(
                    patient=patient,
                )
                result["authenticated_patient_id"] = patient.patient_id
                result["authenticated_folder_name"] = patient.folder_name
                result["authenticated_folder_id"] = patient.folder_id
                result["latest_visit_date"] = context.meta.latest_visit_date
                result["latest_result"] = (
                    context.latest_result.name if context.latest_result else None
                )
                result["latest_chart"] = (
                    context.latest_chart.name if context.latest_chart else None
                )
        except DriveLookupError as exc:
            result["ok"] = False
            result["error"] = str(exc)
            if exc.detail:
                result["detail"] = exc.detail
            return result

        result["ok"] = True
        return result

    def load_patient_index(self) -> PatientIndex:
        patient_index, _ = self.load_patient_index_with_file_id()
        return patient_index

    def load_document_registry(self) -> DocumentRegistry:
        document_registry, _ = self.load_document_registry_with_file_id()
        return document_registry

    def persist_patient_index(
        self,
        *,
        patient_index_file_id: str,
        patient_index: PatientIndex,
    ) -> None:
        self._save_patient_index(patient_index_file_id, patient_index)

    def persist_document_registry(
        self,
        *,
        document_registry_file_id: str | None,
        document_registry: DocumentRegistry,
    ) -> str:
        return self._save_document_registry(document_registry_file_id, document_registry)

    def sync_patient_index(self) -> PatientIndexSyncResult:
        patient_index, _ = self.load_patient_index_with_file_id()
        inventory, skipped = self.list_patient_folders()

        inventory_by_patient_id = {
            item.patient_id: item for item in inventory
        }
        added: list[PatientIndexEntry] = []
        updated: list[PatientIndexEntry] = []
        unchanged: list[PatientIndexEntry] = []
        reconciled_patients: list[PatientIndexEntry] = []

        for patient in patient_index.patients:
            inventory_item = inventory_by_patient_id.pop(patient.patient_id, None)
            if inventory_item is None:
                reconciled_patients.append(patient)
                unchanged.append(patient)
                continue

            reconciled_patient = patient.model_copy(
                update={
                    "name": inventory_item.name,
                    "birth": inventory_item.birth,
                    "folder_name": inventory_item.folder_name,
                    "folder_id": inventory_item.folder_id,
                }
            )
            reconciled_patients.append(reconciled_patient)
            if reconciled_patient == patient:
                unchanged.append(reconciled_patient)
            else:
                updated.append(reconciled_patient)

        for inventory_item in sorted(
            inventory_by_patient_id.values(),
            key=lambda item: (item.patient_id, item.folder_name),
        ):
            new_patient = PatientIndexEntry(
                patient_id=inventory_item.patient_id,
                name=inventory_item.name,
                birth=inventory_item.birth,
                folder_name=inventory_item.folder_name,
                folder_id=inventory_item.folder_id,
                kakao_user_ids=[],
                phone_last4="",
            )
            reconciled_patients.append(new_patient)
            added.append(new_patient)

        missing_in_drive = [
            patient
            for patient in patient_index.patients
            if patient.patient_id not in {item.patient_id for item in inventory}
        ]

        return PatientIndexSyncResult(
            patient_index=PatientIndex(patients=reconciled_patients),
            added=added,
            updated=updated,
            unchanged=unchanged,
            skipped=skipped,
            missing_in_drive=missing_in_drive,
        )

    def list_patient_folders(
        self,
    ) -> tuple[list[PatientFolderInventoryItem], list[SkippedPatientFolder]]:
        root_id = self._find_root_folder_id()
        patients_folder_id = self._find_child_folder_id(
            parent_id=root_id,
            folder_name="patients",
        )
        files = self.gateway.list_files(
            f"'{patients_folder_id}' in parents and "
            f"mimeType = '{FOLDER_MIME}' and trashed = false"
        )

        parsed_items: list[PatientFolderInventoryItem] = []
        skipped: list[SkippedPatientFolder] = []
        for file in files:
            folder_id = file["id"]
            folder_name = file["name"]
            match = PATIENT_FOLDER_RE.match(folder_name)
            if match is None:
                skipped.append(
                    SkippedPatientFolder(
                        folder_name=folder_name,
                        folder_id=folder_id,
                        reason="invalid_folder_name_format",
                    )
                )
                continue
            parsed_items.append(
                PatientFolderInventoryItem(
                    patient_id=match.group("patient_id"),
                    name=match.group("name"),
                    birth=match.group("birth"),
                    folder_name=folder_name,
                    folder_id=folder_id,
                )
            )

        inventory: list[PatientFolderInventoryItem] = []
        patient_id_counts: dict[str, int] = {}
        for item in parsed_items:
            patient_id_counts[item.patient_id] = patient_id_counts.get(item.patient_id, 0) + 1
        for item in parsed_items:
            if patient_id_counts[item.patient_id] > 1:
                skipped.append(
                    SkippedPatientFolder(
                        folder_name=item.folder_name,
                        folder_id=item.folder_id,
                        reason="duplicate_patient_id",
                    )
                )
                continue
            inventory.append(item)

        inventory.sort(key=lambda item: (item.patient_id, item.folder_name))
        skipped.sort(key=lambda item: (item.folder_name, item.folder_id))
        return inventory, skipped

    def load_patient_index_with_file_id(self) -> tuple[PatientIndex, str]:
        try:
            root_id = self._find_root_folder_id()
            system_folder_id = self._find_child_folder_id(
                parent_id=root_id,
                folder_name=self.settings.drive_system_folder_name,
            )
            patient_index_file_id = self._find_file_id(
                parent_id=system_folder_id,
                file_name=self.settings.patient_index_file_name,
            )
            content = self.gateway.download_file_bytes(patient_index_file_id)
            return PatientIndex.model_validate(json.loads(content.decode("utf-8"))), patient_index_file_id
        except DriveLookupError:
            raise
        except Exception as exc:
            logger.exception("Failed to load patient index")
            raise DriveLookupError(
                "patient_index.json 조회에 실패했습니다.",
                detail=str(exc),
            ) from exc

    def load_document_registry_with_file_id(self) -> tuple[DocumentRegistry, str | None]:
        try:
            root_id = self._find_root_folder_id()
            system_folder_id = self._find_child_folder_id(
                parent_id=root_id,
                folder_name=self.settings.drive_system_folder_name,
            )
            document_registry_file_id = self._find_optional_file_id(
                parent_id=system_folder_id,
                file_name=self.settings.document_registry_file_name,
            )
            if document_registry_file_id is None:
                return DocumentRegistry(documents=[]), None
            content = self.gateway.download_file_bytes(document_registry_file_id)
            return (
                DocumentRegistry.model_validate(json.loads(content.decode("utf-8"))),
                document_registry_file_id,
            )
        except DriveLookupError:
            raise
        except Exception as exc:
            logger.exception("Failed to load document registry")
            raise DriveLookupError(
                "document_registry.json 조회에 실패했습니다.",
                detail=str(exc),
            ) from exc

    def get_patient_record_context(
        self,
        patient: PatientIndexEntry,
    ) -> PatientRecordContext:
        try:
            patient_folder_id = self._get_patient_folder_id(patient)
            files = self.gateway.list_files(
                f"'{patient_folder_id}' in parents and trashed = false"
            )
            document_files = self._extract_document_files(files)
            latest_result, latest_chart = self._select_latest_documents(files)
            meta = self._load_or_build_meta(
                patient=patient,
                patient_folder_id=patient_folder_id,
                document_files=document_files,
            )
            return PatientRecordContext(
                patient=patient,
                meta=meta,
                latest_result=latest_result,
                latest_chart=latest_chart,
            )
        except DriveLookupError:
            raise
        except Exception as exc:
            logger.exception("Failed to load patient record context")
            raise DriveLookupError(
                "환자 기록 조회에 실패했습니다.",
                detail=str(exc),
            ) from exc

    def download_drive_file_bytes(self, drive_file: DriveFile) -> bytes:
        try:
            return self.gateway.download_file_bytes(drive_file.file_id)
        except DriveLookupError:
            raise
        except Exception as exc:
            logger.exception("Failed to download drive file bytes")
            raise DriveLookupError(
                "환자 문서 다운로드에 실패했습니다.",
                detail=str(exc),
            ) from exc

    def list_patient_document_files(self, patient: PatientIndexEntry) -> list[dict]:
        try:
            patient_folder_id = self._get_patient_folder_id(patient)
            return self.gateway.list_files(
                f"'{patient_folder_id}' in parents and trashed = false"
            )
        except DriveLookupError:
            raise
        except Exception as exc:
            logger.exception("Failed to list patient document files")
            raise DriveLookupError(
                "환자 문서 목록 조회에 실패했습니다.",
                detail=str(exc),
            ) from exc

    def _load_or_build_meta(
        self,
        patient: PatientIndexEntry,
        patient_folder_id: str,
        document_files: list[PatientFileMeta],
    ) -> PatientMeta:
        if document_files:
            return self._build_patient_meta(patient, document_files)

        meta_file_id = self._find_optional_file_id(
            parent_id=patient_folder_id,
            file_name=self.settings.meta_file_name,
        )
        if meta_file_id is None:
            raise DriveLookupError("환자 폴더에 조회 가능한 PDF가 없습니다.")

        meta_content = self.gateway.download_file_bytes(meta_file_id)
        return PatientMeta.model_validate(json.loads(meta_content.decode("utf-8")))

    def _save_patient_index(self, patient_index_file_id: str, patient_index: PatientIndex) -> None:
        try:
            content = json.dumps(
                patient_index.model_dump(exclude_none=True),
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            self.gateway.update_file_bytes(
                patient_index_file_id,
                content,
                "application/json",
            )
        except DriveLookupError:
            raise
        except Exception as exc:
            logger.exception("Failed to save patient index")
            raise DriveLookupError(
                "patient_index.json 저장에 실패했습니다.",
                detail=str(exc),
            ) from exc

    def _save_document_registry(
        self,
        document_registry_file_id: str | None,
        document_registry: DocumentRegistry,
    ) -> str:
        try:
            content = json.dumps(
                document_registry.model_dump(exclude_none=True),
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            if document_registry_file_id is not None:
                self.gateway.update_file_bytes(
                    document_registry_file_id,
                    content,
                    "application/json",
                )
                return document_registry_file_id

            root_id = self._find_root_folder_id()
            system_folder_id = self._find_child_folder_id(
                parent_id=root_id,
                folder_name=self.settings.drive_system_folder_name,
            )
            return self.gateway.create_file_bytes(
                system_folder_id,
                self.settings.document_registry_file_name,
                content,
                "application/json",
            )
        except DriveLookupError:
            raise
        except Exception as exc:
            logger.exception("Failed to save document registry")
            raise DriveLookupError(
                "document_registry.json 저장에 실패했습니다.",
                detail=str(exc),
            ) from exc

    def _find_patient_by_kakao_user_id(
        self,
        patient_index: PatientIndex,
        kakao_user_id: str,
    ) -> PatientIndexEntry | None:
        return next(
            (item for item in patient_index.patients if kakao_user_id in item.kakao_user_ids),
            None,
        )

    def _find_patient_by_identity(
        self,
        patient_index: PatientIndex,
        name: str,
        birth: str,
    ) -> PatientIndexEntry | None:
        return next(
            (
                item
                for item in patient_index.patients
                if item.name == name and item.birth == birth
            ),
            None,
        )

    def _attach_kakao_user_id(
        self,
        patient_index: PatientIndex,
        target_patient_id: str,
        kakao_user_id: str,
    ) -> PatientIndex:
        owner = self._find_patient_by_kakao_user_id(patient_index, kakao_user_id)
        if owner is not None and owner.patient_id != target_patient_id:
            raise DriveLookupError("이미 다른 환자에 연결된 사용자입니다.")

        updated_patients: list[PatientIndexEntry] = []
        for patient in patient_index.patients:
            if patient.patient_id == target_patient_id:
                kakao_user_ids = list(patient.kakao_user_ids)
                if kakao_user_id not in kakao_user_ids:
                    kakao_user_ids.append(kakao_user_id)
                updated_patients.append(
                    patient.model_copy(update={"kakao_user_ids": kakao_user_ids})
                )
            else:
                updated_patients.append(patient)

        return PatientIndex(patients=updated_patients)

    def _resolve_patient_folder_id(
        self,
        parent_id: str,
        folder_name: str,
        folder_id: str | None,
    ) -> str:
        if not folder_name:
            raise DriveLookupError("patient_index.json에 환자 폴더 정보가 없습니다.")

        try:
            return self._find_child_folder_id(
                parent_id=parent_id,
                folder_name=folder_name,
            )
        except DriveLookupError:
            if not folder_id:
                raise

        file_info = self.gateway.get_file(folder_id)
        if file_info.get("mimeType") != FOLDER_MIME:
            raise DriveLookupError("환자 폴더 정보가 올바르지 않습니다.")
        parents = file_info.get("parents", [])
        if parent_id in parents:
            return folder_id
        raise DriveLookupError("환자 폴더가 patients 하위에 없습니다.")

    def _get_patient_folder_id(self, patient: PatientIndexEntry) -> str:
        root_id = self._find_root_folder_id()
        patients_folder_id = self._find_child_folder_id(
            parent_id=root_id,
            folder_name="patients",
        )
        return self._resolve_patient_folder_id(
            parent_id=patients_folder_id,
            folder_name=patient.folder_name,
            folder_id=patient.folder_id,
        )

    def _find_root_folder_id(self) -> str:
        files = self.gateway.list_files(
            f"name = '{self.settings.drive_root_folder_name}' "
            f"and mimeType = '{FOLDER_MIME}' and trashed = false"
        )
        if not files:
            raise DriveLookupError("Google Drive 루트 폴더를 찾지 못했습니다.")
        return files[0]["id"]

    def _find_child_folder_id(self, parent_id: str, folder_name: str) -> str:
        files = self.gateway.list_files(
            f"'{parent_id}' in parents and name = '{folder_name}' "
            f"and mimeType = '{FOLDER_MIME}' and trashed = false"
        )
        if not files:
            raise DriveLookupError(f"Google Drive 폴더를 찾지 못했습니다: {folder_name}")
        return files[0]["id"]

    def _find_file_id(self, parent_id: str, file_name: str) -> str:
        files = self.gateway.list_files(
            f"'{parent_id}' in parents and name = '{file_name}' and trashed = false"
        )
        if not files:
            raise DriveLookupError(f"Google Drive 파일을 찾지 못했습니다: {file_name}")
        return files[0]["id"]

    def _find_optional_file_id(self, parent_id: str, file_name: str) -> str | None:
        files = self.gateway.list_files(
            f"'{parent_id}' in parents and name = '{file_name}' and trashed = false"
        )
        if not files:
            return None
        return files[0]["id"]

    def _select_latest_documents(
        self,
        files: list[dict],
    ) -> tuple[DriveFile | None, DriveFile | None]:
        latest_by_type: dict[str, DriveFile] = {}

        for file in files:
            if file.get("mimeType") == FOLDER_MIME:
                continue

            match = PDF_FILE_RE.match(file.get("name", ""))
            if match is None:
                continue

            document_type, date = match.groups()
            drive_file = DriveFile(
                file_id=file["id"],
                name=file["name"],
                mime_type=file["mimeType"],
                date=date,
                type=document_type,
            )

            current = latest_by_type.get(document_type)
            if current is None or (current.date or "") < date:
                latest_by_type[document_type] = drive_file

        return latest_by_type.get("result"), latest_by_type.get("chart")

    def _extract_document_files(self, files: list[dict]) -> list[PatientFileMeta]:
        document_files: list[PatientFileMeta] = []
        for file in files:
            if file.get("mimeType") == FOLDER_MIME:
                continue
            match = PDF_FILE_RE.match(file.get("name", ""))
            if match is None:
                continue
            document_type, date = match.groups()
            description = self._file_description(document_type)
            document_files.append(
                PatientFileMeta(
                    type=document_type,
                    date=date,
                    filename=file["name"],
                    description=description,
                    file_id=file.get("id"),
                )
            )
        document_files.sort(key=lambda item: (item.date, item.filename), reverse=True)
        return document_files

    def _build_patient_meta(
        self,
        patient: PatientIndexEntry,
        document_files: list[PatientFileMeta],
    ) -> PatientMeta:
        latest_visit_date = max(file.date for file in document_files)
        latest_summary = self._build_latest_summary(
            latest_visit_date=latest_visit_date,
            document_files=document_files,
        )
        return PatientMeta(
            patient_id=patient.patient_id,
            name=patient.name,
            birth=patient.birth,
            latest_visit_date=latest_visit_date,
            files=document_files,
            latest_summary=latest_summary,
        )

    def _build_latest_summary(
        self,
        latest_visit_date: str,
        document_files: list[PatientFileMeta],
    ) -> dict[str, str]:
        document_types = {file.type for file in document_files}
        if {"result", "chart"}.issubset(document_types):
            description = "최근 검사결과지와 진료기록부가 등록되어 있습니다."
        elif "result" in document_types:
            description = "최근 검사결과지가 등록되어 있습니다."
        elif "chart" in document_types:
            description = "최근 진료기록부가 등록되어 있습니다."
        else:
            description = "최근 진료 기록이 등록되어 있습니다."

        return {
            "date": latest_visit_date,
            "title": (
                f"{latest_visit_date[:4]}년 {int(latest_visit_date[4:6])}월 "
                f"{int(latest_visit_date[6:8])}일 진료 및 검사 기록"
            ),
            "description": description,
        }

    def _file_description(self, document_type: str) -> str:
        if document_type == "result":
            return "검사결과지"
        if document_type == "chart":
            return "진료기록부"
        if document_type == "image":
            return "영상 판독 문서"
        return "의료 문서"


def build_default_drive_service(settings: Settings) -> DriveLookupService:
    return DriveLookupService(
        settings=settings,
        gateway=GoogleDriveGateway(str(settings.google_service_account_path)),
    )
