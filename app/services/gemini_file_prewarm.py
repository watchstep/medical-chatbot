from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Settings
from app.repositories import MedicalRepository
from app.schemas import (
    GeminiFilePrewarmJob,
    MedicalSource,
    MedicalSourceRuntime,
    RuntimeFailure,
)
from app.services.cloud_tasks import CloudTaskEnqueueResult, CloudTasksClient, normalize_task_id
from app.services.gemini_files_cleanup import GeminiFilesCleanupService
from app.services.gemini_files_qa import GeminiFilesQaError, GeminiFilesQaService
from app.services.medical_wiki import KST, now_kst_iso

logger = logging.getLogger(__name__)


class GeminiFilePrewarmError(Exception):
    pass


class GeminiFilePrewarmRetryableError(GeminiFilePrewarmError):
    pass


class GeminiFilePrewarmTimeoutError(GeminiFilePrewarmRetryableError):
    pass


@dataclass(frozen=True)
class GeminiFilePrewarmEnqueueResult:
    ok: bool
    enqueued: bool = False
    job_id: str = ""
    source_id: str = ""
    status: str = ""
    reason: str = ""
    task_name: str = ""
    duplicate: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "enqueued": self.enqueued,
            "job_id": self.job_id,
            "source_id": self.source_id,
            "status": self.status,
            "reason": self.reason,
            "task_name": self.task_name,
            "duplicate": self.duplicate,
        }


@dataclass(frozen=True)
class GeminiFilePrewarmProcessResult:
    ok: bool
    job_id: str
    status: str
    source_id: str = ""
    reason: str = ""
    retryable: bool = False
    error_code: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "job_id": self.job_id,
            "status": self.status,
            "source_id": self.source_id,
            "reason": self.reason,
            "retryable": self.retryable,
            "error_code": self.error_code,
        }


