from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from app.config import Settings
from app.repositories import MedicalRepository
from app.schemas import DriveFolderIndexEntry, PatientProfile
from app.services.drive import DriveGateway, DriveLookupError
from datetime import datetime, timedelta, timezone

from app.services.patient_identity import (
    make_patient_birth_key,
    make_patient_name_key,
    normalize_drive_folder_name,
    normalize_patient_birth,
    normalize_patient_name,
)

GOOGLE_DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
KST = timezone(timedelta(hours=9))


def now_kst_iso() -> str:
    return datetime.now(KST).isoformat()


class PatientBootstrapError(Exception):
    pass


@dataclass(frozen=True)
class BootstrappedPatient:
    patient_id: str
    name: str
    birth: str
    drive_folder_id: str
    drive_folder_name: str
    status: str


@dataclass(frozen=True)
class PatientDriveBootstrapResult:
    ok: bool
    root_folder_id: str = ""
    patients_folder_id: str = ""
    scanned_count: int = 0
    created_count: int = 0
    updated_count: int = 0
    skipped_count: int = 0
    invalid_count: int = 0
    deactivated_count: int = 0
    message: str = ""
    patients: list[BootstrappedPatient] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "root_folder_id": self.root_folder_id,
            "patients_folder_id": self.patients_folder_id,
            "scanned_count": self.scanned_count,
            "created_count": self.created_count,
            "updated_count": self.updated_count,
            "skipped_count": self.skipped_count,
            "invalid_count": self.invalid_count,
            "deactivated_count": self.deactivated_count,
            "message": self.message,
            "patients": [patient.__dict__ for patient in self.patients],
        }


