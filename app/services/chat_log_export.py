from __future__ import annotations

import csv
import io
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.repositories import MedicalRepository
from app.schemas import ChatLog, ChatLogExportState, RuntimeFailure
from app.services.drive import (
    DriveGateway,
    DriveLookupError,
    GOOGLE_DRIVE_FOLDER_MIME_TYPE,
)
from app.services.medical_wiki import now_kst_iso


logger = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))
CSV_MIME_TYPE = "text/csv"
STATE_ID = "main"


class ChatLogExportError(Exception):
    pass


@dataclass(frozen=True)
class ChatLogExportSlot:
    start_at: datetime
    end_at: datetime

    @property
    def start_iso(self) -> str:
        return self.start_at.isoformat()

    @property
    def end_iso(self) -> str:
        return self.end_at.isoformat()

    @property
    def file_name(self) -> str:
        return (
            f"chat_logs_{self.start_at.strftime('%Y%m%d_%H%M')}_"
            f"{self.end_at.strftime('%H%M')}.csv"
        )


@dataclass(frozen=True)
class ChatLogExportResult:
    ok: bool
    status: str
    slot_start_at: str = ""
    slot_end_at: str = ""
    exported_count: int = 0
    patient_count: int = 0
    file_name: str = ""
    file_action: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "status": self.status,
            "slot_start_at": self.slot_start_at,
            "slot_end_at": self.slot_end_at,
            "exported_count": self.exported_count,
            "patient_count": self.patient_count,
            "file_name": self.file_name,
            "file_action": self.file_action,
            "message": self.message,
        }


@dataclass(frozen=True)
class _ExportRow:
    created_at_kst: str
    patient_name: str
    patient_id: str
    kakao_user_hash_prefix: str
    message_type: str
    message: str