@dataclass
class GeminiFilePrewarmEnqueueService:
    settings: Settings
    repository: MedicalRepository
    cloud_tasks_client: CloudTasksClient | None

    def enqueue_latest_source(self, *, patient_id: str, reason: str) -> GeminiFilePrewarmEnqueueResult:
        if not self.settings.gemini_file_prewarm_enabled:
            return GeminiFilePrewarmEnqueueResult(ok=True, reason="disabled")

        source_id = select_latest_prewarm_source_id(repository=self.repository, patient_id=patient_id)
        if not source_id:
            return GeminiFilePrewarmEnqueueResult(ok=True, reason="no_ready_source")

        now = datetime.now(KST)
        idempotency_key = build_prewarm_idempotency_key(
            patient_id=patient_id,
            source_id=source_id,
            now=now,
            bucket_minutes=self.settings.gemini_file_prewarm_idempotency_bucket_minutes,
        )
        existing = self.repository.get_prewarm_job_by_idempotency_key(idempotency_key)
        if existing is not None and existing.status in {"PENDING", "PROCESSING", "DONE", "SKIPPED"}:
            return GeminiFilePrewarmEnqueueResult(
                ok=True,
                enqueued=False,
                job_id=existing.job_id,
                source_id=existing.source_id,
                status=existing.status,
                reason="deduped",
            )

        job_id = f"PREWARM_{uuid.uuid4().hex[:12].upper()}"
        job = GeminiFilePrewarmJob(
            job_id=job_id,
            patient_id=patient_id,
            source_id=source_id,
            reason=reason,
            idempotency_key=idempotency_key,
            status="PENDING",
            runnable=True,
            retry_count=0,
            max_attempts=self.settings.gemini_file_prewarm_max_attempts,
            next_run_at=now_kst_iso(),
            created_at=now_kst_iso(),
            updated_at=now_kst_iso(),
            expires_at=(now + timedelta(minutes=self.settings.gemini_file_prewarm_expiry_minutes)).isoformat(),
        )
        self.repository.create_prewarm_job(job)

        if self.cloud_tasks_client is None:
            self._mark_enqueue_failed(job, code="CLOUD_TASKS_UNAVAILABLE", message="Cloud Tasks client is not configured.")
            raise GeminiFilePrewarmError("Cloud Tasks client is not configured.")

        base_url = self.settings.cloud_tasks_base_url.rstrip("/")
        audience = self.settings.cloud_tasks_audience or self.settings.cloud_tasks_base_url
        if not base_url or not audience or not self.settings.cloud_tasks_service_account_email:
            self._mark_enqueue_failed(job, code="CLOUD_TASKS_CONFIG_MISSING", message="Cloud Tasks target configuration is incomplete.")
            raise GeminiFilePrewarmError("Cloud Tasks target configuration is incomplete.")

        task_id = normalize_task_id(f"prewarm-{job.job_id.lower()}")
        try:
            result = self.cloud_tasks_client.enqueue_http_post(
                task_id=task_id,
                url=f"{base_url}/admin/prewarm-gemini-file",
                payload={"prewarm_job_id": job.job_id},
                service_account_email=self.settings.cloud_tasks_service_account_email,
                audience=audience,
                dispatch_deadline_seconds=self.settings.gemini_file_prewarm_dispatch_deadline_seconds,
            )
            logger.info(
                "prewarm_task enqueued job_id=%s source_id=%s task_name=%s duplicate=%s reason=%s",
                job.job_id,
                source_id,
                result.task_name,
                result.duplicate,
                reason,
            )
            return GeminiFilePrewarmEnqueueResult(
                ok=True,
                enqueued=not result.duplicate,
                job_id=job.job_id,
                source_id=source_id,
                status="PENDING",
                reason="task_duplicate" if result.duplicate else "enqueued",
                task_name=result.task_name,
                duplicate=result.duplicate,
            )
        except Exception as exc:
            self._mark_enqueue_failed(job, code="CLOUD_TASKS_ENQUEUE_FAILED", message=str(exc))
            raise

    def _mark_enqueue_failed(self, job: GeminiFilePrewarmJob, *, code: str, message: str) -> None:
        now = now_kst_iso()
        self.repository.upsert_prewarm_job(
            job.model_copy(
                update={
                    "status": "FAILED",
                    "runnable": False,
                    "next_run_at": "",
                    "last_failure": RuntimeFailure(code=code, message=message[:300], failed_at=now),
                    "updated_at": now,
                    "finished_at": now,
                }
            )
        )