@dataclass
class PatientDriveBootstrapService:
    settings: Settings
    repository: MedicalRepository
    drive_gateway: DriveGateway

    def bootstrap(self) -> PatientDriveBootstrapResult:
        try:
            root_folder = self._resolve_root_folder()
            patients_folder = self._resolve_patients_folder(root_folder["id"])
            patient_folders = self.drive_gateway.list_child_folders(patients_folder["id"])
        except Exception as exc:
            return PatientDriveBootstrapResult(
                ok=False,
                message=f"patient Drive bootstrap failed: {exc}",
            )

        created_count = 0
        updated_count = 0
        skipped_count = 0
        invalid_count = 0
        deactivated_count = 0
        bootstrapped: list[BootstrappedPatient] = []
        active_drive_folder_ids: set[str] = set()
        now = now_kst_iso()

        for folder in patient_folders:
            if folder.get("mimeType") and folder.get("mimeType") != GOOGLE_DRIVE_FOLDER_MIME_TYPE:
                skipped_count += 1
                continue
            if folder.get("trashed"):
                folder_id = str(folder.get("id") or "").strip()
                if self._deactivate_patient_folder(folder_id, now=now, reason="patient folder trashed during bootstrap"):
                    deactivated_count += 1
                else:
                    skipped_count += 1
                continue

            folder_id = str(folder.get("id") or "").strip()
            folder_name = normalize_drive_folder_name(str(folder.get("name") or ""))
            if not folder_id or not folder_name:
                invalid_count += 1
                continue

            parsed = self._parse_patient_folder_name(folder_name)
            if parsed is None:
                invalid_count += 1
                if self._deactivate_patient_folder(
                    folder_id,
                    now=now,
                    reason="patient folder name no longer matches required pattern",
                ):
                    deactivated_count += 1
                continue

            name, birth = parsed
            patient, action = self._upsert_patient_from_folder(
                folder_id=folder_id,
                folder_name=folder_name,
                name=name,
                birth=birth,
                now=now,
            )
            active_drive_folder_ids.add(folder_id)
            if action == "created":
                created_count += 1
            elif action == "updated":
                updated_count += 1
            else:
                skipped_count += 1

            bootstrapped.append(
                BootstrappedPatient(
                    patient_id=patient.patient_id,
                    name=patient.name,
                    birth=patient.birth,
                    drive_folder_id=patient.drive_folder_id,
                    drive_folder_name=patient.drive_folder_name,
                    status=patient.status,
                )
            )

        if getattr(self.settings, "deactivate_missing_drive_patients_on_bootstrap", True):
            deactivated_count += self._deactivate_missing_patient_folders(active_drive_folder_ids, now=now)

        return PatientDriveBootstrapResult(
            ok=True,
            root_folder_id=root_folder["id"],
            patients_folder_id=patients_folder["id"],
            scanned_count=len(patient_folders),
            created_count=created_count,
            updated_count=updated_count,
            skipped_count=skipped_count,
            invalid_count=invalid_count,
            deactivated_count=deactivated_count,
            message="patients bootstrapped from Drive folders",
            patients=bootstrapped,
        )

    def _upsert_patient_from_folder(
        self,
        *,
        folder_id: str,
        folder_name: str,
        name: str,
        birth: str,
        now: str,
    ) -> tuple[PatientProfile, str]:
        name_key = make_patient_name_key(name)
        birth_key = make_patient_birth_key(birth)
        existing = self._find_existing_patient(
            drive_folder_id=folder_id,
            name=name,
            birth=birth,
        )
        patient_id = existing.patient_id if existing is not None else self._patient_id_from_drive_folder_id(folder_id)

        if existing is not None and existing.drive_folder_id != folder_id:
            old_folder_index = self.repository.get_drive_folder_index(existing.drive_folder_id)
            if old_folder_index is not None and old_folder_index.status == "active":
                self.repository.upsert_drive_folder_index(
                    old_folder_index.model_copy(update={"status": "deleted", "updated_at": now})
                )

        incoming = PatientProfile(
            patient_id=patient_id,
            name=name,
            birth=birth,
            name_key=name_key,
            birth_key=birth_key,
            drive_folder_id=folder_id,
            drive_folder_name=folder_name,
            status="active",
            created_at=existing.created_at if existing is not None and existing.created_at else now,
            updated_at=now,
        )

        if existing is None:
            action = "created"
            self.repository.upsert_patient(incoming)
        elif self._patient_needs_update(existing, incoming):
            action = "updated"
            self.repository.upsert_patient(incoming)
        else:
            action = "skipped"

        self._upsert_drive_folder_index(
            drive_folder_id=folder_id,
            patient_id=patient_id,
            status="active",
            now=now,
        )
        return incoming, action

    def _find_existing_patient(self, *, drive_folder_id: str, name: str, birth: str) -> PatientProfile | None:
        folder_index = self.repository.get_drive_folder_index(drive_folder_id)
        if folder_index is not None:
            patient = self.repository.get_patient(folder_index.patient_id)
            if patient is not None:
                return patient

        hashed_patient = self.repository.get_patient(self._patient_id_from_drive_folder_id(drive_folder_id))
        if hashed_patient is not None:
            return hashed_patient

        find_by_identity = getattr(self.repository, "find_active_patient_by_identity", None)
        if callable(find_by_identity):
            return find_by_identity(name=name, birth=birth)
        return None

    def _upsert_drive_folder_index(self, *, drive_folder_id: str, patient_id: str, status: str, now: str) -> None:
        existing = self.repository.get_drive_folder_index(drive_folder_id)
        self.repository.upsert_drive_folder_index(
            DriveFolderIndexEntry(
                drive_folder_id=drive_folder_id,
                patient_id=patient_id,
                status=status,
                created_at=existing.created_at if existing is not None and existing.created_at else now,
                updated_at=now,
            )
        )

    def _deactivate_missing_patient_folders(self, active_drive_folder_ids: set[str], *, now: str) -> int:
        count = 0
        for patient in self.repository.list_active_patients():
            drive_folder_id = str(patient.drive_folder_id or "").strip()
            if not drive_folder_id or drive_folder_id in active_drive_folder_ids:
                continue
            if self._deactivate_patient_folder(
                drive_folder_id,
                now=now,
                reason="patient folder missing during bootstrap",
            ):
                count += 1
        return count

    def _deactivate_patient_folder(self, drive_folder_id: str, *, now: str, reason: str) -> bool:
        if not drive_folder_id:
            return False
        folder_index = self.repository.get_drive_folder_index(drive_folder_id)
        if folder_index is None:
            return False
        patient = self.repository.get_patient(folder_index.patient_id)
        if patient is not None and patient.status != "inactive":
            self.repository.upsert_patient(
                patient.model_copy(update={"status": "inactive", "updated_at": now})
            )
        self.repository.upsert_drive_folder_index(
            folder_index.model_copy(update={"status": "deleted", "updated_at": now})
        )
        append_log = getattr(self.repository, "append_wiki_log", None)
        if callable(append_log):
            append_log(
                folder_index.patient_id,
                {
                    "event_type": "patient_folder_deactivated",
                    "title": "patient folder deactivated during bootstrap",
                    "page_ids": [],
                    "index_updated": False,
                    "status": "success",
                    "message": reason,
                    "created_at": now,
                },
            )
        return True

    def _resolve_root_folder(self) -> dict:
        root_folder_id = (getattr(self.settings, "google_drive_root_folder_id", "") or "").strip()
        if root_folder_id:
            folder = self.drive_gateway.get_file(root_folder_id)
            if folder.get("trashed"):
                raise PatientBootstrapError("configured Google Drive root folder is trashed")
            if folder.get("mimeType") and folder.get("mimeType") != GOOGLE_DRIVE_FOLDER_MIME_TYPE:
                raise PatientBootstrapError("configured Google Drive root ID is not a folder")
            return folder

        root_name = (getattr(self.settings, "google_drive_root_folder_name", "") or "").strip()
        if not root_name:
            raise PatientBootstrapError("GOOGLE_DRIVE_ROOT_FOLDER_ID or GOOGLE_DRIVE_ROOT_FOLDER_NAME is required")
        matches = self.drive_gateway.find_folders_by_name(root_name)
        if not matches:
            raise PatientBootstrapError(f"Google Drive root folder not found: {root_name}")
        if len(matches) > 1:
            raise PatientBootstrapError(
                f"multiple Google Drive root folders named {root_name}; set GOOGLE_DRIVE_ROOT_FOLDER_ID"
            )
        return matches[0]

    def _resolve_patients_folder(self, root_folder_id: str) -> dict:
        patients_folder_name = (getattr(self.settings, "patients_folder_name", "patients") or "patients").strip()
        matches = self.drive_gateway.find_folders_by_name(
            patients_folder_name,
            parent_id=root_folder_id,
        )
        if not matches:
            raise PatientBootstrapError(f"patients folder not found under root: {patients_folder_name}")
        if len(matches) > 1:
            raise PatientBootstrapError(f"multiple patients folders under root: {patients_folder_name}")
        return matches[0]

    def _parse_patient_folder_name(self, folder_name: str) -> tuple[str, str] | None:
        normalized_folder_name = normalize_drive_folder_name(folder_name)
        pattern = re.compile(self.settings.patient_folder_name_regex)
        match = pattern.match(normalized_folder_name)
        if not match:
            return None
        name = normalize_patient_name(match.group(1))
        birth = normalize_patient_birth(match.group(2))
        if not name or len(birth) != 8 or not birth.isdigit():
            return None
        return name, birth

    @staticmethod
    def _patient_id_from_drive_folder_id(folder_id: str) -> str:
        digest = hashlib.sha256(folder_id.encode("utf-8")).hexdigest()[:12].upper()
        return f"P_{digest}"

    @staticmethod
    def _patient_needs_update(existing: PatientProfile, incoming: PatientProfile) -> bool:
        return any(
            getattr(existing, field, None) != getattr(incoming, field, None)
            for field in (
                "name",
                "birth",
                "name_key",
                "birth_key",
                "drive_folder_id",
                "drive_folder_name",
                "status",
            )
        )