from __future__ import annotations

import logging
import re

from fastapi import BackgroundTasks

from app.schemas import (
    DocumentRegistryEntry,
    KakaoSkillRequest,
    ModelAnswer,
    PatientDocumentRegistryContext,
    PatientIndexEntry,
)
from app.services.cache import PatientDataCacheService
from app.services.drive import DriveLookupError
from app.services.gemini_qa import GeminiQaError, GeminiQaService, GeminiRecordNotFoundError
from app.services.kakao_callback import (
    KakaoCallbackService,
    build_callback_ack_response,
    build_simple_text_response,
)
from app.sessions import InMemorySessionStore


logger = logging.getLogger(__name__)
AUTH_PREFIX = "인증 "
AUTH_RESET_COMMANDS = {
    "인증 초기화",
    "다른 환자 인증",
    "환자 변경",
    "재인증",
}
BIRTH_RE = re.compile(r"^\d{8}$")
LATEST_RECORD_INTENTS = {
    "최신기록",
    "최신 기록",
    "최신 기록 조회",
    "최신기록조회",
    "💾 최신 기록 조회",
}
USER_STATE_UNMAPPED = "unmapped_user"
USER_STATE_SESSION_ACTIVE = "authenticated_session_active"
USER_STATE_MAPPED_SESSION_EXPIRED = "mapped_but_session_expired"
PATIENT_NOT_FOUND_MESSAGE = (
    "등록된 환자 정보를 찾지 못했습니다.🥲\n"
    "병원에 등록 여부를 확인해 주세요.\n"
    "이름과 생년월일을 다시 확인한 뒤 재시도해 주세요."
)
RAW_PATIENT_NOT_FOUND_ERROR = "등록된 환자 정보를 찾지 못했습니다. 병원에 등록 여부를 확인해 주세요."
LINKED_TO_OTHER_PATIENT_MESSAGE = (
    "이 카카오 계정은 이미 다른 환자 정보에 연결되어 있습니다.\n"
    "다른 사람으로 다시 인증할 수 없습니다.\n"
    "본인 계정으로 접속했는지 확인하거나 병원에 문의해 주세요."
)
RAW_LINKED_TO_OTHER_PATIENT_ERROR = "이미 다른 환자에 연결된 사용자입니다."
AUTH_SUCCESS_MESSAGE = (
    "{patient_name}님 안녕하세요.🙂\n"
    "인증이 완료되었습니다.\n\n"
    "아래 메뉴에서 [💾 최신 기록 조회]을 누르거나,\n"
    '채팅창에 "최신 기록 조회"라고 입력해 주세요.'
)
AUTHENTICATED_START_BLOCK_MESSAGE = (
    "{patient_name}님 안녕하세요.🙂\n"
    "진료 기록 확인이 가능합니다.\n\n"
    "아래 메뉴에서 [💾 최신 기록 조회]을 누르거나,\n"
    '채팅창에 "최신 기록 조회"라고 입력해 주세요.\n\n'
    '진료 기록에 대해 궁금한 점이 있다면 아래 채팅창에 질문을 입력해 주세요.\n\n'
    '🤳다른 환자로 인증하려면 "인증 초기화"라고 입력해 주세요.'
)
INVALID_AUTH_FORMAT_MESSAGE = (
    "인증 형식이 올바르지 않습니다.\n"
    "인증 {이름} {생년월일} 형식으로 입력해 주세요.\n"
    "예: 인증 홍길동 20010330"
)
UNMAPPED_USER_MESSAGE = (
    "개인 의료 기록을 확인하려면 먼저 환자 인증이 필요합니다.\n"
    "아래 형식으로 입력해 주세요.\n"
    "인증 이름 생년월일 (YYYYMMDD)↩️\n\n"
    "예시:\n"
    "인증 홍길동 19890515"
)
MAPPED_BUT_SESSION_EXPIRED_MESSAGE = (
    "{patient_name}님 안녕하세요.🙂\n"
    "진단 기록 확인을 위해 다시 인증이 필요합니다.\n\n"
    "아래 형식으로 입력해 주세요.\n"
    "인증 이름 생년월일 (YYYYMMDD)↩️\n\n"
    "예시:\n"
    "인증 홍길동 19890515\n\n"
    '🤳다른 환자로 인증하려면 "인증 초기화"라고 입력해 주세요.'
)
FREE_QUESTION_FAILURE_MESSAGE = (
    "현재 진단 기록 기반 답변을 생성하지 못했습니다. 잠시 후 다시 시도해 주세요."
)
FORBIDDEN_ANSWER_TOKENS = [
    ".pdf",
    "source_id",
    "fileSearchStores/",
    "drive_file_id",
    "chart_",
    "result_",
    "image_",
    "📄 출처",
]
PRIVACY_KEYWORDS = [
    "주민등록번호",
    "생년월일",
    "환자번호",
    "등록번호",
    "전화번호",
    "휴대폰",
    "이메일",
    "주소",
    "상세주소",
    "우편번호",
    "보호자명",
    "계좌번호",
    "카드번호",
    "보험번호",
]
RESIDENT_REGISTRATION_RE = re.compile(r"\b\d{6}-\d{7}\b")
PHONE_RE = re.compile(r"\b01[016789]-?\d{3,4}-?\d{4}\b|\b0\d{1,2}-\d{3,4}-\d{4}\b")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
EIGHT_DIGIT_DATE_RE = re.compile(r"\b(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\b")
DOCUMENT_TYPE_LABELS = {
    "result": "검사결과지",
    "chart": "진료기록부",
    "image": "영상검사자료",
}
FIXED_STATUS_MESSAGES = {
    "emergency": "🚨 즉시 의료기관을 방문하시길 바랍니다.",
    "blocked": "🔒 개인정보 보호 정책에 따라 성함 이외의 세부 개인정보는 안내해 드리지 않습니다.",
    "full_doc_block": "📑 의료 보안 정책에 따라 문서 전문 출력이 제한되며, 궁금하신 특정 항목에 대해 요약해 드릴 수 있습니다.",
    "cost_block": "💳 비용 관련 정보는 해당 의료기관에 직접 문의하셔야 합니다.",
    "out_of_scope": "💬 의료 기록과 관련된 질문에만 답변을 드릴 수 있습니다.",
    "cannot_verify": "🔍 해당 내용은 제공된 진단 기록에서 확인하기 어렵습니다.",
}
NO_RECORD_MESSAGE = (
    "확인 가능한 최신 진단 기록이 없어 답변드리기 어렵습니다.\n"
    "병원에 기록 등록 여부를 확인해 주세요."
)
RECORD_PREPARING_MESSAGE = (
    "최신 진단 기록을 조회 중입니다.\n"
    "잠시 후 다시 [💾 최신 기록 조회]을 눌러 주세요."
)
AUTH_RESET_MESSAGE = (
    "기존 인증 정보를 초기화했습니다.\n"
    "아래 형식으로 다시 입력해 주세요.\n"
    "인증 이름 생년월일 (YYYYMMDD)↩️\n\n"
    "예시:\n"
    "인증 홍길동 19890515"
)
def simple_text_response(text: str) -> dict:
    return build_simple_text_response(text)


