from __future__ import annotations

import logging
import hashlib
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING
from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks

from app.config import Settings
from app.medical_wiki_categories import medical_wiki_category_label
from app.repositories import MedicalRepository
from app.schemas import (
    ChatLog,
    ChatSession,
    KakaoCallbackJob,
    KakaoSkillRequest,
    KakaoUserMapping,
    PatientProfile,
    RuntimeFailure,
)
from app.services.callback_tasks import CallbackTaskEnqueueService
from app.services.gemini_file_prewarm import GeminiFilePrewarmEnqueueService
from app.services.kakao_callback import build_callback_ack_response, build_simple_text_response
from app.services.medical_wiki import format_medical_source_display_name, now_kst_iso
from app.services.patient_identity import normalize_patient_birth, normalize_patient_name
from app.services.security import hash_kakao_user_id
from app.services.temporary_attachments import (
    TemporaryAttachmentConfigError,
    TemporaryAttachmentService,
)



if TYPE_CHECKING:
    from app.services.callback_jobs import CallbackJobProcessor

logger = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))
AUTH_PREFIX = "인증 "


def _command_key(value: str) -> str:
    return "".join(value.split()).casefold()


AUTH_RESET_COMMAND_KEYS = frozenset(
    {
        "인증초기화",
        "환자초기화",
        "다른환자인증",
        "환자변경",
        "재인증",
    }
)
LATEST_RECORD_INTENTS = {
    "기록",
    "진단기록",
    "진료기록",
    "진단 기록",
    "진료 기록",
    "진단 기록 조회",
    "진료 기록 조회",
    "검사 기록",
    "검사기록",
    "기록 조회",
    "기록조회",
    "진단기록 조회",
    "진료기록 조회",
    "의료기록",
    "의료 기록",
    "의료 기록 조회",
    "의료기록조회",
    "🔍 의료 기록 조회",
}
UNMAPPED_USER_MESSAGE = (
    "개인 의료 기록을 확인하려면 먼저 환자 인증이 필요합니다.\n"
    "아래 형식으로 입력해 주세요.\n"
    "인증 이름 생년월일 (YYYYMMDD)↩️\n\n"
    "예시:\n"
    "인증 홍길동 19890515"
)
INVALID_AUTH_FORMAT_MESSAGE = (
    "인증 형식이 올바르지 않습니다.\n"
    "인증 {이름} {생년월일} 형식으로 입력해 주세요.\n"
    "예: 인증 홍길동 20010330"
)
PATIENT_NOT_FOUND_MESSAGE = (
    "등록된 환자 정보를 찾지 못했습니다.🥲\n"
    "병원에 등록 여부를 확인해 주세요.\n"
    "이름과 생년월일을 다시 확인한 뒤 재시도해 주세요."
)
AUTH_SUCCESS_MESSAGE = (
    "{patient_name}님 안녕하세요.🙂\n"
    "인증이 완료되었습니다.\n\n"
    "아래 메뉴에서 [🔍 의료 기록 조회]을 누르거나,\n"
    '채팅창에 "의료 기록 조회"라고 입력해 주세요.\n\n'
    "기존 등록 진료기록 외에\n"
    '별도의 PDF나 이미지 파일 질문하고 싶다면 [📎 파일 업로드]을 누르거나 "파일 업로드"라고 입력해 주세요.'
)
AUTHENTICATED_START_BLOCK_MESSAGE = (
    "{patient_name}님 안녕하세요.🙂\n"
    "진료 기록 확인이 가능합니다.\n\n"
    "아래 메뉴에서 [🔍 의료 기록 조회]을 누르거나,\n"
    '채팅창에 "의료 기록 조회"라고 입력해 주세요.\n\n'
    "기존 등록 진료기록 외에\n"
    '별도의 PDF나 이미지 파일 질문하고 싶다면 [📎 파일 업로드]을 누르거나 "파일 업로드"라고 입력해 주세요.\n\n'
    "진료 기록에 대해 궁금한 점이 있다면 아래 채팅창에 질문을 입력해 주세요.\n\n"
    '🤳다른 환자로 인증하려면 "인증 초기화"라고 입력해 주세요.'
)
AUTH_RESET_MESSAGE = (
    "기존 인증 정보를 초기화했습니다.\n"
    "아래 형식으로 다시 입력해 주세요.\n"
    "인증 이름 생년월일 (YYYYMMDD)↩️\n\n"
    "예시:\n"
    "인증 홍길동 19890515"
)
SYSTEM_PREPARING_MESSAGE = "관련 문서를 찾아 준비 중입니다.\n잠시 후 다시 시도해 주세요.🙇‍♂️"
NO_RECORD_MESSAGE = (
    "😥등록된 환자 정보를 찾지 못했습니다.\n"
    "병원에 기록 등록 여부를 확인해 주세요."
)
RECORD_PREPARING_MESSAGE = (
    "의료 기록을 조회 중입니다.\n"
    "잠시 후 다시 [🔍 의료 기록 조회]을 눌러 주세요."
)
CALLBACK_UNAVAILABLE_MESSAGE = (
    "현재 답변 서비스 이용이 원활하지 않습니다.\n잠시 후 다시 시도해 주세요.🙇‍♂️ "
)
UPLOAD_COMMAND_KEYS = frozenset(
    {
        "파일업로드",
        "pdf업로드",
        "이미지업로드",
        "사진업로드",
    }
)
UPLOAD_CONFIG_UNAVAILABLE_MESSAGE = "현재 파일 업로드 기능을 사용할 수 없습니다.\n잠시 후 다시 시도해 주세요.🙇‍♂️"
UPLOAD_LINK_LOG_MESSAGE = "업로드 링크를 발급했습니다."
UPLOAD_ATTACHMENT_MISSING_MESSAGE = (
    "최근 업로드한 파일을 찾지 못했습니다.\n"
    '먼저 "파일 업로드"라고 입력해 업로드 링크를 받은 뒤 파일을 올려 주세요.'
)


