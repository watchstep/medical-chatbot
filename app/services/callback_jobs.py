from __future__ import annotations

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.config import Settings
from app.repositories import MedicalRepository
from app.schemas import (
    ChatLog,
    ChatSession,
    FinalQaAnswer,
    KakaoCallbackJob,
    MedicalQuestionIntent,
    MedicalWikiIndex,
    RuntimeFailure,
)
from app.services.gemini_files_qa import (
    GeminiFileNotReadyError,
    GeminiFilesQaError,
    GeminiFilesQaService,
    MedicalRouterService,
)
from app.services.kakao_callback import KakaoCallbackService
from app.services.medical_wiki import KST, now_kst_iso
from app.services.timing import timing_done, timing_start

logger = logging.getLogger(__name__)

SYSTEM_PREPARING_MESSAGE = "관련 문서를 찾아 준비 중입니다.\n잠시 후 다시 시도해 주세요.🙇‍♂️"
SAFE_FALLBACK_MESSAGE = "🔍 해당 내용은 제공된 의료 기록에서 확인하기 어렵습니다."
OUT_OF_SCOPE_MESSAGE = "💬 의료 기록과 관련된 질문에만 답변을 드릴 수 있습니다."

FIXED_INTENT_TO_STATUS = {
    "EMERGENCY": "emergency",
    "PRIVACY_BLOCK": "blocked",
    "COST_BLOCK": "cost_block",
    "OUT_OF_SCOPE": "out_of_scope",
}
FIXED_STATUS_MESSAGES = {
    "emergency": "🧑‍⚕️ 증상이 지속된다면 의료기관을 찾아 전문의와 상의해 보시길 권합니다.",
    "blocked": "🔒 개인정보 보호 정책에 따라 세부 개인정보는 안내해 드리지 않습니다.",
    "cost_block": "💳 비용 관련 정보는 해당 의료기관에 직접 문의하셔야 합니다.",
    "out_of_scope": OUT_OF_SCOPE_MESSAGE,
    "cannot_verify": SAFE_FALLBACK_MESSAGE,
}


@dataclass(frozen=True)
class CallbackJobProcessResult:
    job_id: str
    status: str
    sent: bool = False
    text_type: str = ""
    error_code: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "sent": self.sent,
            "text_type": self.text_type,
            "error_code": self.error_code,
        }


class CallbackProcessingBudgetExceeded(Exception):
    pass


