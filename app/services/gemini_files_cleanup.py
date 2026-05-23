from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import Settings
from app.repositories import MedicalRepository
from app.schemas import (
    GeminiFileRuntime,
    GeminiFilesCleanupDeletedItem,
    GeminiFilesCleanupError,
    GeminiFilesCleanupResult,
    MedicalSource,
    MedicalSourceRuntime,
)
from app.services.gemini_files_qa import GeminiFilesGateway
from app.services.medical_wiki import now_kst_iso


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CleanupCandidate:
    runtime: MedicalSourceRuntime
    size_bytes: int
    priority: int
    last_checked_at: str
    uploaded_at: str
    reason: str


@dataclass
class GeminiFilesCleanupService:
    settings: Settings
    repository: MedicalRepository
    gateway: GeminiFilesGateway

    def cleanup_if_needed(self) -> GeminiFilesCleanupResult:
        usage_bytes = self.current_usage_bytes()
        if usage_bytes <= self.settings.gemini_files_storage_soft_limit_bytes:
            return GeminiFilesCleanupResult(
                ok=True,
                triggered=False,
                reason="below_soft_limit",
                before_usage_bytes=usage_bytes,
                after_usage_bytes=usage_bytes,
                soft_limit_bytes=self.settings.gemini_files_storage_soft_limit_bytes,
                target_bytes=self.settings.gemini_files_storage_target_bytes,
                hard_limit_bytes=self.settings.gemini_files_storage_hard_limit_bytes,
            )
        return self.cleanup_to_target(reason="soft_limit_exceeded")

    def cleanup_to_target(self, *, reason: str = "manual") -> GeminiFilesCleanupResult:
        runtimes = self.repository.list_source_runtimes_with_gemini_files()
        before_usage = self._usage_bytes(runtimes)
        target_bytes = min(
            self.settings.gemini_files_storage_target_bytes,
            self.settings.gemini_files_storage_soft_limit_bytes,
        )
        result = GeminiFilesCleanupResult(
            ok=True,
            triggered=True,
            reason=reason,
            before_usage_bytes=before_usage,
            after_usage_bytes=before_usage,
            soft_limit_bytes=self.settings.gemini_files_storage_soft_limit_bytes,
            target_bytes=target_bytes,
            hard_limit_bytes=self.settings.gemini_files_storage_hard_limit_bytes,
        )

        if before_usage <= target_bytes:
            result.reason = "already_below_target"
            return result

        candidates = self._cleanup_candidates(runtimes)
        remaining_usage = before_usage
        for candidate in candidates:
            if remaining_usage <= target_bytes:
                break
            if result.deleted_count >= self.settings.gemini_files_cleanup_batch_size:
                break

            runtime = candidate.runtime
            file_name = runtime.gemini_file.file_name
            if not file_name:
                result.skipped_count += 1
                continue
            try:
                self.gateway.delete_file(file_name=file_name)
                self._mark_file_deleted(runtime)
                remaining_usage = max(0, remaining_usage - candidate.size_bytes)
                result.deleted_items.append(
                    GeminiFilesCleanupDeletedItem(
                        patient_id=runtime.patient_id,
                        source_id=runtime.source_id,
                        state=runtime.gemini_file.state,
                        size_bytes=candidate.size_bytes,
                        reason=candidate.reason,
                    )
                )
                result.deleted_count += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "gemini_files_cleanup delete failed patient_id=%s source_id=%s state=%s error=%s",
                    runtime.patient_id,
                    runtime.source_id,
                    runtime.gemini_file.state,
                    exc,
                )
                result.errors.append(
                    GeminiFilesCleanupError(
                        patient_id=runtime.patient_id,
                        source_id=runtime.source_id,
                        state=runtime.gemini_file.state,
                        error=str(exc)[:300],
                    )
                )
                result.skipped_count += 1

        result.after_usage_bytes = remaining_usage
        result.ok = len(result.errors) == 0
        logger.info(
            "gemini_files_cleanup done reason=%s before_usage_bytes=%s after_usage_bytes=%s deleted_count=%s skipped_count=%s error_count=%s",
            result.reason,
            result.before_usage_bytes,
            result.after_usage_bytes,
            result.deleted_count,
            result.skipped_count,
            len(result.errors),
        )
        return result

    def current_usage_bytes(self) -> int:
        return self._usage_bytes(self.repository.list_source_runtimes_with_gemini_files())

    def hard_limit_exceeded(self) -> bool:
        return self.current_usage_bytes() >= self.settings.gemini_files_storage_hard_limit_bytes

    def _usage_bytes(self, runtimes: list[MedicalSourceRuntime]) -> int:
        return sum(self._file_size_bytes(runtime) for runtime in runtimes if runtime.gemini_file.file_name)

    def _cleanup_candidates(self, runtimes: list[MedicalSourceRuntime]) -> list[_CleanupCandidate]:
        candidates: list[_CleanupCandidate] = []
        for runtime in runtimes:
            gemini_file = runtime.gemini_file
            if not gemini_file.file_name:
                continue
            size_bytes = self._file_size_bytes(runtime)
            if size_bytes <= 0:
                continue
            source = self.repository.get_medical_source(runtime.patient_id, runtime.source_id)
            priority, reason = self._candidate_priority(runtime=runtime, source=source)
            if priority is None:
                continue
            candidates.append(
                _CleanupCandidate(
                    runtime=runtime,
                    size_bytes=size_bytes,
                    priority=priority,
                    last_checked_at=gemini_file.last_checked_at or "",
                    uploaded_at=gemini_file.uploaded_at or "",
                    reason=reason,
                )
            )
        candidates.sort(
            key=lambda item: (
                item.priority,
                self._timestamp_sort_key(item.last_checked_at),
                self._timestamp_sort_key(item.uploaded_at),
                item.runtime.patient_id,
                item.runtime.source_id,
            )
        )
        return candidates

    def _candidate_priority(
        self,
        *,
        runtime: MedicalSourceRuntime,
        source: MedicalSource | None,
    ) -> tuple[int | None, str]:
        state = runtime.gemini_file.state
        if source is None or source.source_status != "ACTIVE":
            return 0, "missing_or_inactive_source"
        if state == "EXPIRED":
            return 1, "expired_file"
        if state == "FAILED":
            return 2, "failed_file"
        if self._source_snapshot_mismatch(runtime=runtime, source=source):
            return 3, "source_snapshot_mismatch"
        if state == "ACTIVE":
            return 4, "old_active_file"
        # Avoid deleting UPLOADING/PROCESSING files in normal cleanup because another request may be waiting on them.
        return None, "in_progress_file"

    def _source_snapshot_mismatch(self, *, runtime: MedicalSourceRuntime, source: MedicalSource) -> bool:
        gemini_file = runtime.gemini_file
        return (
            gemini_file.source_drive_modified_at != source.source_ref.drive_modified_at
            or gemini_file.source_file_hash != source.source_ref.file_hash
            or gemini_file.source_file_size_bytes != source.source_ref.file_size_bytes
        )

    def _mark_file_deleted(self, runtime: MedicalSourceRuntime) -> None:
        now = now_kst_iso()
        cleaned = runtime.model_copy(
            update={
                "gemini_file": GeminiFileRuntime(
                    mime_type=runtime.gemini_file.mime_type,
                    state="NONE",
                    last_checked_at=now,
                    error="deleted_by_cleanup",
                    source_drive_modified_at=runtime.gemini_file.source_drive_modified_at,
                    source_file_hash=runtime.gemini_file.source_file_hash,
                    source_file_size_bytes=runtime.gemini_file.source_file_size_bytes,
                ),
                "sync": runtime.sync.model_copy(
                    update={
                        "status": "READY" if runtime.wiki_sync.status == "READY" else runtime.sync.status,
                        "lock_owner": None,
                        "lease_expires_at": None,
                    }
                ),
                "updated_at": now,
            }
        )
        self.repository.upsert_source_runtime(cleaned)

    def _file_size_bytes(self, runtime: MedicalSourceRuntime) -> int:
        value = runtime.gemini_file.source_file_size_bytes
        if value is None or value < 0:
            return 0
        return int(value)

    def _timestamp_sort_key(self, value: str) -> float:
        if not value:
            return 0.0
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return 0.0