@dataclass
class GeminiFilePrewarmService:
    settings: Settings
    repository: MedicalRepository
    qa_service: GeminiFilesQaService
    cleanup_service: GeminiFilesCleanupService | None = None

    def process_job(self, prewarm_job_id: str) -> GeminiFilePrewarmProcessResult:
        if not prewarm_job_id:
            return GeminiFilePrewarmProcessResult(ok=False, job_id="", status="NOT_FOUND", reason="missing_job_id")

        deadline = time.monotonic() + self.settings.async_worker_processing_timeout_seconds
        now = now_kst_iso()
        lock_owner = f"prewarm-worker-{uuid.uuid4().hex[:12]}"
        lease_expires_at = (
            datetime.now(KST) + timedelta(seconds=self.settings.gemini_file_prewarm_lease_seconds)
        ).isoformat()
        job = self.repository.try_acquire_prewarm_job(
            prewarm_job_id,
            lock_owner=lock_owner,
            lease_expires_at=lease_expires_at,
            now=now,
        )
        if job is None:
            existing = self.repository.get_prewarm_job(prewarm_job_id)
            if existing is None:
                return GeminiFilePrewarmProcessResult(ok=False, job_id=prewarm_job_id, status="NOT_FOUND")
            return GeminiFilePrewarmProcessResult(
                ok=True,
                job_id=existing.job_id,
                status=existing.status,
                source_id=existing.source_id,
                reason="not_runnable_or_already_done",
            )

        if job.expires_at and _is_expired(job.expires_at):
            expired = self._finish_job(job, status="EXPIRED", lock_owner=lock_owner, reason="job_expired")
            return GeminiFilePrewarmProcessResult(
                ok=False,
                job_id=job.job_id,
                status=expired.status,
                source_id=job.source_id,
                reason="job_expired",
            )

        try:
            self._ensure_processing_budget(deadline)
            source_id = job.source_id or select_latest_prewarm_source_id(repository=self.repository, patient_id=job.patient_id)
            if not source_id:
                skipped = self._finish_job(job, status="SKIPPED", lock_owner=lock_owner, reason="no_ready_source")
                return GeminiFilePrewarmProcessResult(ok=True, job_id=job.job_id, status=skipped.status, reason="no_ready_source")

            source, runtime = self._load_ready_source_runtime(patient_id=job.patient_id, source_id=source_id)
            if self._has_reusable_active_file(source=source, runtime=runtime):
                skipped = self._finish_job(job, status="SKIPPED", lock_owner=lock_owner, source_id=source_id, reason="already_active")
                return GeminiFilePrewarmProcessResult(ok=True, job_id=job.job_id, status=skipped.status, source_id=source_id, reason="already_active")

            self._ensure_processing_budget(deadline, reserve_seconds=5.0)
            if self.cleanup_service is not None and self.cleanup_service.hard_limit_exceeded():
                self.cleanup_service.cleanup_to_target(reason="prewarm_hard_limit")
                if self.cleanup_service.hard_limit_exceeded():
                    skipped = self._finish_job(job, status="SKIPPED", lock_owner=lock_owner, source_id=source_id, reason="hard_limit_exceeded")
                    return GeminiFilePrewarmProcessResult(ok=True, job_id=job.job_id, status=skipped.status, source_id=source_id, reason="hard_limit_exceeded")
            elif self.cleanup_service is not None:
                self.cleanup_service.cleanup_if_needed()

            self._ensure_processing_budget(deadline, reserve_seconds=5.0)
            self.qa_service.prepare_file(
                patient_id=job.patient_id,
                source_id=source_id,
                timing_context={"prewarm_job_id": job.job_id},
            )
            done = self._finish_job(job, status="DONE", lock_owner=lock_owner, source_id=source_id, reason="prepared")
            return GeminiFilePrewarmProcessResult(ok=True, job_id=job.job_id, status=done.status, source_id=source_id, reason="prepared")
        except Exception as exc:
            failed = self._mark_failed(job, lock_owner=lock_owner, code="PREWARM_FAILED", message=str(exc))
            retryable = failed.retry_count < failed.max_attempts and not _is_expired(failed.expires_at)
            if retryable:
                raise GeminiFilePrewarmRetryableError(str(exc)) from exc
            return GeminiFilePrewarmProcessResult(
                ok=False,
                job_id=job.job_id,
                status=failed.status,
                source_id=job.source_id,
                retryable=False,
                error_code="PREWARM_FAILED",
                reason=str(exc)[:300],
            )

    def _load_ready_source_runtime(self, *, patient_id: str, source_id: str) -> tuple[MedicalSource, MedicalSourceRuntime]:
        source = self.repository.get_medical_source(patient_id, source_id)
        runtime = self.repository.get_source_runtime(patient_id, source_id)
        if source is None or runtime is None:
            raise GeminiFilesQaError("prewarm source runtime not found")
        if source.patient_id != patient_id or source.source_status != "ACTIVE":
            raise GeminiFilesQaError("prewarm source is inactive or foreign")
        if runtime.sync.status != "READY" or runtime.wiki_sync.status != "READY":
            raise GeminiFilesQaError("prewarm source is not runtime-ready")
        return source, runtime

    def _has_reusable_active_file(self, *, source: MedicalSource, runtime: MedicalSourceRuntime) -> bool:
        gemini_file = runtime.gemini_file
        if gemini_file.state != "ACTIVE" or not gemini_file.file_name:
            return False
        if gemini_file.source_drive_modified_at != source.source_ref.drive_modified_at:
            return False
        if gemini_file.source_file_hash != source.source_ref.file_hash:
            return False
        if gemini_file.source_file_size_bytes != source.source_ref.file_size_bytes:
            return False
        return _expiration_is_valid(gemini_file.expiration_time, margin_seconds=self.settings.file_expiration_margin_seconds)

    def _finish_job(
        self,
        job: GeminiFilePrewarmJob,
        *,
        status: str,
        lock_owner: str,
        reason: str,
        source_id: str | None = None,
    ) -> GeminiFilePrewarmJob:
        now = now_kst_iso()
        updates: dict[str, Any] = {
            "status": status,
            "runnable": False,
            "next_run_at": "",
            "lock_owner": None,
            "lease_expires_at": None,
            "skip_reason": reason if status == "SKIPPED" else job.skip_reason,
            "finished_at": now,
            "updated_at": now,
        }
        if source_id:
            updates["source_id"] = source_id
        if job.lock_owner and job.lock_owner != lock_owner:
            logger.warning("prewarm finish lock_owner mismatch job_id=%s", job.job_id)
        finished = job.model_copy(update=updates)
        self.repository.upsert_prewarm_job(finished)
        return finished

    def _mark_failed(self, job: GeminiFilePrewarmJob, *, lock_owner: str, code: str, message: str) -> GeminiFilePrewarmJob:
        now = now_kst_iso()
        retry_count = job.retry_count
        retryable = retry_count < job.max_attempts and not _is_expired(job.expires_at)
        failed = job.model_copy(
            update={
                "status": "FAILED",
                "runnable": retryable,
                "retry_count": retry_count,
                "next_run_at": now if retryable else "",
                "lock_owner": None,
                "lease_expires_at": None,
                "last_failure": RuntimeFailure(code=code, message=message[:300], failed_at=now),
                "updated_at": now,
                "finished_at": "" if retryable else now,
            }
        )
        if job.lock_owner and job.lock_owner != lock_owner:
            logger.warning("prewarm fail lock_owner mismatch job_id=%s", job.job_id)
        self.repository.upsert_prewarm_job(failed)
        return failed

    def _ensure_processing_budget(self, deadline: float, *, reserve_seconds: float = 0.0) -> None:
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds < reserve_seconds:
            raise GeminiFilePrewarmTimeoutError(
                f"prewarm processing budget exceeded; remaining_seconds={remaining_seconds:.3f}"
            )


