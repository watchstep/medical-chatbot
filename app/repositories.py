from __future__ import annotations

import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

try:
    from google.cloud import firestore
except ImportError:  # pragma: no cover - optional runtime dependency
    firestore = None  # type: ignore[assignment]

from app.config import Settings
from app.schemas import (
    ChatLog,
    ChatSession,
    DriveFileIndexEntry,
    DriveFolderIndexEntry,
    DriveSyncState,
    KakaoCallbackJob,
    KakaoUserMapping,
    GeminiFilePrewarmJob,
    MedicalSource,
    MedicalSourceRuntime,
    MedicalWikiIndex,
    MedicalWikiPage,
    PatientProfile,
)
from app.services.patient_identity import (
    make_patient_birth_key,
    make_patient_name_key,
    normalize_patient_birth,
    normalize_patient_name,
)


class MedicalRepositoryError(Exception):
    pass


def _lease_expired(value: str, now: str) -> bool:
    try:
        return datetime.fromisoformat(value) <= datetime.fromisoformat(now)
    except ValueError:
        return True


def _patient_with_identity_keys(patient: PatientProfile) -> PatientProfile:
    """Return a PatientProfile with normalized display fields and lookup keys."""
    name = normalize_patient_name(patient.name)
    birth = normalize_patient_birth(patient.birth)
    drive_folder_name = normalize_patient_name(patient.drive_folder_name)
    return patient.model_copy(
        update={
            "name": name,
            "birth": birth,
            "name_key": make_patient_name_key(name),
            "birth_key": make_patient_birth_key(birth),
            "drive_folder_name": drive_folder_name,
        }
    )


class MedicalRepository:
    def get_patient(self, patient_id: str) -> PatientProfile | None:
        raise NotImplementedError

    def find_active_patient_by_identity(self, *, name: str, birth: str) -> PatientProfile | None:
        raise NotImplementedError

    def list_active_patients(self) -> list[PatientProfile]:
        raise NotImplementedError

    def upsert_patient(self, patient: PatientProfile) -> None:
        raise NotImplementedError

    def get_kakao_mapping(self, kakao_user_id_hash: str) -> KakaoUserMapping | None:
        raise NotImplementedError

    def upsert_kakao_mapping(self, mapping: KakaoUserMapping) -> None:
        raise NotImplementedError

    def delete_kakao_mapping(self, kakao_user_id_hash: str) -> None:
        raise NotImplementedError

    def get_drive_sync_state(self, scope_id: str) -> DriveSyncState | None:
        raise NotImplementedError

    def upsert_drive_sync_state(self, state: DriveSyncState) -> None:
        raise NotImplementedError

    def try_acquire_drive_sync_lock(
        self,
        scope_id: str,
        *,
        lock_owner: str,
        lease_expires_at: str,
        now: str,
    ) -> DriveSyncState | None:
        raise NotImplementedError

    def get_drive_folder_index(self, drive_folder_id: str) -> DriveFolderIndexEntry | None:
        raise NotImplementedError

    def upsert_drive_folder_index(self, entry: DriveFolderIndexEntry) -> None:
        raise NotImplementedError

    def get_drive_file_index(self, drive_file_id: str) -> DriveFileIndexEntry | None:
        raise NotImplementedError

    def upsert_drive_file_index(self, entry: DriveFileIndexEntry) -> None:
        raise NotImplementedError

    def get_medical_source(self, patient_id: str, source_id: str) -> MedicalSource | None:
        raise NotImplementedError

    def upsert_medical_source(self, source: MedicalSource) -> None:
        raise NotImplementedError

    def list_medical_sources(self, patient_id: str) -> list[MedicalSource]:
        raise NotImplementedError

    def list_source_runtimes(self, patient_id: str) -> list[MedicalSourceRuntime]:
        raise NotImplementedError

    def list_source_runtimes_with_gemini_files(self, *, limit: int | None = None) -> list[MedicalSourceRuntime]:
        raise NotImplementedError

    def list_drive_file_index_entries_for_patient(self, patient_id: str) -> list[DriveFileIndexEntry]:
        raise NotImplementedError

    def get_source_runtime(self, patient_id: str, source_id: str) -> MedicalSourceRuntime | None:
        raise NotImplementedError

    def upsert_source_runtime(self, runtime: MedicalSourceRuntime) -> None:
        raise NotImplementedError

    def upsert_source_bundle(
        self,
        *,
        source: MedicalSource,
        runtime: MedicalSourceRuntime,
        drive_file_index: DriveFileIndexEntry,
    ) -> None:
        raise NotImplementedError

    def upsert_source_status_bundle(
        self,
        *,
        source: MedicalSource | None,
        runtime: MedicalSourceRuntime | None,
        drive_file_index: DriveFileIndexEntry | None,
    ) -> None:
        raise NotImplementedError

    def try_acquire_source_runtime_lock(
        self,
        patient_id: str,
        source_id: str,
        *,
        lock_owner: str,
        lease_expires_at: str,
        now: str,
    ) -> MedicalSourceRuntime | None:
        raise NotImplementedError

    def release_source_runtime_lock(
        self,
        patient_id: str,
        source_id: str,
        *,
        lock_owner: str,
        now: str,
        sync_status: str | None = None,
    ) -> MedicalSourceRuntime | None:
        raise NotImplementedError

    def get_wiki_page(self, patient_id: str, page_id: str) -> MedicalWikiPage | None:
        raise NotImplementedError

    def get_wiki_page_by_source(self, patient_id: str, source_id: str) -> MedicalWikiPage | None:
        page = self.get_wiki_page(patient_id, f"PAGE_{source_id}")
        if page is not None:
            return page
        return next(
            (item for item in self.list_wiki_pages(patient_id) if item.source_id == source_id),
            None,
        )

    def upsert_wiki_page(self, page: MedicalWikiPage) -> None:
        raise NotImplementedError

    def list_wiki_pages(self, patient_id: str) -> list[MedicalWikiPage]:
        raise NotImplementedError

    def get_wiki_index(self, patient_id: str) -> MedicalWikiIndex | None:
        raise NotImplementedError

    def upsert_wiki_index(self, index: MedicalWikiIndex) -> None:
        raise NotImplementedError

    def append_wiki_log(self, patient_id: str, payload: dict[str, Any]) -> str:
        raise NotImplementedError

    def upsert_wiki_index_with_log(
        self,
        *,
        index: MedicalWikiIndex,
        log_payload: dict[str, Any],
    ) -> str:
        raise NotImplementedError

    def get_chat_session(self, patient_id: str, kakao_user_id_hash: str) -> ChatSession | None:
        raise NotImplementedError

    def upsert_chat_session(self, session: ChatSession) -> None:
        raise NotImplementedError

    def delete_chat_session(self, patient_id: str, kakao_user_id_hash: str) -> None:
        raise NotImplementedError

    def create_chat_log(self, log: ChatLog) -> str:
        raise NotImplementedError

    def get_chat_log(self, patient_id: str, log_id: str) -> ChatLog | None:
        raise NotImplementedError

    def update_chat_log_job(self, patient_id: str, log_id: str, job_id: str) -> None:
        raise NotImplementedError

    def list_chat_logs_for_patient(self, patient_id: str, *, start_at: str, end_at: str) -> list[ChatLog]:
        raise NotImplementedError

    def list_chat_logs_for_dashboard(
        self,
        *,
        start_at: str = "",
        end_at: str = "",
        limit: int,
        offset: int = 0,
    ) -> list[tuple[PatientProfile, ChatLog]]:
        raise NotImplementedError

    def create_callback_job(self, job: KakaoCallbackJob) -> str:
        raise NotImplementedError

    def get_callback_job(self, job_id: str) -> KakaoCallbackJob | None:
        raise NotImplementedError

    def get_callback_job_by_idempotency_key(self, idempotency_key: str) -> KakaoCallbackJob | None:
        raise NotImplementedError

    def try_acquire_callback_job(
        self,
        job_id: str,
        *,
        lock_owner: str,
        lease_expires_at: str,
        now: str,
    ) -> KakaoCallbackJob | None:
        raise NotImplementedError

    def list_runnable_callback_jobs(self, *, now: str, limit: int) -> list[KakaoCallbackJob]:
        raise NotImplementedError

    def list_expired_callback_jobs(self, *, now: str, limit: int) -> list[KakaoCallbackJob]:
        raise NotImplementedError

    def count_callback_jobs_by_status(self) -> dict[str, int]:
        raise NotImplementedError

    def list_recent_failed_callback_jobs(self, *, limit: int) -> list[KakaoCallbackJob]:
        raise NotImplementedError

    def upsert_callback_job(self, job: KakaoCallbackJob) -> None:
        raise NotImplementedError

    def create_prewarm_job(self, job: GeminiFilePrewarmJob) -> str:
        raise NotImplementedError

    def get_prewarm_job(self, job_id: str) -> GeminiFilePrewarmJob | None:
        raise NotImplementedError

    def get_prewarm_job_by_idempotency_key(self, idempotency_key: str) -> GeminiFilePrewarmJob | None:
        raise NotImplementedError

    def try_acquire_prewarm_job(
        self,
        job_id: str,
        *,
        lock_owner: str,
        lease_expires_at: str,
        now: str,
    ) -> GeminiFilePrewarmJob | None:
        raise NotImplementedError

    def upsert_prewarm_job(self, job: GeminiFilePrewarmJob) -> None:
        raise NotImplementedError


