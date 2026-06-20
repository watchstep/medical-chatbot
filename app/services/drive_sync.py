from __future__ import annotations

import hashlib
import re
import uuid
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.config import Settings
from app.repositories import MedicalRepository
from app.schemas import (
    DriveFileIndexEntry,
    DriveFolderIndexEntry,
    DriveSyncState,
    MedicalSource,
    MedicalSourceRuntime,
    PatientProfile,
    RuntimeFailure,
    RuntimeSyncState,
    SourceRef,
)
from app.services.drive import DriveGateway, DriveLookupError
from app.services.medical_wiki import KST, MedicalWikiService, normalize_firestore_timestamp, now_kst_iso
from app.services.medical_wiki_extractor import MedicalWikiExtractionError
from app.services.wiki_rebuild_tasks import WikiRebuildTaskEnqueueService
from app.services.patient_identity import (
    make_patient_birth_key,
    make_patient_name_key,
    normalize_drive_folder_name,
    normalize_patient_birth,
    normalize_patient_name,
)


SUPPORTED_MIME_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/heic",
    "image/heif",
}
COUNTER_KEYS = ("created", "updated", "deleted", "skipped", "deferred", "failed")
PENDING_WIKI_STATUSES = {"WIKI_PENDING", "STALE", "FAILED"}
GOOGLE_DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


@dataclass(frozen=True)
class DriveSyncResult:
    ok: bool
    processed_count: int = 0
    created_count: int = 0
    updated_count: int = 0
    deleted_count: int = 0
    skipped_count: int = 0
    deferred_count: int = 0
    message: str = ""
    failed_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "processed_count": self.processed_count,
            "created_count": self.created_count,
            "updated_count": self.updated_count,
            "deleted_count": self.deleted_count,
            "skipped_count": self.skipped_count,
            "deferred_count": self.deferred_count,
            "message": self.message,
            "failed_count": self.failed_count,
        }


@dataclass(frozen=True)
class PatientFolderSyncResult:
    patient: PatientProfile
    folder_result: str
    file_counters: dict[str, int]


