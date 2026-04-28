from __future__ import annotations

import logging

from fastapi import BackgroundTasks, FastAPI, Request

from app.config import Settings, get_settings
from app.schemas import KakaoSkillRequest
from app.services.cache import PatientDataCacheService
from app.services.chatbot import ChatbotService
from app.services.drive import DriveLookupService, build_default_drive_service
from app.services.gemini_qa import GeminiQaService, build_default_gemini_qa_service
from app.services.kakao_callback import KakaoCallbackService
from app.sessions import InMemorySessionStore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    drive_service: DriveLookupService | None = None,
    gemini_qa_service: GeminiQaService | None = None,
    patient_data_cache_service: PatientDataCacheService | None = None,
    kakao_callback_service: KakaoCallbackService | None = None,
    session_store: InMemorySessionStore | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()

    app = FastAPI(
        title="Medical Chatbot PoC",
        version="0.1.0",
    )

    app.state.settings = app_settings
    app.state.drive_service = drive_service
    app.state.gemini_qa_service = gemini_qa_service
    app.state.patient_data_cache_service = patient_data_cache_service
    app.state.kakao_callback_service = kakao_callback_service or KakaoCallbackService()
    app.state.session_store = session_store or InMemorySessionStore(
        ttl_minutes=app_settings.session_ttl_minutes
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/debug/drive")
    async def debug_drive(
        request: Request,
        kakao_user_id: str | None = None,
        name: str | None = None,
        birth: str | None = None,
    ) -> dict:
        drive_service = _get_or_init_drive_service(request)
        if drive_service is None:
            return {
                "ok": False,
                "error": "현재 진료기록 시스템에 연결하지 못했습니다.",
            }
        return drive_service.debug_probe(
            kakao_user_id=kakao_user_id,
            name=name,
            birth=birth,
        )

    @app.post("/kakao/auth")
    async def kakao_auth(
        payload: KakaoSkillRequest,
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> dict:
        drive_service = _get_or_init_drive_service(request)
        if drive_service is None:
            return {
                "version": "2.0",
                "template": {
                    "outputs": [
                        {
                            "simpleText": {
                                "text": "현재 진료기록 시스템에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요."
                            }
                        }
                    ]
                },
            }
        patient_data_cache_service = _get_or_init_patient_data_cache_service(request)
        chatbot = ChatbotService(
            patient_data_cache_service=patient_data_cache_service,
            gemini_qa_service=None,
            kakao_callback_service=request.app.state.kakao_callback_service,
            session_store=request.app.state.session_store,
        )
        return await chatbot.handle_auth_entry(payload, background_tasks)

    @app.post("/kakao/chat")
    async def kakao_chat(
        payload: KakaoSkillRequest,
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> dict:
        drive_service = _get_or_init_drive_service(request)
        if drive_service is None:
            return {
                "version": "2.0",
                "template": {
                    "outputs": [
                        {
                            "simpleText": {
                                "text": "현재 진료기록 시스템에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요."
                            }
                        }
                    ]
                },
            }
        patient_data_cache_service = _get_or_init_patient_data_cache_service(request)
        chatbot = ChatbotService(
            patient_data_cache_service=patient_data_cache_service,
            gemini_qa_service=_get_or_init_gemini_qa_service(request),
            kakao_callback_service=request.app.state.kakao_callback_service,
            session_store=request.app.state.session_store,
        )
        return await chatbot.handle_chat(payload, background_tasks)

    return app


def _get_or_init_drive_service(request: Request) -> DriveLookupService | None:
    if request.app.state.drive_service is not None:
        return request.app.state.drive_service
    try:
        request.app.state.drive_service = build_default_drive_service(
            request.app.state.settings
        )
    except Exception:
        logger.exception("Failed to initialize Google Drive service")
        return None
    return request.app.state.drive_service


def _get_or_init_gemini_qa_service(request: Request) -> GeminiQaService | None:
    if request.app.state.gemini_qa_service is not None:
        return request.app.state.gemini_qa_service
    try:
        request.app.state.gemini_qa_service = build_default_gemini_qa_service(
            request.app.state.settings
        )
    except Exception:
        logger.exception("Failed to initialize Gemini QA service")
        return None
    return request.app.state.gemini_qa_service


def _get_or_init_patient_data_cache_service(request: Request) -> PatientDataCacheService:
    if request.app.state.patient_data_cache_service is not None:
        return request.app.state.patient_data_cache_service
    drive_service = _get_or_init_drive_service(request)
    assert drive_service is not None
    request.app.state.patient_data_cache_service = PatientDataCacheService(
        settings=request.app.state.settings,
        drive_service=drive_service,
    )
    return request.app.state.patient_data_cache_service


app = create_app()
