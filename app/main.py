from __future__ import annotations

import logging
import os

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from app.config import Settings, get_settings
from app.repositories import MedicalRepository, build_default_medical_repository
from app.schemas import KakaoSkillRequest
from app.services.callback_jobs import CallbackJobProcessor
from app.services.callback_tasks import CallbackTaskEnqueueService
from app.services.cloud_tasks import CloudTasksClient
from app.services.drive import DriveGateway, DriveLookupService, build_default_drive_service
from app.services.drive_sync import DriveChangesSyncService
from app.services.firestore_chatbot import FirestoreChatbotService
from app.services.gemini_files_qa import (
    GeminiFilesQaService,
    MedicalRouterService,
    build_default_gemini_files_qa_service,
)
from app.services.gemini_files_cleanup import GeminiFilesCleanupService
from app.services.gemini_file_prewarm import (
    GeminiFilePrewarmEnqueueService,
    GeminiFilePrewarmRetryableError,
    GeminiFilePrewarmService,
)
from app.services.kakao_callback import KakaoCallbackService
from app.services.wiki_rebuild_tasks import WikiRebuildTaskEnqueueService
from app.services.admin_auth import is_admin_request
from app.services.medical_wiki import MedicalWikiService
from app.services.medical_wiki_extractor import (
    MedicalWikiExtractor,
    build_default_medical_wiki_extractor,
)
from app.services.drive_bootstrap import PatientDriveBootstrapService

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    drive_service: DriveLookupService | None = None,
    medical_repository: MedicalRepository | None = None,
    gemini_files_qa_service: GeminiFilesQaService | None = None,
    kakao_callback_service: KakaoCallbackService | None = None,
) -> FastAPI:
    app_settings = settings or get_settings()

    app = FastAPI(
        title="Medical Chatbot PoC",
        version="0.1.0",
    )

    app.state.settings = app_settings
    app.state.drive_service = drive_service
    app.state.gemini_files_qa_service = gemini_files_qa_service
    app.state.gemini_files_cleanup_service = None
    app.state.callback_task_enqueue_service = None
    app.state.callback_job_processor = None
    app.state.gemini_file_prewarm_service = None
    app.state.gemini_file_prewarm_enqueue_service = None
    app.state.wiki_rebuild_task_enqueue_service = None
    app.state.medical_repository = medical_repository
    app.state.kakao_callback_service = kakao_callback_service or KakaoCallbackService(
        timeout_seconds=app_settings.kakao_callback_timeout_seconds,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/debug/drive")
    async def debug_drive(
        request: Request,
    ) -> dict:
        if not _admin_authorized(request):
            return {"ok": False, "error": "unauthorized"}
        repository = _get_or_init_medical_repository(request)
        drive_gateway = _get_or_init_drive_gateway(request)
        return {
            "ok": repository is not None and drive_gateway is not None,
            "firestore": repository is not None,
            "drive": drive_gateway is not None,
        }

    @app.post("/kakao/auth")
    async def kakao_auth(
        payload: KakaoSkillRequest,
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> dict:
        chatbot = _get_or_init_firestore_chatbot_service(request)
        if chatbot is None:
            return _simple_system_error_response()
        return await chatbot.handle_auth_entry(payload, background_tasks)

    @app.post("/kakao/chat")
    async def kakao_chat(
        payload: KakaoSkillRequest,
        request: Request,
        background_tasks: BackgroundTasks,
    ) -> dict:
        chatbot = _get_or_init_firestore_chatbot_service(request)
        if chatbot is None:
            return _simple_system_error_response()
        return await chatbot.handle_chat(payload, background_tasks)

    @app.post("/admin/sync-drive-changes")
    async def admin_sync_drive_changes(request: Request) -> dict:
        if not _admin_authorized(request):
            return {"ok": False, "error": "unauthorized"}
        sync_service = _get_or_init_drive_changes_sync_service(request)
        if sync_service is None:
            return {"ok": False, "error": "sync service unavailable"}
        return sync_service.sync_changes().to_dict()

    @app.post("/admin/bootstrap-patients-from-drive")
    async def admin_bootstrap_patients_from_drive(request: Request) -> dict:
        if not _admin_authorized(request):
            return {"ok": False, "error": "unauthorized"}
        bootstrap_service = _get_or_init_patient_bootstrap_service(request)
        if bootstrap_service is None:
            return {"ok": False, "error": "patient bootstrap service unavailable"}
        return bootstrap_service.bootstrap().to_dict()

    @app.post("/admin/sync-drive")
    async def admin_sync_drive(request: Request) -> dict:
        if not _admin_authorized(request):
            return {"ok": False, "error": "unauthorized"}
        bootstrap_payload: dict | None = None
        if request.app.state.settings.drive_patient_bootstrap_on_full_sync:
            bootstrap_service = _get_or_init_patient_bootstrap_service(request)
            if bootstrap_service is None:
                return {"ok": False, "error": "patient bootstrap service unavailable"}
            bootstrap_result = bootstrap_service.bootstrap()
            bootstrap_payload = bootstrap_result.to_dict()
            if not bootstrap_result.ok:
                return {"ok": False, "error": "patient bootstrap failed", "bootstrap": bootstrap_payload}
        sync_service = _get_or_init_drive_changes_sync_service(request)
        if sync_service is None:
            return {"ok": False, "error": "sync service unavailable", "bootstrap": bootstrap_payload}
        payload = sync_service.sync_all().to_dict()
        if bootstrap_payload is not None:
            payload["bootstrap"] = bootstrap_payload
        return payload

    @app.post("/admin/sync-drive/patient/{patient_id}")
    async def admin_sync_drive_patient(patient_id: str, request: Request) -> dict:
        if not _admin_authorized(request):
            return {"ok": False, "error": "unauthorized"}
        sync_service = _get_or_init_drive_changes_sync_service(request)
        if sync_service is None:
            return {"ok": False, "error": "sync service unavailable"}
        return sync_service.sync_patient(patient_id).to_dict()

    @app.post("/admin/rebuild-wiki-page/{patient_id}/{source_id}")
    async def admin_rebuild_wiki_page(patient_id: str, source_id: str, request: Request) -> dict:
        if not _admin_authorized(request):
            return {"ok": False, "error": "unauthorized"}
        sync_service = _get_or_init_drive_changes_sync_service(request)
        if sync_service is None:
            return {"ok": False, "error": "sync service unavailable"}
        return sync_service.rebuild_wiki_page(patient_id=patient_id, source_id=source_id)

    @app.post("/admin/recompile-wiki-index/{patient_id}")
    async def admin_recompile_wiki_index(patient_id: str, request: Request) -> dict:
        if not _admin_authorized(request):
            return {"ok": False, "error": "unauthorized"}
        sync_service = _get_or_init_drive_changes_sync_service(request)
        if sync_service is None:
            return {"ok": False, "error": "sync service unavailable"}
        return sync_service.recompile_wiki_index(patient_id=patient_id)

    @app.post("/admin/process-callback-jobs")
    async def admin_process_callback_jobs(request: Request, limit: int | None = None) -> dict:
        if not _admin_authorized(request):
            return {"ok": False, "error": "unauthorized"}
        processor = _get_or_init_callback_job_processor(request)
        if processor is None:
            return {"ok": False, "error": "callback job processor unavailable"}
        return processor.process_pending_jobs(limit=limit)

    @app.post("/admin/process-callback-job/{job_id}")
    async def admin_process_callback_job(job_id: str, request: Request) -> dict:
        if not _admin_authorized(request):
            return {"ok": False, "error": "unauthorized"}
        processor = _get_or_init_callback_job_processor(request)
        if processor is None:
            return {"ok": False, "error": "callback job processor unavailable"}
        result = processor.process_job(job_id)
        if result.status == "FAILED" and result.error_code in {"FILE_NOT_READY", "CALLBACK_SEND_FAILED"}:
            raise HTTPException(status_code=503, detail=result.to_dict())
        return {"ok": True, "result": result.to_dict()}

    @app.post("/admin/expire-callback-jobs")
    async def admin_expire_callback_jobs(request: Request, limit: int | None = None) -> dict:
        if not _admin_authorized(request):
            return {"ok": False, "error": "unauthorized"}
        processor = _get_or_init_callback_job_processor(request)
        if processor is None:
            return {"ok": False, "error": "callback job processor unavailable"}
        return processor.expire_jobs(limit=limit)

    @app.post("/admin/cleanup-gemini-files")
    async def admin_cleanup_gemini_files(request: Request, force: bool = False) -> dict:
        if not _admin_authorized(request):
            return {"ok": False, "error": "unauthorized"}
        cleanup_service = _get_or_init_gemini_files_cleanup_service(request)
        if cleanup_service is None:
            return {"ok": False, "error": "gemini files cleanup service unavailable"}
        result = cleanup_service.cleanup_to_target(reason="manual_or_scheduled") if force else cleanup_service.cleanup_if_needed()
        return result.model_dump()


    @app.post("/admin/prewarm-gemini-file")
    async def admin_prewarm_gemini_file(request: Request) -> dict:
        if not _admin_authorized(request):
            return {"ok": False, "error": "unauthorized"}
        service = _get_or_init_gemini_file_prewarm_service(request)
        if service is None:
            return {"ok": False, "error": "gemini file prewarm service unavailable"}
        payload = await request.json()
        prewarm_job_id = str(payload.get("prewarm_job_id") or "")
        try:
            return service.process_job(prewarm_job_id).to_dict()
        except GeminiFilePrewarmRetryableError as exc:
            # Let Cloud Tasks retry this HTTP task. The Firestore job already records failure state.
            logger.exception("retryable prewarm job failed prewarm_job_id=%s", prewarm_job_id)
            raise exc

    return app


def _simple_system_error_response() -> dict:
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


def _admin_authorized(request: Request) -> bool:
    return is_admin_request(request, request.app.state.settings)


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


def _get_or_init_drive_gateway(request: Request) -> DriveGateway | None:
    drive_service = _get_or_init_drive_service(request)
    if drive_service is None:
        return None
    return drive_service.gateway


def _get_or_init_medical_repository(request: Request) -> MedicalRepository | None:
    if request.app.state.medical_repository is not None:
        return request.app.state.medical_repository
    try:
        request.app.state.medical_repository = build_default_medical_repository(
            request.app.state.settings
        )
    except Exception:
        logger.exception("Failed to initialize Firestore repository")
        return None
    return request.app.state.medical_repository


def _get_or_init_wiki_service(request: Request) -> MedicalWikiService | None:
    service = getattr(request.app.state, "medical_wiki_service", None)
    if service is not None:
        return service
    repository = _get_or_init_medical_repository(request)
    drive_gateway = _get_or_init_drive_gateway(request)
    if repository is None or drive_gateway is None:
        return None

    extractor = _get_or_init_medical_wiki_extractor(request)
    if request.app.state.settings.medical_wiki_extraction_mode == "gemini" and extractor is None:
        return None

    request.app.state.medical_wiki_service = MedicalWikiService(
        repository=repository,
        drive_gateway=drive_gateway,
        extractor=extractor,
        settings=request.app.state.settings,
    )
    return request.app.state.medical_wiki_service


def _get_or_init_medical_wiki_extractor(request: Request) -> MedicalWikiExtractor | None:
    if request.app.state.settings.medical_wiki_extraction_mode == "metadata":
        return None
    service = getattr(request.app.state, "medical_wiki_extractor", None)
    if service is not None:
        return service
    try:
        request.app.state.medical_wiki_extractor = build_default_medical_wiki_extractor(
            request.app.state.settings
        )
    except Exception:
        logger.exception("Failed to initialize Medical Wiki extractor")
        return None
    return request.app.state.medical_wiki_extractor


def _get_or_init_gemini_files_qa_service(request: Request) -> GeminiFilesQaService | None:
    if request.app.state.gemini_files_qa_service is not None:
        return request.app.state.gemini_files_qa_service
    repository = _get_or_init_medical_repository(request)
    drive_gateway = _get_or_init_drive_gateway(request)
    if repository is None or drive_gateway is None:
        return None
    try:
        request.app.state.gemini_files_qa_service = build_default_gemini_files_qa_service(
            settings=request.app.state.settings,
            repository=repository,
            drive_gateway=drive_gateway,
        )
    except Exception:
        logger.exception("Failed to initialize Gemini Files QA service")
        return None
    return request.app.state.gemini_files_qa_service


def _get_or_init_gemini_files_cleanup_service(request: Request) -> GeminiFilesCleanupService | None:
    service = getattr(request.app.state, "gemini_files_cleanup_service", None)
    if service is not None:
        return service
    repository = _get_or_init_medical_repository(request)
    qa_service = _get_or_init_gemini_files_qa_service(request)
    if repository is None or qa_service is None:
        return None
    request.app.state.gemini_files_cleanup_service = GeminiFilesCleanupService(
        settings=request.app.state.settings,
        repository=repository,
        gateway=qa_service.gateway,
    )
    return request.app.state.gemini_files_cleanup_service


def _get_or_init_medical_router_service(request: Request) -> MedicalRouterService | None:
    service = getattr(request.app.state, "medical_router_service", None)
    if service is not None:
        return service
    qa_service = _get_or_init_gemini_files_qa_service(request)
    if qa_service is None:
        return None
    request.app.state.medical_router_service = MedicalRouterService(
        settings=request.app.state.settings,
        gateway=qa_service.gateway,
    )
    return request.app.state.medical_router_service


def _get_or_init_callback_task_enqueue_service(request: Request) -> CallbackTaskEnqueueService | None:
    service = getattr(request.app.state, "callback_task_enqueue_service", None)
    if service is not None:
        return service

    settings = request.app.state.settings
    cloud_tasks_client = _build_cloud_tasks_client(
        request,
        queue_name=settings.callback_tasks_queue_name,
    )
    if cloud_tasks_client is None:
        return None

    request.app.state.callback_task_enqueue_service = CallbackTaskEnqueueService(
        settings=settings,
        cloud_tasks_client=cloud_tasks_client,
    )
    return request.app.state.callback_task_enqueue_service


def _build_cloud_tasks_client(request: Request, *, queue_name: str) -> CloudTasksClient | None:
    settings = request.app.state.settings
    project_id = settings.cloud_tasks_project_id or settings.firestore_project_id
    if not project_id:
        logger.error("Cloud Tasks project id is not configured")
        return None
    try:
        return CloudTasksClient(
            project_id=project_id,
            location=settings.cloud_tasks_location,
            queue_name=queue_name,
        )
    except Exception:
        logger.exception("Failed to initialize Cloud Tasks client queue=%s", queue_name)
        return None


def _get_or_init_gemini_file_prewarm_service(request: Request) -> GeminiFilePrewarmService | None:
    service = getattr(request.app.state, "gemini_file_prewarm_service", None)
    if service is not None:
        return service
    repository = _get_or_init_medical_repository(request)
    qa_service = _get_or_init_gemini_files_qa_service(request)
    if repository is None or qa_service is None:
        return None
    request.app.state.gemini_file_prewarm_service = GeminiFilePrewarmService(
        settings=request.app.state.settings,
        repository=repository,
        qa_service=qa_service,
        cleanup_service=_get_or_init_gemini_files_cleanup_service(request),
    )
    return request.app.state.gemini_file_prewarm_service


def _get_or_init_gemini_file_prewarm_enqueue_service(request: Request) -> GeminiFilePrewarmEnqueueService | None:
    service = getattr(request.app.state, "gemini_file_prewarm_enqueue_service", None)
    if service is not None:
        return service
    if not request.app.state.settings.gemini_file_prewarm_enabled:
        return None
    repository = _get_or_init_medical_repository(request)
    if repository is None:
        return None
    cloud_tasks_client = _build_cloud_tasks_client(
        request,
        queue_name=request.app.state.settings.prewarm_tasks_queue_name,
    )
    if cloud_tasks_client is None:
        return None
    request.app.state.gemini_file_prewarm_enqueue_service = GeminiFilePrewarmEnqueueService(
        settings=request.app.state.settings,
        repository=repository,
        cloud_tasks_client=cloud_tasks_client,
    )
    return request.app.state.gemini_file_prewarm_enqueue_service



def _get_or_init_wiki_rebuild_task_enqueue_service(request: Request) -> WikiRebuildTaskEnqueueService | None:
    service = getattr(request.app.state, "wiki_rebuild_task_enqueue_service", None)
    if service is not None:
        return service
    if request.app.state.settings.wiki_rebuild_worker_mode != "cloud_tasks":
        return None
    cloud_tasks_client = _build_cloud_tasks_client(
        request,
        queue_name=request.app.state.settings.wiki_rebuild_tasks_queue_name,
    )
    if cloud_tasks_client is None:
        return None
    request.app.state.wiki_rebuild_task_enqueue_service = WikiRebuildTaskEnqueueService(
        settings=request.app.state.settings,
        cloud_tasks_client=cloud_tasks_client,
    )
    return request.app.state.wiki_rebuild_task_enqueue_service

def _get_or_init_callback_job_processor(request: Request) -> CallbackJobProcessor | None:
    service = getattr(request.app.state, "callback_job_processor", None)
    if service is not None:
        return service
    repository = _get_or_init_medical_repository(request)
    router_service = _get_or_init_medical_router_service(request)
    if repository is None or router_service is None:
        return None
    request.app.state.callback_job_processor = CallbackJobProcessor(
        settings=request.app.state.settings,
        repository=repository,
        router_service=router_service,
        gemini_files_qa_service=_get_or_init_gemini_files_qa_service(request),
        kakao_callback_service=request.app.state.kakao_callback_service,
    )
    return request.app.state.callback_job_processor


def _get_or_init_firestore_chatbot_service(request: Request) -> FirestoreChatbotService | None:
    service = getattr(request.app.state, "firestore_chatbot_service", None)
    if service is not None:
        return service
    repository = _get_or_init_medical_repository(request)
    if repository is None:
        return None

    callback_job_processor = None
    callback_task_enqueue_service = None

    # Keep the Kakao Skill path lightweight.
    # In cloud_tasks and polling modes, /kakao/chat only persists a durable Firestore job
    # and returns useCallback=True. The heavy CallbackJobProcessor, Gemini Router,
    # Gemini Files QA, and Drive clients are initialized only in worker/admin routes.
    if request.app.state.settings.callback_worker_mode == "background":
        callback_job_processor = _get_or_init_callback_job_processor(request)
        if callback_job_processor is None:
            return None
    elif request.app.state.settings.callback_worker_mode == "cloud_tasks":
        callback_task_enqueue_service = _get_or_init_callback_task_enqueue_service(request)

    request.app.state.firestore_chatbot_service = FirestoreChatbotService(
        settings=request.app.state.settings,
        repository=repository,
        callback_job_processor=callback_job_processor,
        callback_task_enqueue_service=callback_task_enqueue_service,
        prewarm_enqueue_service=_get_or_init_gemini_file_prewarm_enqueue_service(request),
        session_ttl_minutes=request.app.state.settings.session_ttl_minutes,
    )
    return request.app.state.firestore_chatbot_service


def _get_or_init_patient_bootstrap_service(request: Request) -> PatientDriveBootstrapService | None:
    service = getattr(request.app.state, "patient_bootstrap_service", None)
    if service is not None:
        return service
    repository = _get_or_init_medical_repository(request)
    drive_gateway = _get_or_init_drive_gateway(request)
    if repository is None or drive_gateway is None:
        return None
    request.app.state.patient_bootstrap_service = PatientDriveBootstrapService(
        settings=request.app.state.settings,
        repository=repository,
        drive_gateway=drive_gateway,
    )
    return request.app.state.patient_bootstrap_service


def _get_or_init_drive_changes_sync_service(request: Request) -> DriveChangesSyncService | None:
    service = getattr(request.app.state, "drive_changes_sync_service", None)
    if service is not None:
        return service
    repository = _get_or_init_medical_repository(request)
    drive_gateway = _get_or_init_drive_gateway(request)
    wiki_service = _get_or_init_wiki_service(request)
    if repository is None or drive_gateway is None or wiki_service is None:
        return None
    request.app.state.drive_changes_sync_service = DriveChangesSyncService(
        settings=request.app.state.settings,
        repository=repository,
        drive_gateway=drive_gateway,
        wiki_service=wiki_service,
        wiki_rebuild_task_enqueue_service=_get_or_init_wiki_rebuild_task_enqueue_service(request),
    )
    return request.app.state.drive_changes_sync_service


app = create_app()