@dataclass
class DriveChangesSyncService:
    settings: Settings
    repository: MedicalRepository
    drive_gateway: DriveGateway
    wiki_service: MedicalWikiService
    wiki_rebuild_task_enqueue_service: WikiRebuildTaskEnqueueService | None = None
    patients_folder_id_cache: str = ""
    wiki_generations_this_run: int = 0
    index_dirty_patient_ids: set[str] = field(default_factory=set)

    def sync_changes(self) -> DriveSyncResult:
        scope_id = self.settings.drive_changes_scope_id
        state = self._ensure_drive_sync_state(scope_id)
        if not state.saved_page_token:
            token = self.drive_gateway.get_start_page_token()
            folder_index_count = self._ensure_folder_index_bootstrap() if self.settings.folder_index_bootstrap_on_changes else 0
            next_state = state.model_copy(
                update={
                    "saved_page_token": token,
                    "last_success_page_token": token,
                    "sync_status": "READY",
                    "folder_index_bootstrapped_at": now_kst_iso() if folder_index_count else state.folder_index_bootstrapped_at,
                    "updated_at": now_kst_iso(),
                }
            )
            self.repository.upsert_drive_sync_state(next_state)
            return DriveSyncResult(
                ok=True,
                skipped_count=folder_index_count,
                processed_count=folder_index_count,
                message="Changes API checkpoint initialized. Run again to process changes.",
            )

        locked_state = self._acquire_sync_lock(
            state,
            owner_prefix="changes-sync",
            lease_minutes=self.settings.changes_sync_lock_lease_minutes,
        )
        if locked_state is None:
            return DriveSyncResult(ok=False, message="sync lock is held by another worker")
        state = locked_state
        counters = self._empty_counters()
        self.wiki_generations_this_run = 0
        self.index_dirty_patient_ids.clear()
        folder_index_count = 0
        checkpoint_token = state.saved_page_token
        last_success_page_token = state.last_success_page_token
        try:
            if self.settings.folder_index_bootstrap_on_changes:
                folder_index_count = self._ensure_folder_index_bootstrap()

            # Drain a small amount of stale WIKI_PENDING work before reading new changes.
            self._merge_counters(
                counters,
                self._process_wiki_pending_backlog(
                    budget=self._remaining_wiki_generation_budget(),
                ),
            )
            self._compile_dirty_indexes()

            page_token = state.saved_page_token
            pages_processed = 0
            changes_processed = 0
            while page_token and pages_processed < self.settings.max_changes_pages_per_run:
                if changes_processed >= self.settings.max_changes_per_run:
                    break
                remaining = max(1, self.settings.max_changes_per_run - changes_processed)
                response = self.drive_gateway.list_changes(page_token, page_size=remaining)
                pages_processed += 1
                changes = response.get("changes", []) or []
                for change in changes:
                    self._merge_counters(counters, self._process_change(change))
                changes_processed += len(changes)

                next_page_token = response.get("nextPageToken")
                new_start_page_token = response.get("newStartPageToken")
                if (
                    next_page_token
                    and changes_processed < self.settings.max_changes_per_run
                    and pages_processed < self.settings.max_changes_pages_per_run
                ):
                    page_token = next_page_token
                    checkpoint_token = next_page_token
                    continue
                if next_page_token:
                    checkpoint_token = next_page_token
                elif new_start_page_token:
                    checkpoint_token = new_start_page_token
                    last_success_page_token = new_start_page_token
                else:
                    checkpoint_token = page_token
                break

            # Use leftover budget on WIKI_PENDING rows that may not have a Drive change anymore.
            self._merge_counters(
                counters,
                self._process_wiki_pending_backlog(
                    budget=self._remaining_wiki_generation_budget(),
                ),
            )
            self._compile_dirty_indexes()

            has_deferred_work = counters.get("deferred", 0) > 0 and self.settings.wiki_rebuild_worker_mode != "cloud_tasks"
            updates: dict[str, Any] = {
                "saved_page_token": state.saved_page_token if has_deferred_work else checkpoint_token,
                "sync_status": "READY",
                "lock_owner": None,
                "lease_expires_at": None,
                "retry_count": 0,
                "last_failure": None,
                "updated_at": now_kst_iso(),
            }
            if folder_index_count:
                updates["folder_index_bootstrapped_at"] = now_kst_iso()
            if not has_deferred_work:
                updates["last_synced_at"] = now_kst_iso()
                updates["last_success_page_token"] = last_success_page_token
            self.repository.upsert_drive_sync_state(state.model_copy(update=updates))
        except Exception as exc:
            self.repository.upsert_drive_sync_state(
                state.model_copy(
                    update={
                        "sync_status": "FAILED",
                        "lock_owner": None,
                        "lease_expires_at": None,
                        "retry_count": state.retry_count + 1,
                        "last_failure": RuntimeFailure(
                            code="DRIVE_CHANGES_SYNC_FAILED",
                            message=str(exc),
                            failed_at=now_kst_iso(),
                        ),
                        "updated_at": now_kst_iso(),
                    }
                )
            )
            return self._result_from_counters(
                ok=False,
                counters=counters,
                message="changes processing failed",
                extra_failed=1,
            )

        return self._result_from_counters(
            ok=True,
            counters=counters,
            message=(
                "changes partially processed; checkpoint retained for deferred wiki work"
                if counters.get("deferred", 0) > 0
                else "changes processed"
            ),
        )

    def sync_all(self) -> DriveSyncResult:
        scope_id = self.settings.drive_changes_scope_id
        state = self._ensure_drive_sync_state(scope_id)
        locked_state = self._acquire_sync_lock(
            state,
            owner_prefix="full-sync",
            lease_minutes=self.settings.full_sync_lock_lease_minutes,
        )
        if locked_state is None:
            return DriveSyncResult(ok=False, message="sync lock is held by another worker")
        state = locked_state
        counters = self._empty_counters()
        self.wiki_generations_this_run = 0
        self.index_dirty_patient_ids.clear()
        try:
            if getattr(self.settings, "drive_patient_bootstrap_on_full_sync", False):
                self._merge_counters(counters, self._bootstrap_patient_folders_from_drive())
            self._ensure_folder_index_bootstrap()
            for patient in self.repository.list_active_patients():
                result = self.sync_patient(patient.patient_id, reset_wiki_generation_count=False)
                counters["created"] += result.created_count
                counters["updated"] += result.updated_count
                counters["deleted"] += result.deleted_count
                counters["skipped"] += result.skipped_count
                counters["deferred"] += result.deferred_count
                counters["failed"] += result.failed_count

            self._merge_counters(
                counters,
                self._process_wiki_pending_backlog(
                    budget=max(self.settings.max_wiki_backlog_per_run, self._remaining_wiki_generation_budget()),
                ),
            )
            self._compile_dirty_indexes()
            token = ""
            try:
                token = self.drive_gateway.get_start_page_token()
            except DriveLookupError:
                pass
            self.repository.upsert_drive_sync_state(
                state.model_copy(
                    update={
                        "saved_page_token": token or state.saved_page_token,
                        "last_success_page_token": token or state.last_success_page_token,
                        "sync_status": "READY",
                        "lock_owner": None,
                        "lease_expires_at": None,
                        "retry_count": 0,
                        "last_failure": None,
                        "last_full_scan_at": now_kst_iso(),
                        "folder_index_bootstrapped_at": now_kst_iso(),
                        "updated_at": now_kst_iso(),
                    }
                )
            )
        except Exception as exc:
            self.repository.upsert_drive_sync_state(
                state.model_copy(
                    update={
                        "sync_status": "FAILED",
                        "lock_owner": None,
                        "lease_expires_at": None,
                        "retry_count": state.retry_count + 1,
                        "last_failure": RuntimeFailure(
                            code="DRIVE_FULL_SYNC_FAILED",
                            message=str(exc),
                            failed_at=now_kst_iso(),
                        ),
                        "updated_at": now_kst_iso(),
                    }
                )
            )
            return self._result_from_counters(
                ok=False,
                counters=counters,
                message="full sync failed",
                extra_failed=1,
            )
        return self._result_from_counters(ok=True, counters=counters, message="full sync completed")

    def sync_patient(self, patient_id: str, *, reset_wiki_generation_count: bool = True) -> DriveSyncResult:
        if reset_wiki_generation_count:
            self.wiki_generations_this_run = 0
            self.index_dirty_patient_ids.clear()
        patient = self.repository.get_patient(patient_id)
        if patient is None or patient.status != "active":
            return DriveSyncResult(ok=False, message="patient not found or inactive")
        self._upsert_folder_index_for_patient(patient)
        try:
            files = self.drive_gateway.list_files_in_folder(patient.drive_folder_id)
        except DriveLookupError:
            counters = self._empty_counters()
            result = self._mark_patient_folder_deleted(
                drive_folder_id=patient.drive_folder_id,
                reason="patient folder metadata or file listing lookup failed",
            )
            counters[result] = counters.get(result, 0) + 1
            if reset_wiki_generation_count:
                self._compile_dirty_indexes()
            return self._result_from_counters(
                ok=result != "failed",
                counters=counters,
                message="patient folder inaccessible during sync",
            )

        seen_drive_file_ids: set[str] = set()
        counters = self._empty_counters()
        for file_info in files:
            if file_info.get("mimeType") == GOOGLE_DRIVE_FOLDER_MIME_TYPE:
                counters["skipped"] += 1
                continue
            seen_drive_file_ids.add(file_info.get("id", ""))
            result = self._upsert_file_for_patient(patient_id=patient_id, file_info=file_info)
            counters[result] = counters.get(result, 0) + 1

        deleted = 0
        for source in self.repository.list_medical_sources(patient_id):
            drive_file_id = source.source_ref.drive_file_id
            if (
                drive_file_id
                and drive_file_id not in seen_drive_file_ids
                and source.source_status not in {"DELETED", "INACTIVE"}
            ):
                self._mark_source_deleted(patient_id=patient_id, source_id=source.source_id, drive_file_id=drive_file_id)
                deleted += 1
        counters["deleted"] += deleted

        if any(counters.get(key, 0) for key in ("created", "updated", "deleted", "deferred", "failed")):
            self._mark_index_dirty(patient_id)
        if reset_wiki_generation_count:
            self._compile_dirty_indexes()
        return self._result_from_counters(ok=True, counters=counters, message="patient sync completed")

    def rebuild_wiki_page(self, *, patient_id: str, source_id: str) -> dict[str, object]:
        deadline = time.monotonic() + self.settings.async_worker_processing_timeout_seconds
        self._ensure_async_worker_budget(deadline)
        source = self.repository.get_medical_source(patient_id, source_id)
        runtime = self.repository.get_source_runtime(patient_id, source_id)
        if source is None:
            raise ValueError(f"source not found: {patient_id}/{source_id}")
        if runtime is None:
            runtime = MedicalSourceRuntime(source_id=source_id, patient_id=patient_id)

        try:
            page = self.wiki_service.rebuild_wiki_page(patient_id=patient_id, source_id=source_id)
        except MedicalWikiExtractionError as exc:
            self._mark_wiki_page_failed(
                patient_id=patient_id,
                source_id=source_id,
                source=source,
                runtime=runtime,
                error=exc,
            )
            self.wiki_service.compile_wiki_index(patient_id=patient_id)
            raise

        self._ensure_async_worker_budget(deadline, reserve_seconds=5.0)
        source = self.repository.get_medical_source(patient_id, source_id)
        runtime = self.repository.get_source_runtime(patient_id, source_id)
        drive_file_index = (
            self.repository.get_drive_file_index(source.source_ref.drive_file_id)
            if source is not None and source.source_ref.drive_file_id
            else None
        )
        active_source = (
            source.model_copy(update={"source_status": "ACTIVE", "updated_at": now_kst_iso()})
            if source is not None and source.source_status != "UNSUPPORTED"
            else source
        )
        ready_runtime = (
            runtime.model_copy(
                update={
                    "sync": runtime.sync.model_copy(
                        update={
                            "status": "READY",
                            "last_synced_at": now_kst_iso(),
                            "lock_owner": None,
                            "lease_expires_at": None,
                            "last_failure": None,
                        }
                    ),
                    "wiki_sync": runtime.wiki_sync.model_copy(
                        update={
                            "status": "READY",
                            "last_generated_at": now_kst_iso(),
                            "lock_owner": None,
                            "lease_expires_at": None,
                            "last_failure": None,
                        }
                    ),
                    "updated_at": now_kst_iso(),
                }
            )
            if runtime is not None
            else None
        )
        active_drive_file_index = (
            drive_file_index.model_copy(update={"status": "ACTIVE", "updated_at": now_kst_iso()})
            if drive_file_index is not None
            else None
        )
        if active_source is not None or ready_runtime is not None or active_drive_file_index is not None:
            self.repository.upsert_source_status_bundle(
                source=active_source,
                runtime=ready_runtime,
                drive_file_index=active_drive_file_index,
            )
        self._ensure_async_worker_budget(deadline, reserve_seconds=5.0)
        index = self.wiki_service.compile_wiki_index(patient_id=patient_id)
        return {
            "ok": True,
            "page": page.model_dump(exclude_none=True),
            "index_page_count": len(index.pages),
        }

    def _ensure_async_worker_budget(self, deadline: float, *, reserve_seconds: float = 0.0) -> None:
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds < reserve_seconds:
            raise TimeoutError(
                f"async worker processing budget exceeded; remaining_seconds={remaining_seconds:.3f}"
            )

    def recompile_wiki_index(self, *, patient_id: str) -> dict[str, object]:
        index = self.wiki_service.compile_wiki_index(patient_id=patient_id)
        return {"ok": True, "index": index.model_dump(exclude_none=True)}

    def _process_change(self, change: dict) -> dict[str, int]:
        drive_file_id = change.get("fileId", "")
        if not drive_file_id:
            return self._counter_for("skipped")

        existing_file_index = self.repository.get_drive_file_index(drive_file_id)
        existing_folder_index = self.repository.get_drive_folder_index(drive_file_id)

        if change.get("removed"):
            if existing_folder_index is not None:
                result = self._mark_patient_folder_deleted(
                    drive_folder_id=drive_file_id,
                    reason="drive folder removed",
                )
                if result == "deleted":
                    self._mark_index_dirty(existing_folder_index.patient_id)
                return self._counter_for(result)
            if existing_file_index is None:
                return self._counter_for("skipped")
            self._mark_source_deleted(
                patient_id=existing_file_index.patient_id,
                source_id=existing_file_index.source_id,
                drive_file_id=drive_file_id,
            )
            self._mark_index_dirty(existing_file_index.patient_id)
            return self._counter_for("deleted")

        try:
            file_info = change.get("file") or self.drive_gateway.get_file(drive_file_id)
        except DriveLookupError:
            if existing_folder_index is not None:
                result = self._mark_patient_folder_deleted(
                    drive_folder_id=drive_file_id,
                    reason="drive folder metadata lookup failed",
                )
                if result == "deleted":
                    self._mark_index_dirty(existing_folder_index.patient_id)
                return self._counter_for(result)
            if existing_file_index is None:
                return self._counter_for("skipped")
            self._mark_source_inactive(
                patient_id=existing_file_index.patient_id,
                source_id=existing_file_index.source_id,
                drive_file_id=drive_file_id,
                reason="drive file metadata lookup failed",
            )
            self._mark_index_dirty(existing_file_index.patient_id)
            return self._counter_for("deleted")

        if file_info.get("mimeType") == GOOGLE_DRIVE_FOLDER_MIME_TYPE:
            if file_info.get("trashed"):
                result = self._mark_patient_folder_deleted(
                    drive_folder_id=drive_file_id,
                    reason="drive folder trashed",
                )
                if result == "deleted" and existing_folder_index is not None:
                    self._mark_index_dirty(existing_folder_index.patient_id)
                return self._counter_for(result)
            return self._upsert_patient_folder_from_change(file_info)

        if file_info.get("trashed"):
            if existing_file_index is None:
                return self._counter_for("skipped")
            self._mark_source_deleted(
                patient_id=existing_file_index.patient_id,
                source_id=existing_file_index.source_id,
                drive_file_id=drive_file_id,
            )
            self._mark_index_dirty(existing_file_index.patient_id)
            return self._counter_for("deleted")

        if existing_file_index is not None:
            current_patient_id = self._patient_id_from_file_parents(file_info)
            if not current_patient_id:
                self._mark_source_inactive(
                    patient_id=existing_file_index.patient_id,
                    source_id=existing_file_index.source_id,
                    drive_file_id=drive_file_id,
                    reason="drive file is no longer in an active patient folder",
                )
                self._mark_index_dirty(existing_file_index.patient_id)
                return self._counter_for("deleted")
            if current_patient_id != existing_file_index.patient_id:
                self._mark_source_inactive(
                    patient_id=existing_file_index.patient_id,
                    source_id=existing_file_index.source_id,
                    drive_file_id=drive_file_id,
                    reason="drive file moved to another patient folder",
                )
                self._mark_index_dirty(existing_file_index.patient_id)
                result = self._upsert_file_for_patient(
                    patient_id=current_patient_id,
                    file_info=file_info,
                    source_id=self._new_source_id(current_patient_id),
                )
                if result in {"created", "updated", "deleted", "deferred", "failed"}:
                    self._mark_index_dirty(current_patient_id)
                return self._counter_for(result)
            result = self._upsert_file_for_patient(
                patient_id=existing_file_index.patient_id,
                file_info=file_info,
                source_id=existing_file_index.source_id,
            )
            if result in {"created", "updated", "deleted", "deferred", "failed"}:
                self._mark_index_dirty(existing_file_index.patient_id)
            return self._counter_for(result)

        patient_id = self._patient_id_from_file_parents(file_info)
        if not patient_id:
            return self._counter_for("skipped")
        result = self._upsert_file_for_patient(patient_id=patient_id, file_info=file_info)
        if result in {"created", "updated", "deleted", "deferred", "failed"}:
            self._mark_index_dirty(patient_id)
        return self._counter_for(result)

    def _patient_id_from_file_parents(self, file_info: dict) -> str:
        for parent_id in file_info.get("parents", []) or []:
            folder_entry = self.repository.get_drive_folder_index(parent_id)
            if folder_entry is not None and folder_entry.status == "active":
                patient = self.repository.get_patient(folder_entry.patient_id)
                if patient is not None and patient.status == "active":
                    return patient.patient_id

            # Race-safe fallback: a file change can arrive before the corresponding
            # new patient-folder change is processed. If the parent itself is a
            # valid patient folder under the configured patients root, register the
            # patient folder first, then attach the file to that patient.
            try:
                parent_info = self.drive_gateway.get_file(parent_id)
            except DriveLookupError:
                continue
            if parent_info.get("mimeType") != GOOGLE_DRIVE_FOLDER_MIME_TYPE or parent_info.get("trashed"):
                continue
            patient_result = self._patient_from_folder_info(parent_info, sync_files=False)
            if patient_result is not None:
                patient = patient_result.patient
                if patient.status == "active":
                    return patient.patient_id
        return ""

    def _upsert_file_for_patient(
        self,
        *,
        patient_id: str,
        file_info: dict,
        source_id: str = "",
    ) -> str:
        drive_file_id = file_info.get("id", "")
        if not drive_file_id:
            return "skipped"
        patient = self.repository.get_patient(patient_id)
        if patient is None:
            return "skipped"
        existing_index = self.repository.get_drive_file_index(drive_file_id)
        source_id = source_id or (existing_index.source_id if existing_index else "") or self._new_source_id(patient_id)
        source_status = "ACTIVE" if file_info.get("mimeType") in SUPPORTED_MIME_TYPES else "UNSUPPORTED"
        source = MedicalSource(
            source_id=source_id,
            patient_id=patient_id,
            source_ref=SourceRef(
                drive_file_id=drive_file_id,
                drive_folder_id=patient.drive_folder_id,
                original_filename=file_info.get("name", ""),
                mime_type=file_info.get("mimeType", ""),
                file_size_bytes=self._int_or_none(file_info.get("size")),
                file_hash=file_info.get("md5Checksum", ""),
                drive_modified_at=normalize_firestore_timestamp(file_info.get("modifiedTime")),
            ),
            source_status=source_status,
            last_sync_at=now_kst_iso(),
            updated_at=now_kst_iso(),
        )
        old_source = self.repository.get_medical_source(patient_id, source_id)
        old_runtime = self.repository.get_source_runtime(patient_id, source_id)
        changed = old_source is None or self._source_changed(old_source=old_source, new_source=source)
        wiki_pending = old_runtime is None or old_runtime.sync.status != "READY" or old_runtime.wiki_sync.status != "READY"
        if old_source is not None and not changed and not wiki_pending:
            return "skipped"

        if old_source is not None and old_source.created_at:
            source = source.model_copy(update={"created_at": old_source.created_at})
        else:
            source = source.model_copy(update={"created_at": now_kst_iso()})

        runtime = old_runtime or MedicalSourceRuntime(source_id=source_id, patient_id=patient_id)
        if old_source is not None and changed:
            runtime = runtime.model_copy(
                update={
                    "gemini_file": runtime.gemini_file.model_copy(
                        update={
                            "state": "EXPIRED",
                            "error": "source changed",
                            "last_checked_at": now_kst_iso(),
                            "source_drive_modified_at": "",
                            "source_file_hash": "",
                            "source_file_size_bytes": None,
                        }
                    ),
                    "sync": runtime.sync.model_copy(
                        update={"status": "STALE", "lock_owner": None, "lease_expires_at": None}
                    ),
                    "wiki_sync": runtime.wiki_sync.model_copy(update={"status": "WIKI_PENDING"}),
                    "updated_at": now_kst_iso(),
                }
            )
            source = source.model_copy(update={"source_status": "STALE"})

        drive_index = DriveFileIndexEntry(
            drive_file_id=drive_file_id,
            patient_id=patient_id,
            source_id=source_id,
            drive_folder_id=patient.drive_folder_id,
            status=source.source_status,
            created_at=existing_index.created_at if existing_index is not None else now_kst_iso(),
            updated_at=now_kst_iso(),
        )
        if source_status == "UNSUPPORTED":
            runtime = MedicalSourceRuntime(
                source_id=source_id,
                patient_id=patient_id,
                sync=RuntimeSyncState(
                    status="FAILED",
                    last_failure=RuntimeFailure(
                        code="DRIVE_FILE_UNSUPPORTED",
                        message="unsupported mime type",
                        failed_at=now_kst_iso(),
                    ),
                ),
                wiki_sync=RuntimeSyncState(status="FAILED"),
                updated_at=now_kst_iso(),
            )
            self.repository.upsert_source_bundle(
                source=source,
                runtime=runtime,
                drive_file_index=drive_index,
            )
            return "skipped"

        self.repository.upsert_source_bundle(
            source=source,
            runtime=runtime,
            drive_file_index=drive_index,
        )

        return self._generate_wiki_for_source(
            patient_id=patient_id,
            source_id=source_id,
            source=source,
            old_source=old_source,
        )

    def _generate_wiki_for_source(
        self,
        *,
        patient_id: str,
        source_id: str,
        source: MedicalSource,
        old_source: MedicalSource | None,
    ) -> str:
        runtime = self.repository.get_source_runtime(patient_id, source_id) or MedicalSourceRuntime(
            source_id=source_id,
            patient_id=patient_id,
        )
        if self.settings.wiki_rebuild_worker_mode == "cloud_tasks":
            return self._enqueue_wiki_rebuild_for_source(
                patient_id=patient_id,
                source_id=source_id,
                source=source,
                runtime=runtime,
            )

        if not self._can_generate_wiki_page():
            runtime = runtime.model_copy(
                update={
                    "sync": runtime.sync.model_copy(update={"status": "WIKI_PENDING"}),
                    "wiki_sync": runtime.wiki_sync.model_copy(update={"status": "WIKI_PENDING"}),
                    "updated_at": now_kst_iso(),
                }
            )
            self.repository.upsert_source_runtime(runtime)
            return "deferred"

        runtime = runtime.model_copy(
            update={
                "sync": runtime.sync.model_copy(update={"status": "WIKI_PROCESSING"}),
                "wiki_sync": runtime.wiki_sync.model_copy(update={"status": "WIKI_PROCESSING"}),
                "updated_at": now_kst_iso(),
            }
        )
        self.repository.upsert_source_runtime(runtime)
        try:
            self.wiki_service.rebuild_wiki_page(patient_id=patient_id, source_id=source_id)
        except MedicalWikiExtractionError as exc:
            self._mark_wiki_page_failed(
                patient_id=patient_id,
                source_id=source_id,
                source=source,
                runtime=runtime,
                error=exc,
            )
            self.wiki_generations_this_run += 1
            return "failed"
        self.wiki_generations_this_run += 1
        active_source = source.model_copy(update={"source_status": "ACTIVE", "updated_at": now_kst_iso()})
        drive_file_index = self.repository.get_drive_file_index(source.source_ref.drive_file_id)
        active_drive_file_index = (
            drive_file_index.model_copy(update={"status": "ACTIVE", "updated_at": now_kst_iso()})
            if drive_file_index is not None
            else None
        )
        runtime = runtime.model_copy(
            update={
                "sync": runtime.sync.model_copy(
                    update={
                        "status": "READY",
                        "last_synced_at": now_kst_iso(),
                        "lock_owner": None,
                        "lease_expires_at": None,
                        "last_failure": None,
                    }
                ),
                "wiki_sync": runtime.wiki_sync.model_copy(
                    update={"status": "READY", "last_generated_at": now_kst_iso(), "last_failure": None}
                ),
                "updated_at": now_kst_iso(),
            }
        )
        self.repository.upsert_source_status_bundle(
            source=active_source,
            runtime=runtime,
            drive_file_index=active_drive_file_index,
        )
        return "updated" if old_source is not None else "created"


    def _enqueue_wiki_rebuild_for_source(
        self,
        *,
        patient_id: str,
        source_id: str,
        source: MedicalSource,
        runtime: MedicalSourceRuntime,
    ) -> str:
        """Schedule wiki page rebuild without blocking Drive sync.

        Cloud Tasks mode treats wiki generation as an asynchronous worker job.
        The source stays out of the Router catalog until the worker endpoint
        rebuilds the page and marks runtime READY.
        """
        pending_runtime = runtime.model_copy(
            update={
                "sync": runtime.sync.model_copy(
                    update={
                        "status": "WIKI_PENDING",
                        "lock_owner": None,
                        "lease_expires_at": None,
                    }
                ),
                "wiki_sync": runtime.wiki_sync.model_copy(
                    update={
                        "status": "WIKI_PENDING",
                        "lock_owner": None,
                        "lease_expires_at": None,
                    }
                ),
                "updated_at": now_kst_iso(),
            }
        )
        self.repository.upsert_source_runtime(pending_runtime)

        if self.wiki_rebuild_task_enqueue_service is None:
            self._append_wiki_log(
                patient_id,
                event_type="wiki_rebuild_enqueue_failed",
                title="wiki rebuild task enqueue service unavailable",
                source_id=source_id,
                status="failed",
                message="wiki rebuild worker mode is cloud_tasks but enqueue service is unavailable",
                failure_code="WIKI_REBUILD_TASK_UNAVAILABLE",
            )
            return "failed"

        try:
            result = self.wiki_rebuild_task_enqueue_service.enqueue_wiki_rebuild(
                patient_id=patient_id,
                source_id=source_id,
                reason="drive_sync",
            )
        except Exception as exc:
            failure = RuntimeFailure(
                code="WIKI_REBUILD_TASK_ENQUEUE_FAILED",
                message=str(exc),
                failed_at=now_kst_iso(),
            )
            failed_runtime = pending_runtime.model_copy(
                update={
                    "sync": pending_runtime.sync.model_copy(
                        update={"status": "WIKI_PENDING", "last_failure": failure}
                    ),
                    "wiki_sync": pending_runtime.wiki_sync.model_copy(
                        update={"status": "WIKI_PENDING", "last_failure": failure}
                    ),
                    "updated_at": now_kst_iso(),
                }
            )
            self.repository.upsert_source_runtime(failed_runtime)
            self._append_wiki_log(
                patient_id,
                event_type="wiki_rebuild_enqueue_failed",
                title="wiki rebuild task enqueue failed",
                source_id=source_id,
                status="failed",
                message=str(exc),
                failure_code="WIKI_REBUILD_TASK_ENQUEUE_FAILED",
            )
            return "failed"

        queued_runtime = pending_runtime.model_copy(
            update={
                "sync": pending_runtime.sync.model_copy(update={"status": "WIKI_PROCESSING"}),
                "wiki_sync": pending_runtime.wiki_sync.model_copy(update={"status": "WIKI_PROCESSING"}),
                "updated_at": now_kst_iso(),
            }
        )
        self.repository.upsert_source_runtime(queued_runtime)
        self._append_wiki_log(
            patient_id,
            event_type="wiki_rebuild_enqueued",
            title="wiki rebuild task enqueued",
            source_id=source_id,
            status="success",
            message=f"wiki rebuild task enqueued duplicate={result.duplicate}",
        )
        return "deferred"

    def _process_wiki_pending_backlog(self, *, budget: int) -> dict[str, int]:
        counters = self._empty_counters()
        if budget <= 0:
            return counters
        processed_sources = 0
        processed_patients: set[str] = set()
        for patient in self.repository.list_active_patients():
            if processed_sources >= budget:
                break
            for runtime in self.repository.list_source_runtimes(patient.patient_id):
                if processed_sources >= budget:
                    break
                if not self._runtime_needs_wiki_backlog(runtime):
                    continue
                source = self.repository.get_medical_source(runtime.patient_id, runtime.source_id)
                if source is None or source.source_status in {"DELETED", "INACTIVE", "UNSUPPORTED"}:
                    counters["skipped"] += 1
                    continue
                result = self._generate_wiki_for_source(
                    patient_id=runtime.patient_id,
                    source_id=runtime.source_id,
                    source=source,
                    old_source=source,
                )
                counters[result] = counters.get(result, 0) + 1
                processed_sources += 1
                if result in {"created", "updated", "deleted", "failed"}:
                    processed_patients.add(runtime.patient_id)
                    self._mark_index_dirty(runtime.patient_id)
        if processed_sources:
            for patient_id in processed_patients:
                self._append_wiki_log(
                    patient_id,
                    event_type="backlog_drained",
                    title="pending wiki backlog processed",
                    status="success",
                    message=f"processed {processed_sources} pending source(s)",
                    index_updated=True,
                )
        return counters

    def _runtime_needs_wiki_backlog(self, runtime: MedicalSourceRuntime) -> bool:
        return (
            runtime.sync.status in PENDING_WIKI_STATUSES
            or runtime.wiki_sync.status in PENDING_WIKI_STATUSES
        )

    def _mark_wiki_page_failed(
        self,
        *,
        patient_id: str,
        source_id: str,
        source: MedicalSource,
        runtime: MedicalSourceRuntime,
        error: Exception,
    ) -> None:
        failure = RuntimeFailure(
            code="WIKI_PAGE_GENERATION_FAILED",
            message=str(error),
            failed_at=now_kst_iso(),
        )
        failed_runtime = runtime.model_copy(
            update={
                "sync": runtime.sync.model_copy(
                    update={
                        "status": "FAILED",
                        "last_failure": failure,
                        "lock_owner": None,
                        "lease_expires_at": None,
                    }
                ),
                "wiki_sync": runtime.wiki_sync.model_copy(
                    update={"status": "FAILED", "last_failure": failure}
                ),
                "updated_at": now_kst_iso(),
            }
        )
        self.repository.upsert_source_status_bundle(
            source=source.model_copy(update={"source_status": "STALE", "updated_at": now_kst_iso()}),
            runtime=failed_runtime,
            drive_file_index=None,
        )
        self._append_wiki_log(
            patient_id,
            event_type="page_failed",
            title="source summary page generation failed",
            source_id=source_id,
            page_ids=[f"PAGE_{source_id}"],
            status="failed",
            message="source_summary page generation failed",
            failure_code=failure.code,
        )

    def _mark_source_deleted(self, *, patient_id: str, source_id: str, drive_file_id: str) -> None:
        source = self.repository.get_medical_source(patient_id, source_id)
        deleted_source = (
            source.model_copy(update={"source_status": "DELETED", "updated_at": now_kst_iso()})
            if source is not None
            else None
        )
        runtime = self.repository.get_source_runtime(patient_id, source_id) or MedicalSourceRuntime(
            source_id=source_id,
            patient_id=patient_id,
        )
        runtime = runtime.model_copy(
            update={
                "gemini_file": runtime.gemini_file.model_copy(
                    update={
                        "state": "EXPIRED",
                        "error": "source deleted",
                        "last_checked_at": now_kst_iso(),
                        "source_drive_modified_at": "",
                        "source_file_hash": "",
                        "source_file_size_bytes": None,
                    }
                ),
                "sync": runtime.sync.model_copy(
                    update={"status": "EXPIRED", "lock_owner": None, "lease_expires_at": None}
                ),
                "wiki_sync": runtime.wiki_sync.model_copy(update={"status": "EXPIRED"}),
                "updated_at": now_kst_iso(),
            }
        )
        file_index = self.repository.get_drive_file_index(drive_file_id)
        deleted_index = (
            file_index.model_copy(update={"status": "DELETED", "updated_at": now_kst_iso()})
            if file_index is not None
            else None
        )
        self.repository.upsert_source_status_bundle(
            source=deleted_source,
            runtime=runtime,
            drive_file_index=deleted_index,
        )
        self._append_wiki_log(
            patient_id,
            event_type="source_deleted",
            title="source deleted from Drive",
            source_id=source_id,
            index_updated=True,
            status="success",
            message="source marked deleted and excluded from index",
        )

    def _mark_source_inactive(self, *, patient_id: str, source_id: str, drive_file_id: str, reason: str) -> None:
        source = self.repository.get_medical_source(patient_id, source_id)
        inactive_source = (
            source.model_copy(update={"source_status": "INACTIVE", "updated_at": now_kst_iso()})
            if source is not None
            else None
        )
        runtime = self.repository.get_source_runtime(patient_id, source_id) or MedicalSourceRuntime(
            source_id=source_id,
            patient_id=patient_id,
        )
        failure = RuntimeFailure(
            code="DRIVE_FILE_INACCESSIBLE",
            message=reason,
            failed_at=now_kst_iso(),
        )
        runtime = runtime.model_copy(
            update={
                "gemini_file": runtime.gemini_file.model_copy(
                    update={
                        "state": "EXPIRED",
                        "error": "source inactive",
                        "last_checked_at": now_kst_iso(),
                        "source_drive_modified_at": "",
                        "source_file_hash": "",
                        "source_file_size_bytes": None,
                    }
                ),
                "sync": runtime.sync.model_copy(
                    update={
                        "status": "EXPIRED",
                        "last_failure": failure,
                        "lock_owner": None,
                        "lease_expires_at": None,
                    }
                ),
                "wiki_sync": runtime.wiki_sync.model_copy(
                    update={"status": "EXPIRED", "last_failure": failure}
                ),
                "updated_at": now_kst_iso(),
            }
        )
        file_index = self.repository.get_drive_file_index(drive_file_id)
        inactive_index = (
            file_index.model_copy(update={"status": "INACTIVE", "updated_at": now_kst_iso()})
            if file_index is not None
            else None
        )
        self.repository.upsert_source_status_bundle(
            source=inactive_source,
            runtime=runtime,
            drive_file_index=inactive_index,
        )
        self._append_wiki_log(
            patient_id,
            event_type="source_inactivated",
            title="source inactivated",
            source_id=source_id,
            index_updated=True,
            status="failed",
            message=reason,
            failure_code=failure.code,
        )

    def _bootstrap_patient_folders_from_drive(self) -> dict[str, int]:
        counters = self._empty_counters()
        try:
            patients_folder_id = self._resolve_patients_folder_id()
            patient_folders = self.drive_gateway.list_child_folders(patients_folder_id)
        except Exception:
            counters["failed"] += 1
            return counters

        seen_folder_ids: set[str] = set()
        for folder_info in patient_folders:
            folder_id = str(folder_info.get("id") or "").strip()
            if not folder_id:
                counters["skipped"] += 1
                continue
            seen_folder_ids.add(folder_id)
            self._merge_counters(counters, self._upsert_patient_folder_from_change(folder_info))

        for patient in self.repository.list_active_patients():
            if patient.drive_folder_id and patient.drive_folder_id not in seen_folder_ids:
                result = self._mark_patient_folder_deleted(
                    drive_folder_id=patient.drive_folder_id,
                    reason="patient folder missing during full sync",
                )
                counters[result] = counters.get(result, 0) + 1
                if result == "deleted":
                    self._mark_index_dirty(patient.patient_id)

        return counters

    def _upsert_patient_folder_from_change(self, folder_info: dict) -> dict[str, int]:
        if folder_info.get("mimeType") != GOOGLE_DRIVE_FOLDER_MIME_TYPE:
            return self._counter_for("skipped")
        if folder_info.get("trashed"):
            return self._counter_for(
                self._mark_patient_folder_deleted(
                    drive_folder_id=str(folder_info.get("id") or ""),
                    reason="drive folder trashed",
                )
            )
        existing_folder_index = self.repository.get_drive_folder_index(str(folder_info.get("id") or ""))
        if not self._is_direct_child_of_patients_root(folder_info):
            if existing_folder_index is not None:
                return self._counter_for(
                    self._mark_patient_folder_deleted(
                        drive_folder_id=existing_folder_index.drive_folder_id,
                        reason="patient folder moved outside patients root",
                    )
                )
            return self._counter_for("skipped")

        patient_result = self._patient_from_folder_info(folder_info, sync_files=True)
        if patient_result is None:
            if existing_folder_index is not None:
                return self._counter_for(
                    self._mark_patient_folder_deleted(
                        drive_folder_id=existing_folder_index.drive_folder_id,
                        reason="patient folder name no longer matches required pattern",
                    )
                )
            return self._counter_for("skipped")
        counters = self._counter_for(
            patient_result.folder_result if patient_result.folder_result in {"created", "updated"} else "skipped"
        )
        self._merge_counters(counters, patient_result.file_counters)
        return counters

    def _patient_from_folder_info(self, folder_info: dict, *, sync_files: bool) -> PatientFolderSyncResult | None:
        folder_id = str(folder_info.get("id") or "").strip()
        folder_name = normalize_drive_folder_name(str(folder_info.get("name") or ""))
        if not folder_id or not folder_name:
            return None
        if not self._is_direct_child_of_patients_root(folder_info):
            return None
        parsed = self._parse_patient_folder_name(folder_name)
        if parsed is None:
            return None
        name, birth = parsed
        patient, result = self._upsert_patient_from_folder(
            name=name,
            birth=birth,
            drive_folder_id=folder_id,
            drive_folder_name=folder_name,
        )
        file_counters = self._empty_counters()
        if sync_files:
            file_counters = self._sync_files_in_patient_folder(
                patient_id=patient.patient_id,
                drive_folder_id=folder_id,
            )
            if any(file_counters.get(key, 0) for key in ("created", "updated", "deleted", "deferred", "failed")):
                self._mark_index_dirty(patient.patient_id)
        return PatientFolderSyncResult(patient=patient, folder_result=result, file_counters=file_counters)

    def _upsert_patient_from_folder(
        self,
        *,
        name: str,
        birth: str,
        drive_folder_id: str,
        drive_folder_name: str,
    ) -> tuple[PatientProfile, str]:
        now = now_kst_iso()
        name_key = make_patient_name_key(name)
        birth_key = make_patient_birth_key(birth)
        existing_folder_index = self.repository.get_drive_folder_index(drive_folder_id)
        existing_by_folder = (
            self.repository.get_patient(existing_folder_index.patient_id)
            if existing_folder_index is not None
            else None
        )
        existing_by_identity = None
        find_by_identity = getattr(self.repository, "find_active_patient_by_identity", None)
        if callable(find_by_identity):
            existing_by_identity = find_by_identity(name=name, birth=birth)
        existing = existing_by_folder or existing_by_identity
        patient_id = existing.patient_id if existing is not None else self._patient_id_from_drive_folder_id(drive_folder_id)

        if existing_by_identity is not None and existing_by_identity.drive_folder_id != drive_folder_id:
            old_folder_index = self.repository.get_drive_folder_index(existing_by_identity.drive_folder_id)
            if old_folder_index is not None and old_folder_index.status == "active":
                self.repository.upsert_drive_folder_index(
                    old_folder_index.model_copy(update={"status": "deleted", "updated_at": now})
                )

        patient = PatientProfile(
            patient_id=patient_id,
            name=name,
            birth=birth,
            name_key=name_key,
            birth_key=birth_key,
            drive_folder_id=drive_folder_id,
            drive_folder_name=drive_folder_name,
            status="active",
            created_at=existing.created_at if existing is not None and existing.created_at else now,
            updated_at=now,
        )
        result = "created"
        if existing is not None:
            result = "skipped" if not self._patient_needs_update(existing, patient) else "updated"
        self.repository.upsert_patient(patient)
        self._upsert_folder_index_for_patient(patient)
        return patient, result

    def _sync_files_in_patient_folder(self, *, patient_id: str, drive_folder_id: str) -> dict[str, int]:
        counters = self._empty_counters()
        try:
            files = self.drive_gateway.list_files_in_folder(drive_folder_id)
        except DriveLookupError:
            counters["failed"] += 1
            return counters
        for file_info in files:
            if file_info.get("mimeType") == GOOGLE_DRIVE_FOLDER_MIME_TYPE:
                counters["skipped"] += 1
                continue
            result = self._upsert_file_for_patient(patient_id=patient_id, file_info=file_info)
            counters[result] = counters.get(result, 0) + 1
        return counters

    def _mark_patient_folder_deleted(self, *, drive_folder_id: str, reason: str) -> str:
        if not drive_folder_id:
            return "skipped"
        folder_index = self.repository.get_drive_folder_index(drive_folder_id)
        if folder_index is None:
            return "skipped"
        now = now_kst_iso()
        patient_id = folder_index.patient_id
        patient = self.repository.get_patient(patient_id)
        if patient is not None and patient.status != "inactive":
            self.repository.upsert_patient(
                patient.model_copy(update={"status": "inactive", "updated_at": now})
            )
        self.repository.upsert_drive_folder_index(
            folder_index.model_copy(update={"status": "deleted", "updated_at": now})
        )
        for source in self.repository.list_medical_sources(patient_id):
            if source.source_status not in {"DELETED", "INACTIVE"} and source.source_ref.drive_file_id:
                self._mark_source_deleted(
                    patient_id=patient_id,
                    source_id=source.source_id,
                    drive_file_id=source.source_ref.drive_file_id,
                )
        self._mark_index_dirty(patient_id)
        self._append_wiki_log(
            patient_id,
            event_type="patient_folder_deleted",
            title="patient folder deleted from Drive",
            status="success",
            message=reason,
            index_updated=True,
        )
        return "deleted"

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

    def _is_direct_child_of_patients_root(self, folder_info: dict) -> bool:
        try:
            patients_folder_id = self._resolve_patients_folder_id()
        except DriveLookupError:
            return False
        return patients_folder_id in (folder_info.get("parents") or [])

    def _resolve_patients_folder_id(self) -> str:
        if self.patients_folder_id_cache:
            return self.patients_folder_id_cache
        root_folder = self._resolve_root_folder()
        patients_folder_name = (getattr(self.settings, "patients_folder_name", "patients") or "patients").strip()
        matches = self.drive_gateway.find_folders_by_name(patients_folder_name, parent_id=root_folder["id"])
        if not matches:
            raise DriveLookupError(f"patients folder not found under root: {patients_folder_name}")
        if len(matches) > 1:
            raise DriveLookupError(f"multiple patients folders under root: {patients_folder_name}")
        self.patients_folder_id_cache = str(matches[0].get("id") or "")
        if not self.patients_folder_id_cache:
            raise DriveLookupError("patients folder ID is empty")
        return self.patients_folder_id_cache

    def _resolve_root_folder(self) -> dict:
        root_folder_id = (getattr(self.settings, "google_drive_root_folder_id", "") or "").strip()
        if root_folder_id:
            folder = self.drive_gateway.get_file(root_folder_id)
            if folder.get("trashed"):
                raise DriveLookupError("configured Google Drive root folder is trashed")
            if folder.get("mimeType") and folder.get("mimeType") != GOOGLE_DRIVE_FOLDER_MIME_TYPE:
                raise DriveLookupError("configured Google Drive root ID is not a folder")
            return folder
        root_name = (getattr(self.settings, "google_drive_root_folder_name", "") or "").strip()
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

    @staticmethod
    def _patient_id_from_drive_folder_id(folder_id: str) -> str:
        digest = hashlib.sha256(folder_id.encode("utf-8")).hexdigest()[:12].upper()
        return f"P_{digest}"

    @staticmethod
    def _patient_needs_update(existing: PatientProfile, incoming: PatientProfile) -> bool:
        return any(
            getattr(existing, field) != getattr(incoming, field)
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

    def _ensure_drive_sync_state(self, scope_id: str) -> DriveSyncState:
        state = self.repository.get_drive_sync_state(scope_id)
        if state is not None:
            return state
        state = DriveSyncState(scope_id=scope_id, sync_status="READY", updated_at=now_kst_iso())
        self.repository.upsert_drive_sync_state(state)
        return state

    def _ensure_folder_index_bootstrap(self) -> int:
        count = 0
        for patient in self.repository.list_active_patients():
            if self._upsert_folder_index_for_patient(patient):
                count += 1
        return count

    def _upsert_folder_index_for_patient(self, patient: Any) -> bool:
        existing = self.repository.get_drive_folder_index(patient.drive_folder_id)
        now = now_kst_iso()
        if (
            existing is not None
            and existing.patient_id == patient.patient_id
            and existing.status == "active"
        ):
            return False
        self.repository.upsert_drive_folder_index(
            DriveFolderIndexEntry(
                drive_folder_id=patient.drive_folder_id,
                patient_id=patient.patient_id,
                status="active",
                created_at=existing.created_at if existing is not None else now,
                updated_at=now,
            )
        )
        return True

    def _append_wiki_log(
        self,
        patient_id: str,
        *,
        event_type: str,
        title: str,
        source_id: str = "",
        page_ids: list[str] | None = None,
        index_updated: bool = False,
        status: str = "success",
        message: str = "",
        failure_code: str = "",
    ) -> None:
        payload: dict[str, Any] = {
            "event_type": event_type,
            "title": title,
            "page_ids": page_ids or [],
            "index_updated": index_updated,
            "status": status,
            "message": message,
            "created_at": now_kst_iso(),
        }
        if source_id:
            payload["source_id"] = source_id
        if failure_code:
            payload["failure_code"] = failure_code
        self.repository.append_wiki_log(patient_id, payload)

    def _empty_counters(self) -> dict[str, int]:
        return {key: 0 for key in COUNTER_KEYS}

    def _counter_for(self, key: str) -> dict[str, int]:
        counters = self._empty_counters()
        if key in counters:
            counters[key] += 1
        else:
            counters["failed"] += 1
        return counters

    def _merge_counters(self, target: dict[str, int], source: dict[str, int]) -> None:
        for key in COUNTER_KEYS:
            target[key] = target.get(key, 0) + source.get(key, 0)

    def _mark_index_dirty(self, patient_id: str) -> None:
        if patient_id:
            self.index_dirty_patient_ids.add(patient_id)

    def _compile_dirty_indexes(self) -> None:
        dirty_patient_ids = sorted(self.index_dirty_patient_ids)
        self.index_dirty_patient_ids.clear()
        for patient_id in dirty_patient_ids:
            self.wiki_service.compile_wiki_index(patient_id=patient_id)

    def _result_from_counters(
        self,
        *,
        ok: bool,
        counters: dict[str, int],
        message: str,
        extra_failed: int = 0,
    ) -> DriveSyncResult:
        return DriveSyncResult(
            ok=ok,
            processed_count=sum(counters.get(key, 0) for key in COUNTER_KEYS) + extra_failed,
            created_count=counters.get("created", 0),
            updated_count=counters.get("updated", 0),
            deleted_count=counters.get("deleted", 0),
            skipped_count=counters.get("skipped", 0),
            deferred_count=counters.get("deferred", 0),
            failed_count=counters.get("failed", 0) + extra_failed,
            message=message,
        )

    def _remaining_wiki_generation_budget(self) -> int:
        return max(0, self.settings.max_wiki_page_generations_per_run - self.wiki_generations_this_run)

    def _new_source_id(self, patient_id: str) -> str:
        return f"SRC_{patient_id}_{uuid.uuid4().hex[:12].upper()}"

    def _source_changed(self, *, old_source: MedicalSource, new_source: MedicalSource) -> bool:
        return (
            old_source.source_ref.drive_modified_at != new_source.source_ref.drive_modified_at
            or old_source.source_ref.file_size_bytes != new_source.source_ref.file_size_bytes
            or old_source.source_ref.file_hash != new_source.source_ref.file_hash
            or old_source.source_ref.mime_type != new_source.source_ref.mime_type
            or old_source.source_ref.original_filename != new_source.source_ref.original_filename
        )

    def _can_generate_wiki_page(self) -> bool:
        return self.wiki_generations_this_run < self.settings.max_wiki_page_generations_per_run

    def _acquire_sync_lock(
        self,
        state: DriveSyncState,
        *,
        owner_prefix: str,
        lease_minutes: int,
    ) -> DriveSyncState | None:
        return self.repository.try_acquire_drive_sync_lock(
            state.scope_id,
            lock_owner=f"{owner_prefix}-{uuid.uuid4().hex[:12]}",
            lease_expires_at=(datetime.now(tz=KST) + timedelta(minutes=lease_minutes)).isoformat(),
            now=now_kst_iso(),
        )

    def _int_or_none(self, value: object) -> int | None:
        try:
            return int(value) if value not in {None, ""} else None
        except Exception:
            return None
