from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.schemas import DocumentRegistry, DocumentRegistryEntry, DriveFile, PatientIndexEntry
from app.services.drive import DriveLookupService
from app.services.gemini_qa import (
    GeminiDocument,
    GeminiPatientStoreSyncService,
    GeminiQaError,
    is_file_search_document_name,
    is_file_search_store_name,
)


logger = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))
PDF_FILE_RE = re.compile(r"^(result|chart|image)_(\d{8})\.pdf$")


@dataclass(frozen=True)
class DocumentRegistrySyncResult:
    document_registry: DocumentRegistry
    document_registry_file_id: str | None
    ready_count: int
    failed_count: int
    skipped_count: int
    imported_count: int
    deleted_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "document_registry": self.document_registry.model_dump(exclude_none=True),
            "document_registry_file_id": self.document_registry_file_id,
            "ready_count": self.ready_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "imported_count": self.imported_count,
            "deleted_count": self.deleted_count,
        }


@dataclass
class DocumentRegistrySyncService:
    settings: Settings
    drive_service: DriveLookupService
    gemini_store_sync_service: GeminiPatientStoreSyncService

    def sync_document_registry(self) -> DocumentRegistrySyncResult:
        patient_index = self.drive_service.load_patient_index()
        existing_registry, document_registry_file_id = (
            self.drive_service.load_document_registry_with_file_id()
        )
        existing_by_patient_id: dict[str, list[DocumentRegistryEntry]] = {}
        for item in existing_registry.documents:
            existing_by_patient_id.setdefault(item.patient_id, []).append(item)

        generated_at = datetime.now(KST).isoformat()
        next_entries: list[DocumentRegistryEntry] = []
        imported_count = 0
        deleted_count = 0

        for patient in patient_index.patients:
            patient_entries, patient_imported_count, patient_deleted_count = (
                self._sync_patient_entries(
                    patient=patient,
                    existing_entries=existing_by_patient_id.get(patient.patient_id, []),
                    generated_at=generated_at,
                )
            )
            next_entries.extend(patient_entries)
            imported_count += patient_imported_count
            deleted_count += patient_deleted_count

        next_entries.sort(
            key=lambda item: (
                item.patient_id,
                item.document_date,
                item.filename,
                item.sync_status,
            )
        )
        document_registry = DocumentRegistry(
            generated_at=generated_at,
            documents=next_entries,
        )
        return DocumentRegistrySyncResult(
            document_registry=document_registry,
            document_registry_file_id=document_registry_file_id,
            ready_count=sum(1 for item in next_entries if item.sync_status == "READY"),
            failed_count=sum(1 for item in next_entries if item.sync_status == "FAILED"),
            skipped_count=sum(1 for item in next_entries if item.sync_status == "SKIPPED"),
            imported_count=imported_count,
            deleted_count=deleted_count,
        )

    def _sync_patient_entries(
        self,
        *,
        patient: PatientIndexEntry,
        existing_entries: list[DocumentRegistryEntry],
        generated_at: str,
    ) -> tuple[list[DocumentRegistryEntry], int, int]:
        files = self.drive_service.list_patient_document_files(patient)
        existing_by_filename = {item.filename: item for item in existing_entries}
        existing_store_name = next(
            (
                item.file_search_store_name
                for item in existing_entries
                if is_file_search_store_name(item.file_search_store_name)
            ),
            "",
        )

        candidate_entries: list[DocumentRegistryEntry] = []
        skipped_entries: list[DocumentRegistryEntry] = []
        file_contents_by_filename: dict[str, bytes] = {}
        for file in files:
            if file.get("mimeType") == "application/vnd.google-apps.folder":
                continue

            filename = file.get("name", "")
            if not filename.lower().endswith(".pdf"):
                continue

            match = PDF_FILE_RE.match(filename)
            if match is None:
                skipped_entries.append(
                    DocumentRegistryEntry(
                        patient_id=patient.patient_id,
                        filename=filename,
                        drive_file_id=file.get("id", ""),
                        drive_modified_time=file.get("modifiedTime", ""),
                        sync_status="SKIPPED",
                        synced_at=generated_at,
                    )
                )
                continue

            document_type, document_date = match.groups()
            drive_file = DriveFile(
                file_id=file["id"],
                name=filename,
                mime_type=file.get("mimeType", "application/pdf"),
                date=document_date,
                type=document_type,
            )
            content = self.drive_service.download_drive_file_bytes(drive_file)
            file_contents_by_filename[filename] = content
            candidate_entries.append(
                DocumentRegistryEntry(
                    patient_id=patient.patient_id,
                    filename=filename,
                    document_type=document_type,
                    document_date=document_date,
                    drive_file_id=file["id"],
                    drive_modified_time=file.get("modifiedTime", ""),
                    file_hash=hashlib.sha256(content).hexdigest(),
                    sync_status="PENDING",
                    synced_at=generated_at,
                )
            )

        imported_count = 0
        deleted_count = 0
        if not candidate_entries:
            deleted_count += self._delete_removed_store_documents(
                existing_entries=existing_entries,
                candidate_filenames=set(),
            )
            return skipped_entries, imported_count, deleted_count

        file_search_store_name = self.gemini_store_sync_service.ensure_file_search_store(
            patient_id=patient.patient_id,
            existing_store_name=existing_store_name,
        )
        store_documents = self.gemini_store_sync_service.list_documents(
            file_search_store_name=file_search_store_name,
        )
        store_documents_by_drive_file_id = {
            item.custom_metadata.get("drive_file_id", ""): item
            for item in store_documents
            if item.custom_metadata.get("drive_file_id")
        }
        store_documents_by_display_name = {
            item.display_name: item
            for item in store_documents
            if item.display_name
        }

        ready_entries: list[DocumentRegistryEntry] = []
        for entry in sorted(candidate_entries, key=lambda item: (item.document_date, item.filename)):
            existing_entry = existing_by_filename.get(entry.filename)
            existing_document_name = ""
            store_document = store_documents_by_drive_file_id.get(entry.drive_file_id)
            if store_document is None:
                store_document = store_documents_by_display_name.get(entry.filename)
            if store_document is not None:
                existing_document_name = store_document.name

            if self._is_unchanged(
                entry=entry,
                existing_entry=existing_entry,
                existing_document_name=existing_document_name,
                file_search_store_name=file_search_store_name,
            ):
                ready_entries.append(
                    existing_entry.model_copy(update={"synced_at": generated_at})
                )
                continue

            try:
                imported_document_name = self.gemini_store_sync_service.upsert_document(
                    file_search_store_name=file_search_store_name,
                    document=GeminiDocument(
                        name=entry.filename,
                        content=file_contents_by_filename[entry.filename],
                    ),
                    document_entry=entry,
                    existing_document_name=existing_document_name,
                )
                if not is_file_search_document_name(imported_document_name):
                    raise GeminiQaError("Gemini File Search 문서 이름이 올바르지 않습니다.")
                imported_count += 1
                ready_entries.append(
                    entry.model_copy(
                        update={
                            "file_search_store_name": file_search_store_name,
                            "file_search_document_name": imported_document_name,
                            "sync_status": "READY",
                            "synced_at": generated_at,
                        }
                    )
                )
            except GeminiQaError:
                logger.exception(
                    "document_registry sync failed patient_id=%s drive_file_id=%s existing_document_name=%s",
                    patient.patient_id,
                    entry.drive_file_id,
                    existing_document_name,
                )
                ready_entries.append(
                    entry.model_copy(
                        update={
                            "file_search_store_name": file_search_store_name,
                            "file_search_document_name": existing_document_name,
                            "sync_status": "FAILED",
                            "synced_at": generated_at,
                        }
                    )
                )

        deleted_count += self._delete_removed_store_documents(
            existing_entries=existing_entries,
            candidate_filenames={item.filename for item in candidate_entries},
        )
        return ready_entries + skipped_entries, imported_count, deleted_count

    def _is_unchanged(
        self,
        *,
        entry: DocumentRegistryEntry,
        existing_entry: DocumentRegistryEntry | None,
        existing_document_name: str,
        file_search_store_name: str,
    ) -> bool:
        if existing_entry is None:
            return False
        if existing_entry.sync_status != "READY":
            return False
        if existing_entry.file_hash != entry.file_hash:
            return False
        if existing_entry.drive_modified_time != entry.drive_modified_time:
            return False
        if existing_entry.file_search_store_name != file_search_store_name:
            return False
        if not is_file_search_document_name(existing_entry.file_search_document_name):
            return False
        return existing_entry.file_search_document_name == existing_document_name

    def _delete_removed_store_documents(
        self,
        *,
        existing_entries: list[DocumentRegistryEntry],
        candidate_filenames: set[str],
    ) -> int:
        deleted_count = 0
        for existing_entry in existing_entries:
            if existing_entry.filename in candidate_filenames:
                continue
            if not is_file_search_document_name(existing_entry.file_search_document_name):
                continue
            try:
                self.gemini_store_sync_service.delete_document(
                    document_name=existing_entry.file_search_document_name,
                )
                deleted_count += 1
            except GeminiQaError:
                logger.warning(
                    "failed to delete removed File Search document",
                    extra={
                        "patient_id": existing_entry.patient_id,
                        "document_name": existing_entry.file_search_document_name,
                    },
                )
        return deleted_count