def select_latest_prewarm_source_id(*, repository: MedicalRepository, patient_id: str) -> str | None:
    wiki_index = repository.get_wiki_index(patient_id)
    if wiki_index is None or not wiki_index.pages:
        return None
    candidates: list[tuple[str, float, str]] = []
    for page in wiki_index.pages:
        if not page.source_id or page.needs_review:
            continue
        source = repository.get_medical_source(patient_id, page.source_id)
        runtime = repository.get_source_runtime(patient_id, page.source_id)
        if source is None or runtime is None:
            continue
        if source.source_status != "ACTIVE":
            continue
        if runtime.sync.status != "READY" or runtime.wiki_sync.status != "READY":
            continue
        candidates.append((page.date or "", page.confidence or 0.0, page.source_id))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


def build_prewarm_idempotency_key(*, patient_id: str, source_id: str, now: datetime, bucket_minutes: int) -> str:
    bucket_minutes = max(1, bucket_minutes)
    minute_of_day = now.hour * 60 + now.minute
    bucket = minute_of_day // bucket_minutes
    return f"prewarm:{patient_id}:{source_id}:{now.strftime('%Y%m%d')}:{bucket}"


def _is_expired(value: str) -> bool:
    if not value:
        return False
    try:
        return datetime.fromisoformat(value) <= datetime.now(KST)
    except ValueError:
        return True


def _expiration_is_valid(expiration_time: str, *, margin_seconds: int) -> bool:
    if not expiration_time:
        return False
    try:
        expiration = datetime.fromisoformat(expiration_time.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expiration.tzinfo is None:
        expiration = expiration.replace(tzinfo=timezone.utc)
    return expiration > datetime.now(timezone.utc) + timedelta(seconds=margin_seconds)