@dataclass
class CallbackJobProcessor:
    settings: Settings
    repository: MedicalRepository
    router_service: MedicalRouterService
    gemini_files_qa_service: GeminiFilesQaService | None
    kakao_callback_service: KakaoCallbackService

    def process_job(self, job_id: str) -> CallbackJobProcessResult:
        callback_started_at = timing_start(logger, "callback_job.start", job_id=job_id)
        result: CallbackJobProcessResult | None = None
        patient_id = ""
        error_code = ""
        try:
            result = self._process_job(job_id)
            return result
        except Exception:
            error_code = "UNHANDLED_EXCEPTION"
            raise
        finally:
            stored_job = self.repository.get_callback_job(job_id)
            if stored_job is not None:
                patient_id = stored_job.patient_id
            timing_done(
                logger,
                "callback_job.done",
                callback_started_at,
                status=result.status if result is not None else "error",
                job_id=job_id,
                patient_id=patient_id,
                sent=result.sent if result is not None else None,
                text_type=result.text_type if result is not None else "",
                error_code=result.error_code if result is not None else error_code,
            )

    def _process_job(self, job_id: str) -> CallbackJobProcessResult:
        now = now_kst_iso()
        deadline = time.monotonic() + self.settings.callback_processing_budget_seconds
        lock_owner = f"callback-worker-{uuid.uuid4().hex[:12]}"
        lease_expires_at = (
            datetime.now(KST) + timedelta(seconds=self.settings.callback_job_lease_seconds)
        ).isoformat()

        job_lock_started_at = timing_start(logger, "job_lock.start", job_id=job_id)
        try:
            job = self.repository.try_acquire_callback_job(
                job_id,
                lock_owner=lock_owner,
                lease_expires_at=lease_expires_at,
                now=now,
            )
        except Exception:
            timing_done(logger, "job_lock.done", job_lock_started_at, status="error", job_id=job_id, error_code="JOB_LOCK_FAILED")
            raise
        timing_done(
            logger,
            "job_lock.done",
            job_lock_started_at,
            status="acquired" if job is not None else "not_acquired",
            job_id=job_id,
            patient_id=job.patient_id if job is not None else "",
        )

        if job is None:
            existing = self.repository.get_callback_job(job_id)
            if existing is not None and self._is_expired(existing.expires_at) and existing.status not in {"CALLBACK_SENT", "EXPIRED"}:
                expired = self._mark_expired(existing, code="JOB_EXPIRED", message="callback job expired before processing")
                return CallbackJobProcessResult(job_id=job_id, status=expired.status, error_code="JOB_EXPIRED")
            return CallbackJobProcessResult(
                job_id=job_id,
                status=existing.status if existing is not None else "NOT_FOUND",
                error_code="NOT_RUNNABLE",
            )

        if self._is_expired(job.expires_at):
            expired = self._mark_expired(job, code="JOB_EXPIRED", message="callback job expired before processing")
            return CallbackJobProcessResult(job_id=job_id, status=expired.status, error_code="JOB_EXPIRED")

        answer_text = SAFE_FALLBACK_MESSAGE
        text_type = "fallback"
        failure: RuntimeFailure | None = None

        try:
            question, prior_context, wiki_index = self._load_job_context(job=job, job_id=job_id)
            self._ensure_processing_budget(deadline, reserve_seconds=self.settings.kakao_callback_timeout_seconds + 1.0)

            router_started_at = timing_start(logger, "router.start", job_id=job_id, patient_id=job.patient_id)
            try:
                selection = self.router_service.select_source(
                    question=question,
                    prior_context=prior_context,
                    wiki_index=wiki_index,
                )
            except Exception:
                timing_done(
                    logger,
                    "router.done",
                    router_started_at,
                    status="error",
                    job_id=job_id,
                    patient_id=job.patient_id,
                    error_code="ROUTER_FAILED",
                )
                raise
            timing_done(
                logger,
                "router.done",
                router_started_at,
                status=selection.selection_status,
                job_id=job_id,
                patient_id=job.patient_id,
                intent=selection.intent,
                source_id=selection.primary_source_id,
                confidence=selection.confidence,
                reason=selection.reason,
            )

            answer_source_id = (
                selection.primary_source_id
                if selection.selection_status == "selected" and selection.intent == "OK"
                else ""
            )

            answer = self._build_answer_from_selection(
                job=job,
                job_id=job_id,
                question=question,
                prior_context=prior_context,
                intent=selection.intent,
                answer_source_id=answer_source_id,
                deadline=deadline,
            )
            if answer.status != "ok":
                answer_source_id = ""

            answer_text = self._render_answer(
                patient_id=job.patient_id,
                answer=answer,
                intent=selection.intent,
                answer_source_id=answer_source_id,
                job_id=job_id,
            )
            text_type = "answer"

            self._append_session_turn(
                patient_id=job.patient_id,
                kakao_user_id_hash=job.kakao_user_id_hash,
                question=question,
                answer_status=answer.status,
                text_type=text_type,
            )
        except GeminiFileNotReadyError as exc:
            logger.info("callback file not ready job_id=%s error=%s", job_id, exc)
            failed = self._mark_failed(
                job,
                code="FILE_NOT_READY",
                message=str(exc),
                lock_owner=lock_owner,
                keep_retryable=True,
            )
            return CallbackJobProcessResult(job_id=job_id, status=failed.status, error_code="FILE_NOT_READY")
        except CallbackProcessingBudgetExceeded as exc:
            logger.warning("callback processing budget exceeded job_id=%s", job_id)
            failed = self._mark_failed(
                job,
                code="CALLBACK_PROCESSING_BUDGET_EXCEEDED",
                message=str(exc),
                lock_owner=lock_owner,
                keep_retryable=True,
            )
            return CallbackJobProcessResult(
                job_id=job_id,
                status=failed.status,
                error_code="CALLBACK_PROCESSING_BUDGET_EXCEEDED",
            )
        except GeminiFilesQaError as exc:
            logger.exception("callback QA failed safely job_id=%s", job_id)
            failure = RuntimeFailure(
                code="QA_FAILED",
                message=self._safe_error_message(str(exc)),
                failed_at=now_kst_iso(),
            )
            answer_text = SAFE_FALLBACK_MESSAGE
            text_type = "fallback"
        except Exception as exc:
            logger.exception("callback processing failed job_id=%s", job_id)
            failed = self._mark_failed(
                job,
                code="CALLBACK_JOB_FAILED",
                message=str(exc),
                lock_owner=lock_owner,
                keep_retryable=True,
            )
            return CallbackJobProcessResult(job_id=job_id, status=failed.status, error_code="CALLBACK_JOB_FAILED")

        self._ensure_processing_budget(deadline, reserve_seconds=self.settings.kakao_callback_timeout_seconds + 0.5)
        return self._send_callback(
            job=job,
            lock_owner=lock_owner,
            answer_text=answer_text,
            text_type=text_type,
            failure=failure,
        )

    def _load_job_context(self, *, job: KakaoCallbackJob, job_id: str) -> tuple[str, str, MedicalWikiIndex]:
        context_started_at = timing_start(logger, "job_context_load.start", job_id=job_id, patient_id=job.patient_id)
        try:
            chat_log = self.repository.get_chat_log(job.patient_id, job.chat_log_id)
            if chat_log is None:
                raise GeminiFilesQaError("callback job chat log not found")
            question = chat_log.message
            wiki_index = self.repository.get_wiki_index(job.patient_id) or MedicalWikiIndex(patient_id=job.patient_id)
            prior_context = self._prior_context(job.patient_id, job.kakao_user_id_hash)
        except Exception:
            timing_done(
                logger,
                "job_context_load.done",
                context_started_at,
                status="error",
                job_id=job_id,
                patient_id=job.patient_id,
                error_code="JOB_CONTEXT_LOAD_FAILED",
            )
            raise
        timing_done(
            logger,
            "job_context_load.done",
            context_started_at,
            status="success",
            job_id=job_id,
            patient_id=job.patient_id,
        )
        return question, prior_context, wiki_index

    def _build_answer_from_selection(
        self,
        *,
        job: KakaoCallbackJob,
        job_id: str,
        question: str,
        prior_context: str,
        intent: MedicalQuestionIntent,
        answer_source_id: str,
        deadline: float,
    ) -> FinalQaAnswer:
        if intent in FIXED_INTENT_TO_STATUS:
            return FinalQaAnswer(status=FIXED_INTENT_TO_STATUS[intent])

        if self.gemini_files_qa_service is None:
            raise GeminiFilesQaError("Gemini Files QA service is not configured")

        prepared_file = None
        source_id_for_log = ""
        if answer_source_id:
            self._validate_router_selection_for_patient(patient_id=job.patient_id, source_id=answer_source_id)
            self._ensure_processing_budget(deadline, reserve_seconds=self.settings.kakao_callback_timeout_seconds + 1.0)

            prepare_started_at = timing_start(
                logger,
                "prepare_file.start",
                job_id=job_id,
                patient_id=job.patient_id,
                source_id=answer_source_id,
            )
            try:
                prepared_file = self.gemini_files_qa_service.prepare_file(
                    patient_id=job.patient_id,
                    source_id=answer_source_id,
                    timing_context={"job_id": job_id},
                )
            except Exception:
                timing_done(
                    logger,
                    "prepare_file.done",
                    prepare_started_at,
                    status="error",
                    job_id=job_id,
                    patient_id=job.patient_id,
                    source_id=answer_source_id,
                    error_code="PREPARE_FILE_FAILED",
                )
                raise
            timing_done(
                logger,
                "prepare_file.done",
                prepare_started_at,
                status="success",
                job_id=job_id,
                patient_id=job.patient_id,
                source_id=answer_source_id,
            )
            source_id_for_log = prepared_file.source_id

        self._ensure_processing_budget(deadline, reserve_seconds=self.settings.kakao_callback_timeout_seconds + 1.0)
        final_qa_started_at = timing_start(
            logger,
            "final_qa.start",
            job_id=job_id,
            patient_id=job.patient_id,
            source_id=source_id_for_log,
        )
        try:
            answer = self.gemini_files_qa_service.answer_question(
                patient_id=job.patient_id,
                question=question,
                prior_context=prior_context,
                prepared_file=prepared_file,
                intent=intent,
            )
        except Exception:
            timing_done(
                logger,
                "final_qa.done",
                final_qa_started_at,
                status="error",
                job_id=job_id,
                patient_id=job.patient_id,
                source_id=source_id_for_log,
                error_code="FINAL_QA_FAILED",
            )
            raise
        timing_done(
            logger,
            "final_qa.done",
            final_qa_started_at,
            status=answer.status,
            job_id=job_id,
            patient_id=job.patient_id,
            source_id=source_id_for_log,
        )
        return answer

    def _render_answer(
        self,
        *,
        patient_id: str,
        answer: FinalQaAnswer,
        intent: MedicalQuestionIntent,
        answer_source_id: str,
        job_id: str,
    ) -> str:
        render_started_at = timing_start(logger, "render.start", job_id=job_id, patient_id=patient_id, source_id=answer_source_id)
        try:
            if self.gemini_files_qa_service is None:
                answer_text = self._fixed_status_text(answer)
            else:
                answer_text = self.gemini_files_qa_service.render_answer(
                    patient_id=patient_id,
                    answer=answer,
                    intent=intent,
                    selected_source_id=answer_source_id,
                )
        except Exception:
            timing_done(
                logger,
                "render.done",
                render_started_at,
                status="error",
                job_id=job_id,
                patient_id=patient_id,
                source_id=answer_source_id,
                error_code="RENDER_FAILED",
            )
            raise
        timing_done(
            logger,
            "render.done",
            render_started_at,
            status="success",
            job_id=job_id,
            patient_id=patient_id,
            source_id=answer_source_id,
        )
        return answer_text

    def _send_callback(
        self,
        *,
        job: KakaoCallbackJob,
        lock_owner: str,
        answer_text: str,
        text_type: str,
        failure: RuntimeFailure | None,
    ) -> CallbackJobProcessResult:
        callback_send_started_at: float | None = None
        callback_send_done = False
        try:
            callback_send_started_at = timing_start(
                logger,
                "callback_send.start",
                job_id=job.job_id,
                patient_id=job.patient_id,
                text_type=text_type,
            )
            self.kakao_callback_service.send_text_response(callback_url=job.callback_url, text=answer_text)
            timing_done(
                logger,
                "callback_send.done",
                callback_send_started_at,
                status="success",
                job_id=job.job_id,
                patient_id=job.patient_id,
                text_type=text_type,
            )
            callback_send_done = True
            self._store_delivered_answer_log(job=job, answer_text=answer_text, text_type=text_type)
            sent = job.model_copy(
                update={
                    "status": "CALLBACK_SENT",
                    "runnable": False,
                    "next_run_at": "",
                    "lock_owner": None,
                    "lease_expires_at": None,
                    "last_failure": failure,
                    "sent_at": now_kst_iso(),
                    "sent_text_type": text_type,
                    "final_text_hash": self._hash_text(answer_text),
                    "callback_sent_count": job.callback_sent_count + 1,
                    "updated_at": now_kst_iso(),
                }
            )
            self.repository.upsert_callback_job(sent)
            return CallbackJobProcessResult(job_id=job.job_id, status="CALLBACK_SENT", sent=True, text_type=text_type)
        except Exception as exc:
            logger.exception("callback send failed job_id=%s", job.job_id)
            if callback_send_started_at is not None and not callback_send_done:
                timing_done(
                    logger,
                    "callback_send.done",
                    callback_send_started_at,
                    status="error",
                    job_id=job.job_id,
                    patient_id=job.patient_id,
                    text_type=text_type,
                    error_code="CALLBACK_SEND_FAILED",
                )
            failed = self._mark_failed(
                job,
                code="CALLBACK_SEND_FAILED",
                message=str(exc),
                lock_owner=lock_owner,
                keep_retryable=True,
            )
            return CallbackJobProcessResult(job_id=job.job_id, status=failed.status, error_code="CALLBACK_SEND_FAILED")

    def _store_delivered_answer_log(self, *, job: KakaoCallbackJob, answer_text: str, text_type: str) -> None:
        try:
            self.repository.create_chat_log(
                ChatLog(
                    log_id=f"ANSWER_{job.job_id}",
                    patient_id=job.patient_id,
                    kakao_user_id_hash=job.kakao_user_id_hash,
                    role="assistant",
                    message=answer_text,
                    message_type="answer" if text_type == "answer" else "system",
                    job_id=job.job_id,
                    created_at=now_kst_iso(),
                )
            )
        except Exception:
            logger.exception("failed to store delivered assistant answer log job_id=%s", job.job_id)

    def process_pending_jobs(self, *, limit: int | None = None) -> dict[str, object]:
        now = now_kst_iso()
        batch_size = limit or self.settings.callback_job_batch_size
        jobs = self.repository.list_runnable_callback_jobs(now=now, limit=batch_size)
        results = [self.process_job(job.job_id).to_dict() for job in jobs]
        return {"ok": True, "processed_count": len(results), "results": results}

    def expire_jobs(self, *, limit: int | None = None) -> dict[str, object]:
        now = now_kst_iso()
        batch_size = limit or self.settings.callback_job_batch_size
        jobs = self.repository.list_expired_callback_jobs(now=now, limit=batch_size)
        results = [
            self._mark_expired(job, code="JOB_EXPIRED", message="callback job expired").model_dump()
            for job in jobs
        ]
        return {"ok": True, "expired_count": len(results), "results": results}

    def _validate_router_selection_for_patient(self, *, patient_id: str, source_id: str) -> None:
        source = self.repository.get_medical_source(patient_id, source_id)
        runtime = self.repository.get_source_runtime(patient_id, source_id)
        if source is None or runtime is None:
            raise GeminiFilesQaError("Router가 선택한 source를 찾지 못했습니다.")
        if source.patient_id != patient_id or source.source_status != "ACTIVE":
            raise GeminiFilesQaError("Router가 선택한 source가 현재 환자의 활성 문서가 아닙니다.")
        if runtime.sync.status != "READY":
            raise GeminiFilesQaError("Router가 선택한 source의 동기화 상태가 READY가 아닙니다.")
        if runtime.wiki_sync.status != "READY":
            raise GeminiFilesQaError("Router가 선택한 source의 wiki 상태가 READY가 아닙니다.")

    def _prior_context(self, patient_id: str, kakao_user_id_hash: str) -> str:
        session = self.repository.get_chat_session(patient_id, kakao_user_id_hash)
        if session is None or not session.recent_messages:
            return ""
        lines: list[str] = []
        for item in session.recent_messages[-6:]:
            role = str(item.get("role") or "")
            text = str(item.get("text") or item.get("status") or "")
            if not role or not text:
                continue
            lines.append(f"{role}: {text[:160]}")
        return "\n".join(lines)

    def _append_session_turn(
        self,
        *,
        patient_id: str,
        kakao_user_id_hash: str,
        question: str,
        answer_status: str,
        text_type: str,
    ) -> None:
        now = now_kst_iso()
        session = self.repository.get_chat_session(patient_id, kakao_user_id_hash)
        if session is None:
            session = ChatSession(
                patient_id=patient_id,
                kakao_user_id_hash=kakao_user_id_hash,
                recent_messages=[],
            )
        recent = list(session.recent_messages or [])
        recent.extend(
            [
                {"role": "user", "text": question[:500], "created_at": now},
                {
                    "role": "assistant",
                    "text": f"callback {text_type} status={answer_status}",
                    "status": answer_status,
                    "created_at": now,
                },
            ]
        )
        expires_at = (datetime.now(KST) + timedelta(minutes=getattr(self.settings, "session_ttl_minutes", 1440))).isoformat()
        self.repository.upsert_chat_session(
            session.model_copy(update={"recent_messages": recent[-10:], "updated_at": now, "expires_at": expires_at})
        )

    def _mark_failed(
        self,
        job: KakaoCallbackJob,
        *,
        code: str,
        message: str,
        lock_owner: str,
        keep_retryable: bool,
    ) -> KakaoCallbackJob:
        now = now_kst_iso()
        retryable = keep_retryable and job.retry_count < job.max_attempts
        next_run_at = ""
        if retryable:
            backoff_seconds = self.settings.callback_job_retry_backoff_seconds
            if backoff_seconds > 0:
                next_run_at = (datetime.now(KST) + timedelta(seconds=backoff_seconds)).isoformat()
            else:
                next_run_at = now
        failed = job.model_copy(
            update={
                "status": "FAILED",
                "runnable": retryable,
                "next_run_at": next_run_at,
                "lock_owner": None,
                "lease_expires_at": None,
                "last_failure": RuntimeFailure(
                    code=code,
                    message=self._safe_error_message(message),
                    failed_at=now,
                ),
                "updated_at": now,
            }
        )
        self.repository.upsert_callback_job(failed)
        return failed

    def _mark_expired(self, job: KakaoCallbackJob, *, code: str, message: str) -> KakaoCallbackJob:
        now = now_kst_iso()
        expired = job.model_copy(
            update={
                "status": "EXPIRED",
                "runnable": False,
                "next_run_at": "",
                "lock_owner": None,
                "lease_expires_at": None,
                "last_failure": RuntimeFailure(code=code, message=self._safe_error_message(message), failed_at=now),
                "updated_at": now,
            }
        )
        self.repository.upsert_callback_job(expired)
        return expired

    def _fixed_status_text(self, answer: FinalQaAnswer) -> str:
        return FIXED_STATUS_MESSAGES.get(answer.status, SAFE_FALLBACK_MESSAGE)

    def _hash_text(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _safe_error_message(self, message: str) -> str:
        return " ".join(str(message).split())[:300]

    def _is_expired(self, value: str) -> bool:
        if not value:
            return False
        try:
            return datetime.fromisoformat(value) <= datetime.now(KST)
        except ValueError:
            return True

    def _ensure_processing_budget(self, deadline: float, *, reserve_seconds: float = 0.0) -> None:
        if time.monotonic() + reserve_seconds > deadline:
            raise CallbackProcessingBudgetExceeded("callback processing budget exceeded")