class ChatbotService:
    def __init__(
        self,
        patient_data_cache_service: PatientDataCacheService,
        gemini_qa_service: GeminiQaService | None,
        kakao_callback_service: KakaoCallbackService,
        session_store: InMemorySessionStore,
    ):
        self.patient_data_cache_service = patient_data_cache_service
        self.gemini_qa_service = gemini_qa_service
        self.kakao_callback_service = kakao_callback_service
        self.session_store = session_store

    async def handle_auth_entry(
        self,
        payload: KakaoSkillRequest,
        background_tasks: BackgroundTasks,
    ) -> dict:
        user_id = payload.user_request.user.id
        utterance = payload.user_request.utterance.strip()
        logger.info("auth_entry received user_id=%s", user_id)

        try:
            user_state, patient = self._resolve_user_state(user_id)
        except DriveLookupError:
            return simple_text_response(
                "현재 진료기록 시스템에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요."
            )

        if utterance in AUTH_RESET_COMMANDS:
            return self._handle_auth_reset(user_id, background_tasks)

        if utterance.startswith(AUTH_PREFIX):
            return self._handle_auth(user_id, utterance, background_tasks)

        if user_state == USER_STATE_UNMAPPED or patient is None:
            return simple_text_response(UNMAPPED_USER_MESSAGE)

        if user_state == USER_STATE_MAPPED_SESSION_EXPIRED:
            return simple_text_response(
                MAPPED_BUT_SESSION_EXPIRED_MESSAGE.format(patient_name=patient.name)
            )

        return simple_text_response(
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
        is_latest_record_intent = self._is_latest_record_intent(utterance)
        logger.info(
            "chat_entry received user_id=%s latest_record_intent=%s has_callback=%s",
            user_id,
            is_latest_record_intent,
            callback_url is not None,
        )

        try:
            user_state, patient = self._resolve_user_state(user_id)
        except DriveLookupError:
            return simple_text_response(
                "현재 진료기록 시스템에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요."
            )

        if utterance in AUTH_RESET_COMMANDS:
            return self._handle_auth_reset(user_id, background_tasks)

        if utterance.startswith(AUTH_PREFIX):
            return self._handle_auth(user_id, utterance, background_tasks)

        if user_state == USER_STATE_UNMAPPED or patient is None:
            return simple_text_response(UNMAPPED_USER_MESSAGE)

        if user_state == USER_STATE_MAPPED_SESSION_EXPIRED:
            return simple_text_response(
                MAPPED_BUT_SESSION_EXPIRED_MESSAGE.format(patient_name=patient.name)
            )

        if not is_latest_record_intent:
            return self._handle_free_question(
                patient=patient,
                question=utterance,
                callback_url=callback_url,
                background_tasks=background_tasks,
            )

        return self._handle_latest_record(
            patient=patient,
        )

    def _handle_latest_record(
        self,
        *,
        patient: PatientIndexEntry,
    ) -> dict:
        try:
            context = self.patient_data_cache_service.get_patient_document_context(
                patient=patient,
            )
        except DriveLookupError:
            return simple_text_response(
                "현재 진료기록을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요."
            )
        if not self._has_ready_documents(context):
            if self._has_sync_in_progress_documents(context):
                return simple_text_response(RECORD_PREPARING_MESSAGE)
            return simple_text_response(NO_RECORD_MESSAGE)

        latest_summary_title = self._build_latest_summary_title(context)
        latest_summary_description = self._build_latest_summary_description(context)

        return simple_text_response(
            f"{patient.name}님 안녕하세요.\n"
            f"{latest_summary_title}\n"
            f"{latest_summary_description}\n\n"
            f"👉최신 검사결과지: {context.latest_result.filename if context.latest_result else '없음'}\n"
            f"👉최신 진료기록부: {context.latest_chart.filename if context.latest_chart else '없음'}\n\n"
            "진료 기록에 대해 궁금한 점이 있다면 아래 채팅창에 질문을 입력해 주세요."
        )

    def _handle_free_question(
        self,
        *,
        patient: PatientIndexEntry,
        question: str,
        callback_url: str | None,
        background_tasks: BackgroundTasks,
    ) -> dict:
        if callback_url is None:
            return simple_text_response(FREE_QUESTION_FAILURE_MESSAGE)
        logger.info("free_question_callback queued patient_id=%s", patient.patient_id)
        background_tasks.add_task(
            self._process_free_question_callback,
            patient,
            question,
            callback_url,
        )
        return build_callback_ack_response()

    def _process_free_question_callback(
        self,
        patient: PatientIndexEntry,
        question: str,
        callback_url: str,
    ) -> None:
        logger.info("free_question_callback start patient_id=%s", patient.patient_id)
        try:
            context = self.patient_data_cache_service.get_patient_document_context(
                patient=patient,
            )
            if not self._has_ready_documents(context):
                answer = (
                    RECORD_PREPARING_MESSAGE
                    if self._has_sync_in_progress_documents(context)
                    else NO_RECORD_MESSAGE
                )
            else:
                answer = self._generate_free_question_answer(
                    question=question,
                    context=context,
                )
        except DriveLookupError as exc:
            logger.warning("free_question_callback document lookup failure: %s", str(exc))
            answer = FREE_QUESTION_FAILURE_MESSAGE
        except Exception:
            logger.exception("free_question_callback unexpected failure")
            answer = FREE_QUESTION_FAILURE_MESSAGE

        try:
            self.kakao_callback_service.send_text_response(
                callback_url=callback_url,
                text=answer,
            )
            logger.info("free_question_callback success patient_id=%s", patient.patient_id)
        except Exception:
            logger.exception("free_question_callback send failure")

    def _generate_free_question_answer(
        self,
        *,
        question: str,
        context: PatientDocumentRegistryContext,
    ) -> str:
        if self.gemini_qa_service is None:
            return FREE_QUESTION_FAILURE_MESSAGE
        try:
            qa_answer = self.gemini_qa_service.answer_question(
                question=question,
                context=context,
            )
            return self._validate_and_render_model_answer(
                model_answer=qa_answer.model_answer,
                context=context,
            )
        except GeminiRecordNotFoundError:
            return NO_RECORD_MESSAGE
        except (GeminiQaError, DriveLookupError):
            return FREE_QUESTION_FAILURE_MESSAGE

    def _validate_and_render_model_answer(
        self,
        *,
        model_answer: ModelAnswer,
        context: PatientDocumentRegistryContext,
    ) -> str:
        """Validate structure/safety and assemble the final Kakao message.

        The backend does not reinterpret the user's question or semantically
        reclassify the model-provided status. It only decides whether the model
        response is safe/usable and appends a backend-generated source section
        for ok responses.
        """
        if model_answer.status != "ok":
            return FIXED_STATUS_MESSAGES[model_answer.status]

        if not model_answer.evidence.strip():
            logger.warning("model answer rejected: empty evidence")
            return FIXED_STATUS_MESSAGES["cannot_verify"]

        if not model_answer.kakaotalk_render.strip():
            logger.warning("model answer rejected: empty kakaotalk_render")
            return FIXED_STATUS_MESSAGES["cannot_verify"]

        if self._answer_contains_forbidden_token(model_answer):
            logger.warning("model answer rejected: forbidden token in kakaotalk_render")
            return FIXED_STATUS_MESSAGES["cannot_verify"]

        final_sources = self._validate_final_sources(
            model_answer=model_answer,
            context=context,
        )
        if not final_sources:
            logger.warning("model answer rejected: no valid READY source")
            return FIXED_STATUS_MESSAGES["cannot_verify"]

        return self._build_final_kakao_message(
            model_answer=model_answer,
            sources=final_sources,
        )

    def _validate_final_sources(
        self,
        *,
        model_answer: ModelAnswer,
        context: PatientDocumentRegistryContext,
    ) -> list[DocumentRegistryEntry]:
        ready_documents = [
            item for item in context.documents if item.sync_status == "READY"
        ]
        ready_by_filename = {item.filename: item for item in ready_documents}
        model_sources = dedupe_preserve_order(model_answer.used_source_ids)
        if not model_sources:
            return []

        return [
            ready_by_filename[source_id]
            for source_id in model_sources
            if source_id in ready_by_filename
        ]

    def _answer_contains_forbidden_token(self, model_answer: ModelAnswer) -> bool:
        return any(
            token in model_answer.kakaotalk_render
            for token in FORBIDDEN_ANSWER_TOKENS
        )

    def _build_final_kakao_message(
        self,
        *,
        model_answer: ModelAnswer,
        sources: list[DocumentRegistryEntry],
    ) -> str:
        body = model_answer.kakaotalk_render.strip()
        source_section = self._render_source_section(sources)
        if not source_section:
            return FIXED_STATUS_MESSAGES["cannot_verify"]
        return f"{body}\n\n{source_section}"

    def _render_source_section(self, sources: list[DocumentRegistryEntry]) -> str:
        source_lines = []
        for index, source in enumerate(sources, start=1):
            line = (
                f"{index}. {self._document_type_label(source.document_type)}, "
                f"{self._format_document_date(source.document_date)}"
            )
            if source.filename:
                line += f" ({source.filename})"
            source_lines.append(line)

        if not source_lines:
            return ""

        source_header = f"📄 출처 ({len(source_lines)}건)"
        return source_header + "\n" + "\n".join(source_lines)

    def _handle_auth(
        self,
        user_id: str,
        utterance: str,
        background_tasks: BackgroundTasks,
    ) -> dict:
        auth_tokens = utterance.split()
        if len(auth_tokens) != 3:
            return simple_text_response(INVALID_AUTH_FORMAT_MESSAGE)

        _, name, birth = auth_tokens
        if BIRTH_RE.fullmatch(birth) is None:
            return simple_text_response(INVALID_AUTH_FORMAT_MESSAGE)

        try:
            auth_result = self.patient_data_cache_service.authenticate_and_map_patient(
                kakao_user_id=user_id,
                name=name,
                birth=birth,
            )
        except DriveLookupError as exc:
            if str(exc) == RAW_PATIENT_NOT_FOUND_ERROR:
                return simple_text_response(PATIENT_NOT_FOUND_MESSAGE)
            if str(exc) == RAW_LINKED_TO_OTHER_PATIENT_ERROR:
                return simple_text_response(LINKED_TO_OTHER_PATIENT_MESSAGE)
            return simple_text_response(str(exc))

        patient = auth_result.patient
        self.session_store.set(user_id, patient)
        if auth_result.persistence_required:
            background_tasks.add_task(self._persist_patient_index)
        return simple_text_response(AUTH_SUCCESS_MESSAGE.format(patient_name=patient.name))

    def _handle_auth_reset(
        self,
        user_id: str,
        background_tasks: BackgroundTasks,
    ) -> dict:
        changed = self.patient_data_cache_service.reset_kakao_user_id(kakao_user_id=user_id)
        self.session_store.delete(user_id)
        if changed:
            background_tasks.add_task(self._persist_patient_index)
        return simple_text_response(AUTH_RESET_MESSAGE)

    def _resolve_user_state(
        self,
        user_id: str,
    ) -> tuple[str, PatientIndexEntry | None]:
        session_patient = self.session_store.get(user_id)
        if session_patient is not None:
            return USER_STATE_SESSION_ACTIVE, session_patient

        mapped_patient = self.patient_data_cache_service.get_patient_by_kakao_user_id(user_id)
        if mapped_patient is None:
            return USER_STATE_UNMAPPED, None
        return USER_STATE_MAPPED_SESSION_EXPIRED, mapped_patient

    def _persist_patient_index(self) -> None:
        try:
            self.patient_data_cache_service.persist_patient_index()
            logger.info("patient_index persist success")
        except Exception:
            logger.exception("patient_index persist failure")

    def _is_latest_record_intent(self, utterance: str) -> bool:
        normalized = utterance.replace(" ", "").replace("💾", "")
        return utterance in LATEST_RECORD_INTENTS or normalized in {
            "최신기록",
            "최신기록보여줘",
        }

    def _has_ready_documents(self, context: PatientDocumentRegistryContext) -> bool:
        return any(item.sync_status == "READY" for item in context.documents)

    def _has_sync_in_progress_documents(self, context: PatientDocumentRegistryContext) -> bool:
        return any(
            item.sync_status in {"PENDING", "INDEXING", "STALE"}
            for item in context.documents
        )

    def _build_latest_summary_title(self, context: PatientDocumentRegistryContext) -> str:
        latest_date = max(
            (
                item.document_date
                for item in (context.latest_result, context.latest_chart)
                if item is not None and item.document_date
            ),
            default="",
        )
        if not latest_date:
            return "최신 진단 기록"
        return (
            f"{latest_date[:4]}년 {int(latest_date[4:6])}월 "
            f"{int(latest_date[6:8])}일 진료 및 검사 기록"
        )

    def _build_latest_summary_description(
        self,
        context: PatientDocumentRegistryContext,
    ) -> str:
        has_result = context.latest_result is not None
        has_chart = context.latest_chart is not None
        if has_result and has_chart:
            return "최근 검사결과지와 진료기록부가 등록되어 있습니다."
        if has_result:
            return "최근 검사결과지가 등록되어 있습니다."
        if has_chart:
            return "최근 진료기록부가 등록되어 있습니다."
        return "최근 진료 기록이 등록되어 있습니다."

    def _document_type_label(self, document_type: str) -> str:
        return DOCUMENT_TYPE_LABELS.get(document_type, document_type or "문서")

    def _format_document_date(self, document_date: str) -> str:
        if len(document_date) != 8 or not document_date.isdigit():
            return "날짜 미상"
        return (
            f"{document_date[:4]}년 {int(document_date[4:6])}월 "
            f"{int(document_date[6:8])}일"
        )


def dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result