def _matches_command(value: str, command_keys: frozenset[str]) -> bool:
    return _command_key(value) in command_keys


@dataclass
class FirestoreChatbotService:
    settings: Settings
    repository: MedicalRepository
    callback_job_processor: "CallbackJobProcessor | None" = None
    callback_task_enqueue_service: CallbackTaskEnqueueService | None = None
    prewarm_enqueue_service: GeminiFilePrewarmEnqueueService | None = None
    temporary_attachment_service: TemporaryAttachmentService | None = None
    session_ttl_minutes: int = 1440

    async def handle_auth_entry(
        self,
        payload: KakaoSkillRequest,
        background_tasks: BackgroundTasks,
    ) -> dict:
        del background_tasks
        user_id = payload.user_request.user.id
        utterance = payload.user_request.utterance.strip()
        kakao_user_id_hash = hash_kakao_user_id(user_id)

        if self._is_auth_reset_command(utterance):
            self._reset_auth(kakao_user_id_hash)
            return build_simple_text_response(AUTH_RESET_MESSAGE)

        if utterance.startswith(AUTH_PREFIX):
            return self._handle_auth(kakao_user_id_hash=kakao_user_id_hash, utterance=utterance)

        patient = self._resolve_patient(kakao_user_id_hash)
        if patient is None:
            return build_simple_text_response(UNMAPPED_USER_MESSAGE)
        if self._is_upload_command(utterance):
            answer_text, _ = self._build_upload_link_response(
                patient_id=patient.patient_id,
                kakao_user_id_hash=kakao_user_id_hash,
            )
            return build_simple_text_response(answer_text)
        self._try_enqueue_prewarm(patient_id=patient.patient_id, reason="authenticated_start")
        return build_simple_text_response(
            AUTHENTICATED_START_BLOCK_MESSAGE.format(patient_name=patient.name)
        )

    async def handle_chat(
        self,
        payload: KakaoSkillRequest,
        background_tasks: BackgroundTasks,
    ) -> dict:
        user_id = payload.user_request.user.id
        utterance = payload.user_request.utterance.strip()
        callback_url = payload.user_request.callback_url
        kakao_user_id_hash = hash_kakao_user_id(user_id)

        if self._is_auth_reset_command(utterance):
            self._reset_auth(kakao_user_id_hash)
            return build_simple_text_response(AUTH_RESET_MESSAGE)
        if utterance.startswith(AUTH_PREFIX):
            return self._handle_auth(kakao_user_id_hash=kakao_user_id_hash, utterance=utterance)

        patient = self._resolve_patient(kakao_user_id_hash)
        if patient is None:
            return build_simple_text_response(UNMAPPED_USER_MESSAGE)

        chat_log_id = self._create_chat_log(
            patient=patient,
            kakao_user_id_hash=kakao_user_id_hash,
            message=utterance,
            message_type="question",
        )

        if self._is_upload_command(utterance):
            answer_text, log_text = self._build_upload_link_response(
                patient_id=patient.patient_id,
                kakao_user_id_hash=kakao_user_id_hash,
            )
            self._create_assistant_chat_log(
                patient=patient,
                kakao_user_id_hash=kakao_user_id_hash,
                message=log_text,
                message_type="system",
                related_log_id=chat_log_id,
            )
            return build_simple_text_response(answer_text)

        if self._is_latest_record_intent(utterance):
            self._try_enqueue_prewarm(patient_id=patient.patient_id, reason="medical_record_lookup")
            answer_text = self._latest_record_message(patient.patient_id)
            self._create_assistant_chat_log(
                patient=patient,
                kakao_user_id_hash=kakao_user_id_hash,
                message=answer_text,
                message_type="system",
                related_log_id=chat_log_id,
            )
            return build_simple_text_response(answer_text)

        answer_route = "drive"
        attachment_id = ""
        if self._is_temporary_attachment_reference(utterance):
            attachment = (
                self.temporary_attachment_service.get_current_attachment(
                    patient_id=patient.patient_id,
                    kakao_user_id_hash=kakao_user_id_hash,
                )
                if self.temporary_attachment_service is not None
                else None
            )
            if attachment is None:
                self._create_assistant_chat_log(
                    patient=patient,
                    kakao_user_id_hash=kakao_user_id_hash,
                    message=UPLOAD_ATTACHMENT_MISSING_MESSAGE,
                    message_type="system",
                    related_log_id=chat_log_id,
                )
                return build_simple_text_response(UPLOAD_ATTACHMENT_MISSING_MESSAGE)
            answer_route = "temporary_attachment"
            attachment_id = attachment.attachment_id

        if not callback_url:
            self._create_assistant_chat_log(
                patient=patient,
                kakao_user_id_hash=kakao_user_id_hash,
                message=CALLBACK_UNAVAILABLE_MESSAGE,
                message_type="system",
                related_log_id=chat_log_id,
            )
            return build_simple_text_response(CALLBACK_UNAVAILABLE_MESSAGE)

        now = datetime.now(KST)
        idempotency_key = self._callback_idempotency_key(
            patient_id=patient.patient_id,
            kakao_user_id_hash=kakao_user_id_hash,
            utterance=utterance,
            callback_url=callback_url,
            now=now,
            answer_route=answer_route,
            attachment_id=attachment_id,
        )
        existing_job = self.repository.get_callback_job_by_idempotency_key(idempotency_key)
        if existing_job is not None and existing_job.status in {"PENDING", "PROCESSING"}:
            self.repository.update_chat_log_job(patient.patient_id, chat_log_id, existing_job.job_id)
            return build_callback_ack_response()

        job_id = f"JOB_{uuid.uuid4().hex[:12].upper()}"
        job = KakaoCallbackJob(
            job_id=job_id,
            patient_id=patient.patient_id,
            kakao_user_id_hash=kakao_user_id_hash,
            chat_log_id=chat_log_id,
            callback_url=callback_url,
            answer_route=answer_route,  # type: ignore[arg-type]
            attachment_id=attachment_id,
            idempotency_key=idempotency_key,
            max_attempts=self.settings.callback_job_max_attempts,
            runnable=True,
            next_run_at=now_kst_iso(),
            created_at=now_kst_iso(),
            updated_at=now_kst_iso(),
            expires_at=(now + timedelta(minutes=self.settings.callback_job_expiry_minutes)).isoformat(),
        )
        self.repository.create_callback_job(job)
        self.repository.update_chat_log_job(patient.patient_id, chat_log_id, job_id)

        if not self._dispatch_callback_job(job=job, background_tasks=background_tasks):
            self._create_assistant_chat_log(
                patient=patient,
                kakao_user_id_hash=kakao_user_id_hash,
                message=CALLBACK_UNAVAILABLE_MESSAGE,
                message_type="system",
                related_log_id=chat_log_id,
                job_id=job_id,
            )
            return build_simple_text_response(CALLBACK_UNAVAILABLE_MESSAGE)
        return build_callback_ack_response()

    def _try_enqueue_prewarm(self, *, patient_id: str, reason: str) -> None:
        """Best-effort latency optimization. Failures must not affect user-facing responses."""
        if self.prewarm_enqueue_service is None:
            return
        try:
            result = self.prewarm_enqueue_service.enqueue_latest_source(
                patient_id=patient_id,
                reason=reason,
            )
            logger.info(
                "prewarm enqueue patient_id=%s reason=%s job_id=%s source_id=%s status=%s enqueued=%s duplicate=%s",
                patient_id,
                reason,
                result.job_id,
                result.source_id,
                result.status,
                result.enqueued,
                result.duplicate,
            )
        except Exception:
            logger.exception("prewarm enqueue failed patient_id=%s reason=%s", patient_id, reason)

    def _dispatch_callback_job(self, *, job: KakaoCallbackJob, background_tasks: BackgroundTasks) -> bool:
        """Dispatch the callback job according to CALLBACK_WORKER_MODE.

        Cloud Tasks only policy:
        - /kakao/chat stays lightweight because it does not initialize CallbackJobProcessor.
        - Cloud Tasks enqueue is still done synchronously before returning useCallback=True.
        - If enqueue fails, the user receives a normal simpleText error instead of a callback ack
          that cannot be fulfilled.
        """
        job_id = job.job_id
        if self.settings.callback_worker_mode == "cloud_tasks":
            return self._enqueue_callback_task(job)

        if self.settings.callback_worker_mode == "background":
            if self.callback_job_processor is None:
                logger.error("callback background processor unavailable job_id=%s", job_id)
                self._mark_callback_dispatch_failed(
                    job,
                    code="CALLBACK_BACKGROUND_PROCESSOR_UNAVAILABLE",
                    message="callback background processor is not configured",
                )
                return False
            background_tasks.add_task(self.callback_job_processor.process_job, job_id)
            return True

        logger.info("callback job queued for polling job_id=%s", job_id)
        return True

    def _enqueue_callback_task(self, job: KakaoCallbackJob) -> bool:
        job_id = job.job_id
        if self.callback_task_enqueue_service is None:
            logger.error("callback cloud task enqueue service unavailable job_id=%s", job_id)
            self._mark_callback_dispatch_failed(
                job,
                code="CALLBACK_TASK_ENQUEUE_SERVICE_UNAVAILABLE",
                message="callback task enqueue service is not configured",
            )
            return False
        try:
            result = self.callback_task_enqueue_service.enqueue_callback_job(job_id=job_id)
            logger.info(
                "callback cloud task dispatch job_id=%s task_name=%s duplicate=%s",
                job_id,
                result.task_name,
                result.duplicate,
            )
            return True
        except Exception as exc:
            logger.exception("callback cloud task enqueue failed job_id=%s", job_id)
            self._mark_callback_dispatch_failed(
                job,
                code="CALLBACK_TASK_ENQUEUE_FAILED",
                message=str(exc),
            )
            return False

    def _mark_callback_dispatch_failed(self, job: KakaoCallbackJob, *, code: str, message: str) -> None:
        self.repository.upsert_callback_job(
            job.model_copy(
                update={
                    "status": "FAILED",
                    "runnable": False,
                    "next_run_at": "",
                    "last_failure": RuntimeFailure(
                        code=code,
                        message=" ".join(str(message).split())[:300],
                        failed_at=now_kst_iso(),
                    ),
                    "updated_at": now_kst_iso(),
                }
            )
        )

    def _callback_idempotency_key(
        self,
        *,
        patient_id: str,
        kakao_user_id_hash: str,
        utterance: str,
        callback_url: str,
        now: datetime,
        answer_route: str = "drive",
        attachment_id: str = "",
    ) -> str:
        normalized_utterance = " ".join(utterance.split()).casefold()
        callback_hash = hashlib.sha256(callback_url.encode("utf-8")).hexdigest()[:16]
        utterance_hash = hashlib.sha256(normalized_utterance.encode("utf-8")).hexdigest()[:16]
        minute_bucket = now.strftime("%Y%m%d%H%M")
        return ":".join(
            [
                patient_id,
                kakao_user_id_hash,
                utterance_hash,
                callback_hash,
                minute_bucket,
                answer_route,
                attachment_id,
            ]
        )

    def _build_upload_link_response(self, *, patient_id: str, kakao_user_id_hash: str) -> tuple[str, str]:
        if self.temporary_attachment_service is None:
            return UPLOAD_CONFIG_UNAVAILABLE_MESSAGE, UPLOAD_CONFIG_UNAVAILABLE_MESSAGE
        try:
            link = self.temporary_attachment_service.create_upload_link(
                patient_id=patient_id,
                kakao_user_id_hash=kakao_user_id_hash,
            )
        except TemporaryAttachmentConfigError:
            logger.warning("temporary upload is not configured patient_id=%s", patient_id)
            return UPLOAD_CONFIG_UNAVAILABLE_MESSAGE, UPLOAD_CONFIG_UNAVAILABLE_MESSAGE
        except Exception:
            logger.exception("temporary upload link creation failed patient_id=%s", patient_id)
            return UPLOAD_CONFIG_UNAVAILABLE_MESSAGE, UPLOAD_CONFIG_UNAVAILABLE_MESSAGE
        text = (
            "아래 링크에서 PDF 또는 이미지를 1개 업로드해 주세요.\n"
            f"{link.url}\n\n"
            f"링크는 {self.settings.upload_token_ttl_minutes}분 동안 사용할 수 있습니다."
        )
        return text, UPLOAD_LINK_LOG_MESSAGE

    def _handle_auth(self, *, kakao_user_id_hash: str, utterance: str) -> dict:
        tokens = utterance.split()
        if len(tokens) != 3:
            return build_simple_text_response(INVALID_AUTH_FORMAT_MESSAGE)
        _, raw_name, raw_birth = tokens
        name = normalize_patient_name(raw_name)
        birth = normalize_patient_birth(raw_birth)
        if len(birth) != 8 or not birth.isdigit():
            return build_simple_text_response(INVALID_AUTH_FORMAT_MESSAGE)
        patient = self.repository.find_active_patient_by_identity(name=name, birth=birth)
        if patient is None:
            return build_simple_text_response(PATIENT_NOT_FOUND_MESSAGE)
        now = datetime.now(KST)
        mapping = KakaoUserMapping(
            kakao_user_id_hash=kakao_user_id_hash,
            patient_id=patient.patient_id,
            authenticated_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=self.session_ttl_minutes)).isoformat(),
            status="active",
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
        self.repository.upsert_kakao_mapping(mapping)
        self.repository.upsert_chat_session(
            ChatSession(
                patient_id=patient.patient_id,
                kakao_user_id_hash=kakao_user_id_hash,
                recent_messages=[],
                updated_at=now.isoformat(),
                expires_at=(now + timedelta(minutes=self.session_ttl_minutes)).isoformat(),
            )
        )
        self._try_enqueue_prewarm(patient_id=patient.patient_id, reason="auth_success")
        return build_simple_text_response(AUTH_SUCCESS_MESSAGE.format(patient_name=patient.name))

    def _resolve_patient(self, kakao_user_id_hash: str) -> PatientProfile | None:
        mapping = self.repository.get_kakao_mapping(kakao_user_id_hash)
        if mapping is None or mapping.status != "active":
            return None
        if self._is_expired(mapping.expires_at):
            return None
        patient = self.repository.get_patient(mapping.patient_id)
        if patient is None or patient.status != "active":
            return None
        return patient

    def _reset_auth(self, kakao_user_id_hash: str) -> None:
        mapping = self.repository.get_kakao_mapping(kakao_user_id_hash)
        if mapping is not None:
            self.repository.delete_chat_session(mapping.patient_id, kakao_user_id_hash)
        self.repository.delete_kakao_mapping(kakao_user_id_hash)

    def _create_chat_log(
        self,
        *,
        patient: PatientProfile,
        kakao_user_id_hash: str,
        message: str,
        message_type: str,
    ) -> str:
        log_id = f"LOG_{uuid.uuid4().hex[:12].upper()}"
        self.repository.create_chat_log(
            ChatLog(
                log_id=log_id,
                patient_id=patient.patient_id,
                kakao_user_id_hash=kakao_user_id_hash,
                message=message,
                message_type=message_type,
                created_at=now_kst_iso(),
            )
        )
        return log_id

    def _create_assistant_chat_log(
        self,
        *,
        patient: PatientProfile,
        kakao_user_id_hash: str,
        message: str,
        message_type: str,
        related_log_id: str,
        job_id: str | None = None,
    ) -> str:
        log_id = f"ANSWER_{job_id}" if job_id else f"ANSWER_{related_log_id}"
        self.repository.create_chat_log(
            ChatLog(
                log_id=log_id,
                patient_id=patient.patient_id,
                kakao_user_id_hash=kakao_user_id_hash,
                role="assistant",
                message=message,
                message_type=message_type,
                job_id=job_id,
                created_at=now_kst_iso(),
            )
        )
        return log_id

    def _latest_record_message(self, patient_id: str) -> str:
        patient = self.repository.get_patient(patient_id)
        patient_name = patient.name if patient is not None else "환자"

        wiki_index = self.repository.get_wiki_index(patient_id)
        if wiki_index is None:
            return RECORD_PREPARING_MESSAGE
        if not wiki_index.pages:
            return NO_RECORD_MESSAGE

        dated_pages = [page for page in wiki_index.pages if page.date]
        latest_page = max(
            dated_pages,
            key=lambda page: page.date,
            default=wiki_index.pages[0],
        )

        latest_date = latest_page.date or ""
        if len(latest_date) >= 10:
            title = (
                f"{latest_date[:4]}년 {int(latest_date[5:7])}월 "
                f"{int(latest_date[8:10])}일 진료 및 검사 기록"
            )
        elif len(latest_date) >= 7:
            title = f"{latest_date[:4]}년 {int(latest_date[5:7])}월 진료 및 검사 기록"
        else:
            title = "최신 진단 기록"

        latest_category = medical_wiki_category_label(latest_page.category)
        latest_display = format_medical_source_display_name(
            category=latest_page.category,
            date=latest_page.date,
            page_count=None,
        )

        return (
            f"{patient_name}님 안녕하세요.\n"
            f"{title}\n"
            f"등록된 의료 문서 {len(wiki_index.pages)}건을 확인했습니다.\n\n"
            f"👉최근 확인 가능한 문서: {latest_display}\n"
            f"👉문서 유형: {latest_category}\n\n"
            "진료 기록에 대해 궁금한 점이 있다면 아래 채팅창에 질문을 입력해 주세요."
        )

    def _is_latest_record_intent(self, utterance: str) -> bool:
        stripped = utterance.strip()
        normalized = stripped.replace(" ", "").replace("🔍", "").replace("[", "").replace("]", "")
        return stripped in LATEST_RECORD_INTENTS or normalized in {
            "기록",
            "기록조회",
            "진단기록",
            "진단기록조회",
            "진료기록",
            "진료기록조회",
            "검사기록",
            "의료기록",
            "의료기록조회",
            "의료기록보여줘",
        }

    def _is_upload_command(self, utterance: str) -> bool:
        return _matches_command(utterance, UPLOAD_COMMAND_KEYS)

    def _is_auth_reset_command(self, utterance: str) -> bool:
        return _matches_command(utterance, AUTH_RESET_COMMAND_KEYS)

    def _is_temporary_attachment_reference(self, utterance: str) -> bool:
        normalized = utterance.replace(" ", "").casefold()
        phrases = [
            "방금파일",
            "방금올린파일",
            "이파일",
            "업로드한파일",
            "업로드파일",
            "첨부한파일",
            "첨부파일",
            "이pdf",
            "올린pdf",
            "이이미지",
            "올린이미지",
        ]
        return any(phrase in normalized for phrase in phrases)

    def _is_expired(self, value: str) -> bool:
        if not value:
            return True
        try:
            return datetime.fromisoformat(value) <= datetime.now(KST)
        except ValueError:
            return True