@dataclass
class ChatLogExportService:
    settings: Settings
    repository: MedicalRepository
    drive_gateway: DriveGateway

    def export_latest_closed_slot(self, *, now: datetime | None = None) -> ChatLogExportResult:
        if not self.settings.chat_log_export_enabled:
            return ChatLogExportResult(
                ok=True,
                status="disabled",
                message="chat log export is disabled",
            )

        now_dt = self._normalize_now(now)
        slot = self._latest_closed_slot(now_dt)
        lock_owner = f"chat-log-export-{uuid.uuid4().hex[:12]}"
        acquired_at = now_kst_iso()
        lease_expires_at = (
            datetime.now(KST) + timedelta(seconds=self.settings.chat_log_export_lock_lease_seconds)
        ).isoformat()
        state = self.repository.try_acquire_chat_log_export_lock(
            STATE_ID,
            lock_owner=lock_owner,
            lease_expires_at=lease_expires_at,
            now=acquired_at,
        )
        if state is None:
            return ChatLogExportResult(
                ok=False,
                status="locked",
                slot_start_at=slot.start_iso,
                slot_end_at=slot.end_iso,
                message="chat log export is already running",
            )

        if state.last_exported_slot_end_at and state.last_exported_slot_end_at >= slot.end_iso:
            self._release_state(
                state,
                lock_owner=lock_owner,
                slot_end_at=state.last_exported_slot_end_at,
                failure=None,
            )
            return ChatLogExportResult(
                ok=True,
                status="already_exported",
                slot_start_at=slot.start_iso,
                slot_end_at=slot.end_iso,
                message="slot already exported",
            )

        try:
            return self._export_slot(slot=slot, state=state, lock_owner=lock_owner)
        except Exception as exc:
            logger.exception("chat log export failed slot_end_at=%s", slot.end_iso)
            self._release_state(
                state,
                lock_owner=lock_owner,
                slot_end_at=state.last_exported_slot_end_at,
                failure=RuntimeFailure(
                    code="CHAT_LOG_EXPORT_FAILED",
                    message=" ".join(str(exc).split())[:300],
                    failed_at=now_kst_iso(),
                ),
            )
            return ChatLogExportResult(
                ok=False,
                status="failed",
                slot_start_at=slot.start_iso,
                slot_end_at=slot.end_iso,
                message="chat log export failed",
            )

    def _export_slot(
        self,
        *,
        slot: ChatLogExportSlot,
        state: ChatLogExportState,
        lock_owner: str,
    ) -> ChatLogExportResult:
        rows = self._collect_rows(slot)
        if not rows:
            self._release_state(
                state,
                lock_owner=lock_owner,
                slot_end_at=slot.end_iso,
                failure=None,
            )
            logger.info("chat log export empty slot_start_at=%s slot_end_at=%s", slot.start_iso, slot.end_iso)
            return ChatLogExportResult(
                ok=True,
                status="empty",
                slot_start_at=slot.start_iso,
                slot_end_at=slot.end_iso,
                exported_count=0,
                patient_count=0,
                file_name=slot.file_name,
                file_action="skipped",
            )

        folder_id = self._ensure_export_folder(slot)
        csv_bytes = build_chat_log_csv(rows)
        file_action = self._upload_slot_file(folder_id=folder_id, file_name=slot.file_name, content=csv_bytes)
        self._release_state(
            state,
            lock_owner=lock_owner,
            slot_end_at=slot.end_iso,
            failure=None,
        )
        patient_count = len({row.patient_id for row in rows})
        logger.info(
            "chat log export done slot_start_at=%s slot_end_at=%s row_count=%s patient_count=%s file_action=%s",
            slot.start_iso,
            slot.end_iso,
            len(rows),
            patient_count,
            file_action,
        )
        return ChatLogExportResult(
            ok=True,
            status="exported",
            slot_start_at=slot.start_iso,
            slot_end_at=slot.end_iso,
            exported_count=len(rows),
            patient_count=patient_count,
            file_name=slot.file_name,
            file_action=file_action,
        )

    def _collect_rows(self, slot: ChatLogExportSlot) -> list[_ExportRow]:
        rows: list[_ExportRow] = []
        for patient in self.repository.list_active_patients():
            logs = self.repository.list_chat_logs_for_patient(
                patient.patient_id,
                start_at=slot.start_iso,
                end_at=slot.end_iso,
            )
            for log in logs:
                rows.append(
                    _ExportRow(
                        created_at_kst=log.created_at,
                        patient_name=patient.name,
                        patient_id=patient.patient_id,
                        kakao_user_hash_prefix=log.kakao_user_id_hash[:16],
                        message_type=log.message_type,
                        message=log.message,
                    )
                )
        rows.sort(key=lambda row: (row.created_at_kst, row.patient_id, row.kakao_user_hash_prefix))
        return rows

    def _ensure_export_folder(self, slot: ChatLogExportSlot) -> str:
        root = self._resolve_root_folder()
        system_folder = self._ensure_single_folder(
            (self.settings.system_folder_name or "_system").strip() or "_system",
            parent_id=str(root.get("id") or ""),
        )
        export_root = self._ensure_single_folder(
            (self.settings.chat_log_export_folder_name or "chat_exports").strip() or "chat_exports",
            parent_id=str(system_folder.get("id") or ""),
        )
        year_folder = self._ensure_single_folder(slot.start_at.strftime("%Y"), parent_id=str(export_root.get("id") or ""))
        month_folder = self._ensure_single_folder(slot.start_at.strftime("%m"), parent_id=str(year_folder.get("id") or ""))
        day_folder = self._ensure_single_folder(slot.start_at.strftime("%d"), parent_id=str(month_folder.get("id") or ""))
        folder_id = str(day_folder.get("id") or "")
        if not folder_id:
            raise ChatLogExportError("chat log export folder ID is empty")
        return folder_id

    def _upload_slot_file(self, *, folder_id: str, file_name: str, content: bytes) -> str:
        matches = self.drive_gateway.find_files_by_name(
            file_name,
            parent_id=folder_id,
            mime_type=CSV_MIME_TYPE,
        )
        if len(matches) > 1:
            raise ChatLogExportError("multiple chat log export files found for the same slot")
        if matches:
            file_id = str(matches[0].get("id") or "")
            if not file_id:
                raise ChatLogExportError("existing chat log export file ID is empty")
            self.drive_gateway.update_file_bytes(
                file_id=file_id,
                content=content,
                mime_type=CSV_MIME_TYPE,
            )
            return "updated"
        self.drive_gateway.upload_file_bytes(
            parent_id=folder_id,
            file_name=file_name,
            content=content,
            mime_type=CSV_MIME_TYPE,
        )
        return "created"

    def _ensure_single_folder(self, folder_name: str, *, parent_id: str) -> dict:
        matches = self.drive_gateway.find_folders_by_name(folder_name, parent_id=parent_id)
        if len(matches) > 1:
            raise DriveLookupError(f"multiple Google Drive folders found: {folder_name}")
        if matches:
            folder = matches[0]
            if folder.get("trashed"):
                raise DriveLookupError(f"Google Drive folder is trashed: {folder_name}")
            if folder.get("mimeType") and folder.get("mimeType") != GOOGLE_DRIVE_FOLDER_MIME_TYPE:
                raise DriveLookupError(f"Google Drive item is not a folder: {folder_name}")
            return folder
        return self.drive_gateway.create_folder(folder_name, parent_id=parent_id)

    def _resolve_root_folder(self) -> dict:
        root_folder_id = (self.settings.google_drive_root_folder_id or "").strip()
        if root_folder_id:
            folder = self.drive_gateway.get_file(root_folder_id)
            if folder.get("trashed"):
                raise DriveLookupError("configured Google Drive root folder is trashed")
            if folder.get("mimeType") and folder.get("mimeType") != GOOGLE_DRIVE_FOLDER_MIME_TYPE:
                raise DriveLookupError("configured Google Drive root ID is not a folder")
            return folder

        root_name = (self.settings.google_drive_root_folder_name or "").strip()
        if not root_name:
            raise DriveLookupError("GOOGLE_DRIVE_ROOT_FOLDER_ID or GOOGLE_DRIVE_ROOT_FOLDER_NAME is required")
        matches = self.drive_gateway.find_folders_by_name(root_name)
        if not matches:
            raise DriveLookupError(f"Google Drive root folder not found: {root_name}")
        if len(matches) > 1:
            raise DriveLookupError(
                f"multiple Google Drive root folders named {root_name}; set GOOGLE_DRIVE_ROOT_FOLDER_ID"
            )
        return matches[0]

    def _release_state(
        self,
        state: ChatLogExportState,
        *,
        lock_owner: str,
        slot_end_at: str,
        failure: RuntimeFailure | None,
    ) -> None:
        if state.lock_owner and state.lock_owner != lock_owner:
            logger.warning("chat log export lock owner mismatch")
            return
        self.repository.upsert_chat_log_export_state(
            state.model_copy(
                update={
                    "last_exported_slot_end_at": slot_end_at,
                    "lock_owner": None,
                    "lease_expires_at": None,
                    "last_failure": failure,
                    "updated_at": now_kst_iso(),
                    "created_at": state.created_at or now_kst_iso(),
                }
            )
        )

    def _latest_closed_slot(self, now: datetime) -> ChatLogExportSlot:
        interval = self.settings.chat_log_export_interval_hours
        anchor = self.settings.chat_log_export_anchor_hour
        current = now.replace(minute=0, second=0, microsecond=0)
        while (current.hour - anchor) % interval != 0:
            current -= timedelta(hours=1)
        return ChatLogExportSlot(start_at=current - timedelta(hours=interval), end_at=current)

    @staticmethod
    def _normalize_now(now: datetime | None) -> datetime:
        if now is None:
            return datetime.now(KST)
        if now.tzinfo is None:
            return now.replace(tzinfo=KST)
        return now.astimezone(KST)


def build_chat_log_csv(rows: list[_ExportRow]) -> bytes:
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "created_at_kst",
            "patient_name",
            "patient_id",
            "kakao_user_hash_prefix",
            "message_type",
            "message",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                _safe_csv_cell(row.created_at_kst),
                _safe_csv_cell(row.patient_name),
                _safe_csv_cell(row.patient_id),
                _safe_csv_cell(row.kakao_user_hash_prefix),
                _safe_csv_cell(row.message_type),
                _safe_csv_cell(row.message),
            ]
        )
    return ("\ufeff" + output.getvalue()).encode("utf-8")


def _safe_csv_cell(value: object) -> str:
    text = str(value or "")
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text