@dataclass
class InMemoryMedicalRepository(MedicalRepository):
    patients: dict[str, PatientProfile] = field(default_factory=dict)
    kakao_user_map: dict[str, KakaoUserMapping] = field(default_factory=dict)
    drive_sync_state: dict[str, DriveSyncState] = field(default_factory=dict)
    drive_folder_index: dict[str, DriveFolderIndexEntry] = field(default_factory=dict)
    drive_file_index: dict[str, DriveFileIndexEntry] = field(default_factory=dict)
    medical_sources: dict[tuple[str, str], MedicalSource] = field(default_factory=dict)
    source_runtime: dict[tuple[str, str], MedicalSourceRuntime] = field(default_factory=dict)
    wiki_pages: dict[tuple[str, str], MedicalWikiPage] = field(default_factory=dict)
    wiki_index: dict[str, MedicalWikiIndex] = field(default_factory=dict)
    wiki_logs: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    chat_sessions: dict[tuple[str, str], ChatSession] = field(default_factory=dict)
    chat_logs: dict[tuple[str, str], ChatLog] = field(default_factory=dict)
    callback_jobs: dict[str, KakaoCallbackJob] = field(default_factory=dict)
    prewarm_jobs: dict[str, GeminiFilePrewarmJob] = field(default_factory=dict)

    def _copy(self, value: Any) -> Any:
        return deepcopy(value)

    def get_patient(self, patient_id: str) -> PatientProfile | None:
        return self._copy(self.patients.get(patient_id))

    def find_active_patient_by_identity(self, *, name: str, birth: str) -> PatientProfile | None:
        name_key = make_patient_name_key(name)
        birth_key = make_patient_birth_key(birth)
        return self._copy(
            next(
                (
                    patient
                    for patient in self.patients.values()
                    if patient.status == "active"
                    and (patient.name_key or make_patient_name_key(patient.name)) == name_key
                    and (patient.birth_key or make_patient_birth_key(patient.birth)) == birth_key
                ),
                None,
            )
        )

    def list_active_patients(self) -> list[PatientProfile]:
        return [self._copy(item) for item in self.patients.values() if item.status == "active"]

    def upsert_patient(self, patient: PatientProfile) -> None:
        self.patients[patient.patient_id] = self._copy(_patient_with_identity_keys(patient))

    def get_kakao_mapping(self, kakao_user_id_hash: str) -> KakaoUserMapping | None:
        return self._copy(self.kakao_user_map.get(kakao_user_id_hash))

    def upsert_kakao_mapping(self, mapping: KakaoUserMapping) -> None:
        self.kakao_user_map[mapping.kakao_user_id_hash] = self._copy(mapping)

    def delete_kakao_mapping(self, kakao_user_id_hash: str) -> None:
        self.kakao_user_map.pop(kakao_user_id_hash, None)

    def get_drive_sync_state(self, scope_id: str) -> DriveSyncState | None:
        return self._copy(self.drive_sync_state.get(scope_id))

    def upsert_drive_sync_state(self, state: DriveSyncState) -> None:
        self.drive_sync_state[state.scope_id] = self._copy(state)

    def try_acquire_drive_sync_lock(
        self,
        scope_id: str,
        *,
        lock_owner: str,
        lease_expires_at: str,
        now: str,
    ) -> DriveSyncState | None:
        state = self.drive_sync_state.get(scope_id)
        if state is None:
            return None
        if state.lock_owner and state.lease_expires_at and not _lease_expired(state.lease_expires_at, now):
            return None
        locked = state.model_copy(
            update={
                "sync_status": "PROCESSING",
                "lock_owner": lock_owner,
                "lease_expires_at": lease_expires_at,
                "updated_at": now,
            }
        )
        self.drive_sync_state[scope_id] = self._copy(locked)
        return self._copy(locked)

    def get_drive_folder_index(self, drive_folder_id: str) -> DriveFolderIndexEntry | None:
        return self._copy(self.drive_folder_index.get(drive_folder_id))

    def upsert_drive_folder_index(self, entry: DriveFolderIndexEntry) -> None:
        self.drive_folder_index[entry.drive_folder_id] = self._copy(entry)

    def get_drive_file_index(self, drive_file_id: str) -> DriveFileIndexEntry | None:
        return self._copy(self.drive_file_index.get(drive_file_id))

    def upsert_drive_file_index(self, entry: DriveFileIndexEntry) -> None:
        self.drive_file_index[entry.drive_file_id] = self._copy(entry)

    def get_medical_source(self, patient_id: str, source_id: str) -> MedicalSource | None:
        return self._copy(self.medical_sources.get((patient_id, source_id)))

    def upsert_medical_source(self, source: MedicalSource) -> None:
        self.medical_sources[(source.patient_id, source.source_id)] = self._copy(source)

    def list_medical_sources(self, patient_id: str) -> list[MedicalSource]:
        return [
            self._copy(source)
            for (stored_patient_id, _), source in self.medical_sources.items()
            if stored_patient_id == patient_id
        ]

    def list_source_runtimes(self, patient_id: str) -> list[MedicalSourceRuntime]:
        return [
            self._copy(runtime)
            for (stored_patient_id, _), runtime in self.source_runtime.items()
            if stored_patient_id == patient_id
        ]

    def list_source_runtimes_with_gemini_files(self, *, limit: int | None = None) -> list[MedicalSourceRuntime]:
        items = [
            self._copy(runtime)
            for runtime in self.source_runtime.values()
            if runtime.gemini_file.file_name
        ]
        return items[:limit] if limit is not None else items

    def list_drive_file_index_entries_for_patient(self, patient_id: str) -> list[DriveFileIndexEntry]:
        return [
            self._copy(entry)
            for entry in self.drive_file_index.values()
            if entry.patient_id == patient_id
        ]

    def get_source_runtime(self, patient_id: str, source_id: str) -> MedicalSourceRuntime | None:
        return self._copy(self.source_runtime.get((patient_id, source_id)))

    def upsert_source_runtime(self, runtime: MedicalSourceRuntime) -> None:
        self.source_runtime[(runtime.patient_id, runtime.source_id)] = self._copy(runtime)

    def upsert_source_bundle(
        self,
        *,
        source: MedicalSource,
        runtime: MedicalSourceRuntime,
        drive_file_index: DriveFileIndexEntry,
    ) -> None:
        self.medical_sources[(source.patient_id, source.source_id)] = self._copy(source)
        self.source_runtime[(runtime.patient_id, runtime.source_id)] = self._copy(runtime)
        self.drive_file_index[drive_file_index.drive_file_id] = self._copy(drive_file_index)

    def upsert_source_status_bundle(
        self,
        *,
        source: MedicalSource | None,
        runtime: MedicalSourceRuntime | None,
        drive_file_index: DriveFileIndexEntry | None,
    ) -> None:
        if source is not None:
            self.medical_sources[(source.patient_id, source.source_id)] = self._copy(source)
        if runtime is not None:
            self.source_runtime[(runtime.patient_id, runtime.source_id)] = self._copy(runtime)
        if drive_file_index is not None:
            self.drive_file_index[drive_file_index.drive_file_id] = self._copy(drive_file_index)

    def try_acquire_source_runtime_lock(
        self,
        patient_id: str,
        source_id: str,
        *,
        lock_owner: str,
        lease_expires_at: str,
        now: str,
    ) -> MedicalSourceRuntime | None:
        runtime = self.source_runtime.get((patient_id, source_id))
        if runtime is None:
            return None
        if runtime.sync.lock_owner and runtime.sync.lease_expires_at and not _lease_expired(runtime.sync.lease_expires_at, now):
            return None
        locked = runtime.model_copy(
            update={
                "sync": runtime.sync.model_copy(
                    update={
                        "status": "UPLOADING",
                        "lock_owner": lock_owner,
                        "lease_expires_at": lease_expires_at,
                    }
                ),
                "updated_at": now,
            }
        )
        self.source_runtime[(patient_id, source_id)] = self._copy(locked)
        return self._copy(locked)

    def release_source_runtime_lock(
        self,
        patient_id: str,
        source_id: str,
        *,
        lock_owner: str,
        now: str,
        sync_status: str | None = None,
    ) -> MedicalSourceRuntime | None:
        runtime = self.source_runtime.get((patient_id, source_id))
        if runtime is None:
            return None
        if runtime.sync.lock_owner and runtime.sync.lock_owner != lock_owner:
            return None
        updates = {"lock_owner": None, "lease_expires_at": None}
        if sync_status is not None:
            updates["status"] = sync_status
        released = runtime.model_copy(
            update={
                "sync": runtime.sync.model_copy(update=updates),
                "updated_at": now,
            }
        )
        self.source_runtime[(patient_id, source_id)] = self._copy(released)
        return self._copy(released)

    def get_wiki_page(self, patient_id: str, page_id: str) -> MedicalWikiPage | None:
        return self._copy(self.wiki_pages.get((patient_id, page_id)))

    def upsert_wiki_page(self, page: MedicalWikiPage) -> None:
        self.wiki_pages[(page.patient_id, page.page_id)] = self._copy(page)

    def list_wiki_pages(self, patient_id: str) -> list[MedicalWikiPage]:
        return [
            self._copy(page)
            for (stored_patient_id, _), page in self.wiki_pages.items()
            if stored_patient_id == patient_id
        ]

    def get_wiki_index(self, patient_id: str) -> MedicalWikiIndex | None:
        return self._copy(self.wiki_index.get(patient_id))

    def upsert_wiki_index(self, index: MedicalWikiIndex) -> None:
        self.wiki_index[index.patient_id] = self._copy(index)

    def append_wiki_log(self, patient_id: str, payload: dict[str, Any]) -> str:
        log_id = str(payload.get("log_id") or f"LOG_{uuid.uuid4().hex[:12].upper()}")
        self.wiki_logs[(patient_id, log_id)] = self._copy({**payload, "log_id": log_id})
        return log_id

    def upsert_wiki_index_with_log(
        self,
        *,
        index: MedicalWikiIndex,
        log_payload: dict[str, Any],
    ) -> str:
        self.wiki_index[index.patient_id] = self._copy(index)
        log_id = str(log_payload.get("log_id") or f"LOG_{uuid.uuid4().hex[:12].upper()}")
        self.wiki_logs[(index.patient_id, log_id)] = self._copy({**log_payload, "log_id": log_id})
        return log_id

    def get_chat_session(self, patient_id: str, kakao_user_id_hash: str) -> ChatSession | None:
        return self._copy(self.chat_sessions.get((patient_id, kakao_user_id_hash)))

    def upsert_chat_session(self, session: ChatSession) -> None:
        self.chat_sessions[(session.patient_id, session.kakao_user_id_hash)] = self._copy(session)

    def delete_chat_session(self, patient_id: str, kakao_user_id_hash: str) -> None:
        self.chat_sessions.pop((patient_id, kakao_user_id_hash), None)

    def create_chat_log(self, log: ChatLog) -> str:
        self.chat_logs[(log.patient_id, log.log_id)] = self._copy(log)
        return log.log_id

    def get_chat_log(self, patient_id: str, log_id: str) -> ChatLog | None:
        return self._copy(self.chat_logs.get((patient_id, log_id)))

    def update_chat_log_job(self, patient_id: str, log_id: str, job_id: str) -> None:
        log = self.chat_logs.get((patient_id, log_id))
        if log is not None:
            self.chat_logs[(patient_id, log_id)] = log.model_copy(update={"job_id": job_id})

    def list_chat_logs_for_patient(self, patient_id: str, *, start_at: str, end_at: str) -> list[ChatLog]:
        logs = [
            self._copy(log)
            for (stored_patient_id, _), log in self.chat_logs.items()
            if stored_patient_id == patient_id and start_at <= log.created_at < end_at
        ]
        return sorted(logs, key=lambda item: (item.created_at, item.log_id))

    def list_chat_logs_for_dashboard(
        self,
        *,
        start_at: str = "",
        end_at: str = "",
        limit: int,
        offset: int = 0,
    ) -> list[tuple[PatientProfile, ChatLog]]:
        patients = {patient.patient_id: patient for patient in self.list_active_patients()}
        items: list[tuple[PatientProfile, ChatLog]] = []
        for (patient_id, _), log in self.chat_logs.items():
            patient = patients.get(patient_id)
            in_range = True
            if start_at:
                in_range = in_range and log.created_at >= start_at
            if end_at:
                in_range = in_range and log.created_at < end_at
            if patient is not None and in_range:
                items.append((self._copy(patient), self._copy(log)))
        items.sort(key=lambda item: (item[1].created_at, item[1].log_id), reverse=True)
        safe_offset = max(0, offset)
        return items[safe_offset:safe_offset + limit]

    def create_callback_job(self, job: KakaoCallbackJob) -> str:
        self.callback_jobs[job.job_id] = self._copy(job)
        return job.job_id

    def get_callback_job(self, job_id: str) -> KakaoCallbackJob | None:
        return self._copy(self.callback_jobs.get(job_id))

    def get_callback_job_by_idempotency_key(self, idempotency_key: str) -> KakaoCallbackJob | None:
        if not idempotency_key:
            return None
        fallback: KakaoCallbackJob | None = None
        for job in self.callback_jobs.values():
            if job.idempotency_key == idempotency_key:
                if job.status in {"PENDING", "PROCESSING"}:
                    return self._copy(job)
                if fallback is None:
                    fallback = self._copy(job)
        return fallback

    def try_acquire_callback_job(
        self,
        job_id: str,
        *,
        lock_owner: str,
        lease_expires_at: str,
        now: str,
    ) -> KakaoCallbackJob | None:
        job = self.callback_jobs.get(job_id)
        if job is None:
            return None
        if job.status == "CALLBACK_SENT" or job.status == "EXPIRED":
            return None
        if not job.runnable:
            return None
        if job.next_run_at and not _lease_expired(job.next_run_at, now):
            return None
        if job.expires_at and _lease_expired(job.expires_at, now):
            return None
        if job.retry_count >= job.max_attempts:
            return None
        if job.lock_owner and job.lease_expires_at and not _lease_expired(job.lease_expires_at, now):
            return None
        locked = job.model_copy(
            update={
                "status": "PROCESSING",
                "runnable": False,
                "lock_owner": lock_owner,
                "lease_expires_at": lease_expires_at,
                "retry_count": job.retry_count + 1,
                "processing_started_at": now,
                "updated_at": now,
            }
        )
        self.callback_jobs[job_id] = self._copy(locked)
        return self._copy(locked)

    def list_runnable_callback_jobs(self, *, now: str, limit: int) -> list[KakaoCallbackJob]:
        result: list[KakaoCallbackJob] = []
        for job in sorted(self.callback_jobs.values(), key=lambda item: item.next_run_at or item.created_at or item.job_id):
            if not job.runnable:
                continue
            if job.status not in {"PENDING", "FAILED"}:
                continue
            if job.next_run_at and not _lease_expired(job.next_run_at, now):
                continue
            if job.expires_at and _lease_expired(job.expires_at, now):
                continue
            if job.retry_count >= job.max_attempts:
                continue
            if job.lock_owner and job.lease_expires_at and not _lease_expired(job.lease_expires_at, now):
                continue
            result.append(self._copy(job))
            if len(result) >= limit:
                break
        return result

    def list_expired_callback_jobs(self, *, now: str, limit: int) -> list[KakaoCallbackJob]:
        result: list[KakaoCallbackJob] = []
        for job in sorted(self.callback_jobs.values(), key=lambda item: item.created_at or item.job_id):
            if job.status in {"CALLBACK_SENT", "EXPIRED"}:
                continue
            if job.expires_at and _lease_expired(job.expires_at, now):
                result.append(self._copy(job))
                if len(result) >= limit:
                    break
        return result

    def count_callback_jobs_by_status(self) -> dict[str, int]:
        counts = {
            "PENDING": 0,
            "PROCESSING": 0,
            "FAILED": 0,
            "EXPIRED": 0,
            "CALLBACK_SENT": 0,
        }
        for job in self.callback_jobs.values():
            if job.status in counts:
                counts[job.status] += 1
        return counts

    def list_recent_failed_callback_jobs(self, *, limit: int) -> list[KakaoCallbackJob]:
        jobs = [self._copy(job) for job in self.callback_jobs.values() if job.status == "FAILED"]
        jobs.sort(key=lambda item: (item.updated_at or item.created_at, item.job_id), reverse=True)
        return jobs[:limit]

    def upsert_callback_job(self, job: KakaoCallbackJob) -> None:
        self.callback_jobs[job.job_id] = self._copy(job)

    def create_prewarm_job(self, job: GeminiFilePrewarmJob) -> str:
        self.prewarm_jobs[job.job_id] = self._copy(job)
        return job.job_id

    def get_prewarm_job(self, job_id: str) -> GeminiFilePrewarmJob | None:
        return self._copy(self.prewarm_jobs.get(job_id))

    def get_prewarm_job_by_idempotency_key(self, idempotency_key: str) -> GeminiFilePrewarmJob | None:
        if not idempotency_key:
            return None
        fallback: GeminiFilePrewarmJob | None = None
        for job in self.prewarm_jobs.values():
            if job.idempotency_key != idempotency_key:
                continue
            if job.status in {"PENDING", "PROCESSING", "DONE"}:
                return self._copy(job)
            if fallback is None:
                fallback = self._copy(job)
        return fallback

    def try_acquire_prewarm_job(
        self,
        job_id: str,
        *,
        lock_owner: str,
        lease_expires_at: str,
        now: str,
    ) -> GeminiFilePrewarmJob | None:
        job = self.prewarm_jobs.get(job_id)
        if job is None:
            return None
        if job.status in {"DONE", "SKIPPED", "EXPIRED"}:
            return None
        if not job.runnable:
            return None
        if job.next_run_at and not _lease_expired(job.next_run_at, now):
            return None
        if job.expires_at and _lease_expired(job.expires_at, now):
            return None
        if job.retry_count >= job.max_attempts:
            return None
        if job.lock_owner and job.lease_expires_at and not _lease_expired(job.lease_expires_at, now):
            return None
        locked = job.model_copy(
            update={
                "status": "PROCESSING",
                "runnable": False,
                "lock_owner": lock_owner,
                "lease_expires_at": lease_expires_at,
                "retry_count": job.retry_count + 1,
                "started_at": job.started_at or now,
                "updated_at": now,
            }
        )
        self.prewarm_jobs[job_id] = self._copy(locked)
        return self._copy(locked)

    def upsert_prewarm_job(self, job: GeminiFilePrewarmJob) -> None:
        self.prewarm_jobs[job.job_id] = self._copy(job)


class FirestoreMedicalRepository(MedicalRepository):
    def __init__(
        self,
        *,
        project_id: str | None = None,
        database_id: str | None = None,
        client: Any | None = None,
    ):
        if firestore is None:
            raise MedicalRepositoryError(
                "google-cloud-firestore 패키지가 필요합니다. requirements.txt를 확인하세요."
            )
        self.client = client or firestore.Client(
            project=project_id,
            database=database_id or "(default)",
        )

    def _doc(self, *path: str) -> Any:
        ref = self.client.collection(path[0])
        for index in range(1, len(path), 2):
            ref = ref.document(path[index])
            if index + 1 < len(path):
                ref = ref.collection(path[index + 1])
        return ref

    def _get_model(self, model: type[Any], *path: str) -> Any | None:
        snapshot = self._doc(*path).get()
        if not snapshot.exists:
            return None
        return model.model_validate(snapshot.to_dict() or {})

    def _replace_model(self, value: Any, *path: str) -> None:
        self._doc(*path).set(value.model_dump(exclude_none=True), merge=False)

    def _merge_model(self, value: Any, *path: str) -> None:
        self._doc(*path).set(value.model_dump(exclude_none=True), merge=True)

    def _update_fields(self, updates: dict[str, Any], *path: str) -> None:
        self._doc(*path).update(updates)

    def _set_model(self, value: Any, *path: str) -> None:
        # Default upsert policy is replace. Use _merge_model or _update_fields for patches/locks.
        self._replace_model(value, *path)

    def _stream_models(self, model: type[Any], *collection_path: str) -> list[Any]:
        collection = self._doc(*collection_path)
        return [model.model_validate(snapshot.to_dict() or {}) for snapshot in collection.stream()]

    def get_patient(self, patient_id: str) -> PatientProfile | None:
        return self._get_model(PatientProfile, "patients", patient_id)

    def find_active_patient_by_identity(self, *, name: str, birth: str) -> PatientProfile | None:
        name_key = make_patient_name_key(name)
        birth_key = make_patient_birth_key(birth)

        query = (
            self.client.collection("patients")
            .where("name_key", "==", name_key)
            .where("birth_key", "==", birth_key)
            .where("status", "==", "active")
            .limit(1)
        )
        for snapshot in query.stream():
            return PatientProfile.model_validate(snapshot.to_dict() or {})

        # Backward-compatible fallback for patient docs created before
        # name_key/birth_key were introduced. Remove after migration is complete.
        legacy_query = (
            self.client.collection("patients")
            .where("birth", "==", birth_key)
            .where("status", "==", "active")
            .limit(10)
        )
        for snapshot in legacy_query.stream():
            patient = PatientProfile.model_validate(snapshot.to_dict() or {})
            if make_patient_name_key(patient.name) == name_key:
                return patient

        return None

    def list_active_patients(self) -> list[PatientProfile]:
        query = self.client.collection("patients").where("status", "==", "active")
        return [PatientProfile.model_validate(snapshot.to_dict() or {}) for snapshot in query.stream()]

    def upsert_patient(self, patient: PatientProfile) -> None:
        self._set_model(_patient_with_identity_keys(patient), "patients", patient.patient_id)

    def get_kakao_mapping(self, kakao_user_id_hash: str) -> KakaoUserMapping | None:
        return self._get_model(KakaoUserMapping, "kakao_user_map", kakao_user_id_hash)

    def upsert_kakao_mapping(self, mapping: KakaoUserMapping) -> None:
        self._set_model(mapping, "kakao_user_map", mapping.kakao_user_id_hash)

    def delete_kakao_mapping(self, kakao_user_id_hash: str) -> None:
        self._doc("kakao_user_map", kakao_user_id_hash).delete()

    def get_drive_sync_state(self, scope_id: str) -> DriveSyncState | None:
        return self._get_model(DriveSyncState, "drive_sync_state", scope_id)

    def upsert_drive_sync_state(self, state: DriveSyncState) -> None:
        self._set_model(state, "drive_sync_state", state.scope_id)

    def try_acquire_drive_sync_lock(
        self,
        scope_id: str,
        *,
        lock_owner: str,
        lease_expires_at: str,
        now: str,
    ) -> DriveSyncState | None:
        doc_ref = self._doc("drive_sync_state", scope_id)
        transaction = self.client.transaction()

        @firestore.transactional
        def acquire_in_transaction(transaction: Any) -> DriveSyncState | None:
            snapshot = doc_ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            state = DriveSyncState.model_validate(snapshot.to_dict() or {})
            if state.lock_owner and state.lease_expires_at and not _lease_expired(state.lease_expires_at, now):
                return None
            locked = state.model_copy(
                update={
                    "sync_status": "PROCESSING",
                    "lock_owner": lock_owner,
                    "lease_expires_at": lease_expires_at,
                    "updated_at": now,
                }
            )
            transaction.set(doc_ref, locked.model_dump(exclude_none=True), merge=True)
            return locked

        return acquire_in_transaction(transaction)

    def get_drive_folder_index(self, drive_folder_id: str) -> DriveFolderIndexEntry | None:
        return self._get_model(DriveFolderIndexEntry, "drive_folder_index", drive_folder_id)

    def upsert_drive_folder_index(self, entry: DriveFolderIndexEntry) -> None:
        self._set_model(entry, "drive_folder_index", entry.drive_folder_id)

    def get_drive_file_index(self, drive_file_id: str) -> DriveFileIndexEntry | None:
        return self._get_model(DriveFileIndexEntry, "drive_file_index", drive_file_id)

    def upsert_drive_file_index(self, entry: DriveFileIndexEntry) -> None:
        self._set_model(entry, "drive_file_index", entry.drive_file_id)

    def get_medical_source(self, patient_id: str, source_id: str) -> MedicalSource | None:
        return self._get_model(MedicalSource, "patients", patient_id, "medical_sources", source_id)

    def upsert_medical_source(self, source: MedicalSource) -> None:
        self._set_model(source, "patients", source.patient_id, "medical_sources", source.source_id)

    def list_medical_sources(self, patient_id: str) -> list[MedicalSource]:
        return self._stream_models(MedicalSource, "patients", patient_id, "medical_sources")

    def list_source_runtimes(self, patient_id: str) -> list[MedicalSourceRuntime]:
        return self._stream_models(
            MedicalSourceRuntime,
            "patients",
            patient_id,
            "medical_source_runtime",
        )

    def list_source_runtimes_with_gemini_files(self, *, limit: int | None = None) -> list[MedicalSourceRuntime]:
        query = self.client.collection_group("medical_source_runtime")
        if limit is not None:
            query = query.limit(limit)
        return [
            MedicalSourceRuntime.model_validate(snapshot.to_dict() or {})
            for snapshot in query.stream()
            if (snapshot.to_dict() or {}).get("gemini_file", {}).get("file_name")
        ]

    def list_drive_file_index_entries_for_patient(self, patient_id: str) -> list[DriveFileIndexEntry]:
        query = self.client.collection("drive_file_index").where("patient_id", "==", patient_id)
        return [DriveFileIndexEntry.model_validate(snapshot.to_dict() or {}) for snapshot in query.stream()]

    def get_source_runtime(self, patient_id: str, source_id: str) -> MedicalSourceRuntime | None:
        return self._get_model(
            MedicalSourceRuntime,
            "patients",
            patient_id,
            "medical_source_runtime",
            source_id,
        )

    def upsert_source_runtime(self, runtime: MedicalSourceRuntime) -> None:
        self._set_model(
            runtime,
            "patients",
            runtime.patient_id,
            "medical_source_runtime",
            runtime.source_id,
        )

    def upsert_source_bundle(
        self,
        *,
        source: MedicalSource,
        runtime: MedicalSourceRuntime,
        drive_file_index: DriveFileIndexEntry,
    ) -> None:
        batch = self.client.batch()
        batch.set(
            self._doc("patients", source.patient_id, "medical_sources", source.source_id),
            source.model_dump(exclude_none=True),
            merge=False,
        )
        batch.set(
            self._doc("patients", runtime.patient_id, "medical_source_runtime", runtime.source_id),
            runtime.model_dump(exclude_none=True),
            merge=False,
        )
        batch.set(
            self._doc("drive_file_index", drive_file_index.drive_file_id),
            drive_file_index.model_dump(exclude_none=True),
            merge=False,
        )
        batch.commit()

    def upsert_source_status_bundle(
        self,
        *,
        source: MedicalSource | None,
        runtime: MedicalSourceRuntime | None,
        drive_file_index: DriveFileIndexEntry | None,
    ) -> None:
        batch = self.client.batch()
        wrote = False
        if source is not None:
            batch.set(
                self._doc("patients", source.patient_id, "medical_sources", source.source_id),
                source.model_dump(exclude_none=True),
                merge=False,
            )
            wrote = True
        if runtime is not None:
            batch.set(
                self._doc("patients", runtime.patient_id, "medical_source_runtime", runtime.source_id),
                runtime.model_dump(exclude_none=True),
                merge=False,
            )
            wrote = True
        if drive_file_index is not None:
            batch.set(
                self._doc("drive_file_index", drive_file_index.drive_file_id),
                drive_file_index.model_dump(exclude_none=True),
                merge=False,
            )
            wrote = True
        if wrote:
            batch.commit()

    def try_acquire_source_runtime_lock(
        self,
        patient_id: str,
        source_id: str,
        *,
        lock_owner: str,
        lease_expires_at: str,
        now: str,
    ) -> MedicalSourceRuntime | None:
        doc_ref = self._doc("patients", patient_id, "medical_source_runtime", source_id)
        transaction = self.client.transaction()

        @firestore.transactional
        def acquire_in_transaction(transaction: Any) -> MedicalSourceRuntime | None:
            snapshot = doc_ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            runtime = MedicalSourceRuntime.model_validate(snapshot.to_dict() or {})
            if runtime.sync.lock_owner and runtime.sync.lease_expires_at and not _lease_expired(runtime.sync.lease_expires_at, now):
                return None
            locked = runtime.model_copy(
                update={
                    "sync": runtime.sync.model_copy(
                        update={
                            "status": "UPLOADING",
                            "lock_owner": lock_owner,
                            "lease_expires_at": lease_expires_at,
                        }
                    ),
                    "updated_at": now,
                }
            )
            transaction.set(doc_ref, locked.model_dump(exclude_none=True), merge=True)
            return locked

        return acquire_in_transaction(transaction)

    def release_source_runtime_lock(
        self,
        patient_id: str,
        source_id: str,
        *,
        lock_owner: str,
        now: str,
        sync_status: str | None = None,
    ) -> MedicalSourceRuntime | None:
        doc_ref = self._doc("patients", patient_id, "medical_source_runtime", source_id)
        transaction = self.client.transaction()

        @firestore.transactional
        def release_in_transaction(transaction: Any) -> MedicalSourceRuntime | None:
            snapshot = doc_ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            runtime = MedicalSourceRuntime.model_validate(snapshot.to_dict() or {})
            if runtime.sync.lock_owner and runtime.sync.lock_owner != lock_owner:
                return None
            updates = {"lock_owner": None, "lease_expires_at": None}
            if sync_status is not None:
                updates["status"] = sync_status
            released = runtime.model_copy(
                update={
                    "sync": runtime.sync.model_copy(update=updates),
                    "updated_at": now,
                }
            )
            transaction.set(doc_ref, released.model_dump(exclude_none=True), merge=True)
            return released

        return release_in_transaction(transaction)

    def get_wiki_page(self, patient_id: str, page_id: str) -> MedicalWikiPage | None:
        return self._get_model(MedicalWikiPage, "patients", patient_id, "medical_wiki_pages", page_id)

    def upsert_wiki_page(self, page: MedicalWikiPage) -> None:
        self._set_model(page, "patients", page.patient_id, "medical_wiki_pages", page.page_id)

    def list_wiki_pages(self, patient_id: str) -> list[MedicalWikiPage]:
        return self._stream_models(MedicalWikiPage, "patients", patient_id, "medical_wiki_pages")

    def get_wiki_index(self, patient_id: str) -> MedicalWikiIndex | None:
        return self._get_model(MedicalWikiIndex, "patients", patient_id, "medical_wiki_index", "main")

    def upsert_wiki_index(self, index: MedicalWikiIndex) -> None:
        self._set_model(index, "patients", index.patient_id, "medical_wiki_index", "main")

    def append_wiki_log(self, patient_id: str, payload: dict[str, Any]) -> str:
        log_id = str(payload.get("log_id") or f"LOG_{uuid.uuid4().hex[:12].upper()}")
        self._doc("patients", patient_id, "medical_wiki_logs", log_id).set(
            {**payload, "log_id": log_id},
            merge=False,
        )
        return log_id

    def upsert_wiki_index_with_log(
        self,
        *,
        index: MedicalWikiIndex,
        log_payload: dict[str, Any],
    ) -> str:
        log_id = str(log_payload.get("log_id") or f"LOG_{uuid.uuid4().hex[:12].upper()}")
        batch = self.client.batch()
        batch.set(
            self._doc("patients", index.patient_id, "medical_wiki_index", "main"),
            index.model_dump(exclude_none=True),
            merge=False,
        )
        batch.set(
            self._doc("patients", index.patient_id, "medical_wiki_logs", log_id),
            {**log_payload, "log_id": log_id},
            merge=False,
        )
        batch.commit()
        return log_id

    def get_chat_session(self, patient_id: str, kakao_user_id_hash: str) -> ChatSession | None:
        return self._get_model(
            ChatSession,
            "patients",
            patient_id,
            "chat_sessions",
            kakao_user_id_hash,
        )

    def upsert_chat_session(self, session: ChatSession) -> None:
        self._set_model(
            session,
            "patients",
            session.patient_id,
            "chat_sessions",
            session.kakao_user_id_hash,
        )

    def delete_chat_session(self, patient_id: str, kakao_user_id_hash: str) -> None:
        self._doc("patients", patient_id, "chat_sessions", kakao_user_id_hash).delete()

    def create_chat_log(self, log: ChatLog) -> str:
        self._set_model(log, "patients", log.patient_id, "chat_logs", log.log_id)
        return log.log_id

    def get_chat_log(self, patient_id: str, log_id: str) -> ChatLog | None:
        return self._get_model(ChatLog, "patients", patient_id, "chat_logs", log_id)

    def update_chat_log_job(self, patient_id: str, log_id: str, job_id: str) -> None:
        self._doc("patients", patient_id, "chat_logs", log_id).set({"job_id": job_id}, merge=True)

    def list_chat_logs_for_patient(self, patient_id: str, *, start_at: str, end_at: str) -> list[ChatLog]:
        query = (
            self.client.collection("patients")
            .document(patient_id)
            .collection("chat_logs")
            .where("created_at", ">=", start_at)
            .where("created_at", "<", end_at)
            .order_by("created_at")
        )
        return [ChatLog.model_validate(snapshot.to_dict() or {}) for snapshot in query.stream()]

    def list_chat_logs_for_dashboard(
        self,
        *,
        start_at: str = "",
        end_at: str = "",
        limit: int,
        offset: int = 0,
    ) -> list[tuple[PatientProfile, ChatLog]]:
        items: list[tuple[PatientProfile, ChatLog]] = []
        for patient in self.list_active_patients():
            if start_at and end_at:
                logs = self.list_chat_logs_for_patient(patient.patient_id, start_at=start_at, end_at=end_at)
            else:
                query = (
                    self.client.collection("patients")
                    .document(patient.patient_id)
                    .collection("chat_logs")
                    .order_by("created_at")
                )
                logs = [ChatLog.model_validate(snapshot.to_dict() or {}) for snapshot in query.stream()]
            for log in logs:
                if start_at and log.created_at < start_at:
                    continue
                if end_at and log.created_at >= end_at:
                    continue
                items.append((patient, log))
        items.sort(key=lambda item: (item[1].created_at, item[1].log_id), reverse=True)
        safe_offset = max(0, offset)
        return items[safe_offset:safe_offset + limit]

    def create_callback_job(self, job: KakaoCallbackJob) -> str:
        self._set_model(job, "kakao_callback_jobs", job.job_id)
        return job.job_id

    def get_callback_job(self, job_id: str) -> KakaoCallbackJob | None:
        return self._get_model(KakaoCallbackJob, "kakao_callback_jobs", job_id)

    def get_callback_job_by_idempotency_key(self, idempotency_key: str) -> KakaoCallbackJob | None:
        if not idempotency_key:
            return None
        query = self.client.collection("kakao_callback_jobs").where("idempotency_key", "==", idempotency_key).limit(10)
        fallback: KakaoCallbackJob | None = None
        for snapshot in query.stream():
            job = KakaoCallbackJob.model_validate(snapshot.to_dict() or {})
            if job.status in {"PENDING", "PROCESSING"}:
                return job
            if fallback is None:
                fallback = job
        return fallback

    def try_acquire_callback_job(
        self,
        job_id: str,
        *,
        lock_owner: str,
        lease_expires_at: str,
        now: str,
    ) -> KakaoCallbackJob | None:
        doc_ref = self._doc("kakao_callback_jobs", job_id)
        transaction = self.client.transaction()

        @firestore.transactional
        def acquire_in_transaction(transaction: Any) -> KakaoCallbackJob | None:
            snapshot = doc_ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            job = KakaoCallbackJob.model_validate(snapshot.to_dict() or {})
            if job.status in {"CALLBACK_SENT", "EXPIRED"}:
                return None
            if not job.runnable:
                return None
            if job.next_run_at and not _lease_expired(job.next_run_at, now):
                return None
            if job.expires_at and _lease_expired(job.expires_at, now):
                return None
            if job.retry_count >= job.max_attempts:
                return None
            if job.lock_owner and job.lease_expires_at and not _lease_expired(job.lease_expires_at, now):
                return None
            locked = job.model_copy(
                update={
                    "status": "PROCESSING",
                    "runnable": False,
                    "lock_owner": lock_owner,
                    "lease_expires_at": lease_expires_at,
                    "retry_count": job.retry_count + 1,
                    "processing_started_at": now,
                    "updated_at": now,
                }
            )
            transaction.set(doc_ref, locked.model_dump(exclude_none=True), merge=True)
            return locked

        return acquire_in_transaction(transaction)

    def list_runnable_callback_jobs(self, *, now: str, limit: int) -> list[KakaoCallbackJob]:
        query = (
            self.client.collection("kakao_callback_jobs")
            .where("runnable", "==", True)
            .where("next_run_at", "<=", now)
            .order_by("next_run_at")
            .limit(max(limit * 5, limit))
        )
        result: list[KakaoCallbackJob] = []
        for snapshot in query.stream():
            job = KakaoCallbackJob.model_validate(snapshot.to_dict() or {})
            if job.status not in {"PENDING", "FAILED"}:
                continue
            if job.expires_at and _lease_expired(job.expires_at, now):
                continue
            if job.retry_count >= job.max_attempts:
                continue
            if job.lock_owner and job.lease_expires_at and not _lease_expired(job.lease_expires_at, now):
                continue
            result.append(job)
            if len(result) >= limit:
                break
        return result

    def list_expired_callback_jobs(self, *, now: str, limit: int) -> list[KakaoCallbackJob]:
        query = self.client.collection("kakao_callback_jobs").where("status", "in", ["PENDING", "PROCESSING", "FAILED"]).limit(max(limit * 5, limit))
        result: list[KakaoCallbackJob] = []
        for snapshot in query.stream():
            job = KakaoCallbackJob.model_validate(snapshot.to_dict() or {})
            if job.expires_at and _lease_expired(job.expires_at, now):
                result.append(job)
                if len(result) >= limit:
                    break
        return result

    def count_callback_jobs_by_status(self) -> dict[str, int]:
        counts = {
            "PENDING": 0,
            "PROCESSING": 0,
            "FAILED": 0,
            "EXPIRED": 0,
            "CALLBACK_SENT": 0,
        }
        for status in counts:
            query = self.client.collection("kakao_callback_jobs").where("status", "==", status)
            counts[status] = sum(1 for _ in query.stream())
        return counts

    def list_recent_failed_callback_jobs(self, *, limit: int) -> list[KakaoCallbackJob]:
        query = self.client.collection("kakao_callback_jobs").where("status", "==", "FAILED")
        jobs = [KakaoCallbackJob.model_validate(snapshot.to_dict() or {}) for snapshot in query.stream()]
        jobs.sort(key=lambda item: (item.updated_at or item.created_at, item.job_id), reverse=True)
        return jobs[:limit]

    def upsert_callback_job(self, job: KakaoCallbackJob) -> None:
        self._set_model(job, "kakao_callback_jobs", job.job_id)

    def create_prewarm_job(self, job: GeminiFilePrewarmJob) -> str:
        self._set_model(job, "gemini_file_prewarm_jobs", job.job_id)
        return job.job_id

    def get_prewarm_job(self, job_id: str) -> GeminiFilePrewarmJob | None:
        return self._get_model(GeminiFilePrewarmJob, "gemini_file_prewarm_jobs", job_id)

    def get_prewarm_job_by_idempotency_key(self, idempotency_key: str) -> GeminiFilePrewarmJob | None:
        if not idempotency_key:
            return None
        query = self.client.collection("gemini_file_prewarm_jobs").where("idempotency_key", "==", idempotency_key).limit(10)
        fallback: GeminiFilePrewarmJob | None = None
        for snapshot in query.stream():
            job = GeminiFilePrewarmJob.model_validate(snapshot.to_dict() or {})
            if job.status in {"PENDING", "PROCESSING", "DONE"}:
                return job
            if fallback is None:
                fallback = job
        return fallback

    def try_acquire_prewarm_job(
        self,
        job_id: str,
        *,
        lock_owner: str,
        lease_expires_at: str,
        now: str,
    ) -> GeminiFilePrewarmJob | None:
        doc_ref = self._doc("gemini_file_prewarm_jobs", job_id)
        transaction = self.client.transaction()

        @firestore.transactional
        def acquire_in_transaction(transaction: Any) -> GeminiFilePrewarmJob | None:
            snapshot = doc_ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            job = GeminiFilePrewarmJob.model_validate(snapshot.to_dict() or {})
            if job.status in {"DONE", "SKIPPED", "EXPIRED"}:
                return None
            if not job.runnable:
                return None
            if job.next_run_at and not _lease_expired(job.next_run_at, now):
                return None
            if job.expires_at and _lease_expired(job.expires_at, now):
                return None
            if job.retry_count >= job.max_attempts:
                return None
            if job.lock_owner and job.lease_expires_at and not _lease_expired(job.lease_expires_at, now):
                return None
            locked = job.model_copy(
                update={
                    "status": "PROCESSING",
                    "runnable": False,
                    "lock_owner": lock_owner,
                    "lease_expires_at": lease_expires_at,
                    "retry_count": job.retry_count + 1,
                    "started_at": job.started_at or now,
                    "updated_at": now,
                }
            )
            transaction.set(doc_ref, locked.model_dump(exclude_none=True), merge=True)
            return locked

        return acquire_in_transaction(transaction)

    def upsert_prewarm_job(self, job: GeminiFilePrewarmJob) -> None:
        self._set_model(job, "gemini_file_prewarm_jobs", job.job_id)


def build_default_medical_repository(settings: Settings) -> MedicalRepository:
    return FirestoreMedicalRepository(
        project_id=settings.firestore_project_id,
        database_id=settings.firestore_database_id,
    )
