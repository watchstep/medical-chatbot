from __future__ import annotations

import logging
import os

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse

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
from app.services.temporary_attachments import (
    TemporaryAttachmentConfigError,
    TemporaryAttachmentError,
    TemporaryAttachmentTokenError,
    TemporaryAttachmentService,
    build_default_temporary_attachment_service,
)
from app.services.wiki_rebuild_tasks import WikiRebuildTaskEnqueueService
from app.services.admin_auth import is_admin_request
from app.services.admin_auth import DASHBOARD_BASIC_REALM, is_dashboard_request
from app.services.admin_dashboard import (
    DASHBOARD_DEFAULT_CHAT_LOG_LIMIT,
    AdminDashboardService,
    dashboard_html,
)
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

UPLOAD_COMPLETE_GUIDANCE = (
    "업로드한 파일을 기준으로 질문하려면\n"
    "“방금 파일” 또는 “업로드한 파일”을 포함해 주세요.\n\n"
    "해당 표현이 없으면 기존 등록 진료기록을 기준으로 답변합니다."
)


def _upload_complete_message(prefix: str) -> str:
    return f"{prefix}\n\n{UPLOAD_COMPLETE_GUIDANCE}"


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
    app.state.temporary_attachment_service = None
    app.state.wiki_rebuild_task_enqueue_service = None
    app.state.admin_dashboard_service = None
    app.state.medical_repository = medical_repository
    app.state.kakao_callback_service = kakao_callback_service or KakaoCallbackService(
        timeout_seconds=app_settings.kakao_callback_timeout_seconds,
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/upload/{token}", response_class=HTMLResponse)
    async def upload_form(token: str, request: Request) -> HTMLResponse:
        service = _get_or_init_temporary_attachment_service(request)
        if service is None:
            return _upload_html_response("현재 파일 업로드 기능을 사용할 수 없습니다.", status_code=503)
        try:
            service.validate_upload_token(token)
        except TemporaryAttachmentConfigError:
            return _upload_html_response("현재 파일 업로드 기능을 사용할 수 없습니다.", status_code=503)
        except TemporaryAttachmentError:
            return _upload_html_response("업로드 링크가 만료되었거나 유효하지 않습니다.", status_code=400)
        return _upload_html_response(
            "PDF 또는 이미지 파일 1개를 선택해 업로드해 주세요.",
            form_action=f"/upload/{token}",
            status_code=200,
        )

    @app.post("/upload/{token}", response_class=HTMLResponse)
    async def upload_file(token: str, request: Request, file: UploadFile = File(...)) -> HTMLResponse:
        service = _get_or_init_temporary_attachment_service(request)
        if service is None:
            return _upload_html_response("현재 파일 업로드 기능을 사용할 수 없습니다.", status_code=503)
        try:
            content = await file.read()
            service.upload_file(
                token=token,
                content=content,
                content_type=file.content_type or "",
            )
        except TemporaryAttachmentConfigError:
            return _upload_html_response("현재 파일 업로드 기능을 사용할 수 없습니다.", status_code=503)
        except TemporaryAttachmentTokenError:
            try:
                completed_attachment = service.get_completed_upload_attachment(token=token)
            except TemporaryAttachmentError:
                completed_attachment = None
            if completed_attachment is not None:
                return _upload_html_response(
                    _upload_complete_message("이미 업로드가 완료되었습니다. 카카오톡으로 돌아가 파일에 대해 질문해 주세요."),
                    status_code=200,
                )
            return _upload_html_response(
                "파일을 업로드하지 못했습니다. 링크 만료, 파일 형식, 용량을 확인해 주세요.",
                status_code=400,
            )
        except TemporaryAttachmentError:
            return _upload_html_response(
                "파일을 업로드하지 못했습니다. 링크 만료, 파일 형식, 용량을 확인해 주세요.",
                status_code=400,
            )
        except Exception:
            logger.exception("temporary upload failed")
            return _upload_html_response("파일을 업로드하지 못했습니다. 잠시 후 다시 시도해 주세요.", status_code=500)
        return _upload_html_response(
            _upload_complete_message("파일 업로드가 완료되었습니다. 카카오톡으로 돌아가 파일에 대해 질문해 주세요."),
            status_code=200,
        )

    @app.get("/admin/dashboard", response_class=HTMLResponse)
    async def admin_dashboard(request: Request) -> HTMLResponse:
        _require_dashboard_authorized(request)
        return HTMLResponse(dashboard_html())

    @app.get("/admin/dashboard/status")
    async def admin_dashboard_status(request: Request) -> dict:
        _require_dashboard_authorized(request)
        dashboard = _get_or_init_admin_dashboard_service(request)
        if dashboard is None:
            return {"firestore": "unavailable", "drive": "unavailable"}
        return dashboard.status()

    @app.get("/admin/dashboard/chat-logs")
    async def admin_dashboard_chat_logs(
        request: Request,
        mode: str = "recent",
        start_at: str = "",
        end_at: str = "",
        limit: int = DASHBOARD_DEFAULT_CHAT_LOG_LIMIT,
        page: int = 1,
    ) -> dict:
        _require_dashboard_authorized(request)
        dashboard = _get_or_init_admin_dashboard_service(request)
        if dashboard is None:
            return {"ok": False, "error": "dashboard service unavailable", "items": []}
        return dashboard.chat_logs(
            mode=mode,
            start_at=start_at,
            end_at=end_at,
            limit=limit,
            page=page,
        )

    @app.get("/admin/dashboard/callback-jobs")
    async def admin_dashboard_callback_jobs(request: Request, limit: int = 20) -> dict:
        _require_dashboard_authorized(request)
        dashboard = _get_or_init_admin_dashboard_service(request)
        if dashboard is None:
            return {"ok": False, "error": "dashboard service unavailable", "counts": {}, "recent_failed_jobs": []}
        return dashboard.callback_jobs(limit=limit)

    @app.get("/admin/dashboard/logs-link")
    async def admin_dashboard_logs_link(request: Request) -> dict:
        _require_dashboard_authorized(request)
        dashboard = _get_or_init_admin_dashboard_service(request)
        if dashboard is None:
            return {"url": ""}
        return dashboard.logs_link()

    @app.post("/admin/dashboard/actions/sync-drive-changes")
    async def admin_dashboard_sync_drive_changes(request: Request) -> dict:
        _require_dashboard_authorized(request)
        sync_service = _get_or_init_drive_changes_sync_service(request)
        if sync_service is None:
            return {"ok": False, "error": "sync service unavailable"}
        return sync_service.sync_changes().to_dict()

    @app.post("/admin/dashboard/actions/sync-drive-full")
    async def admin_dashboard_sync_drive_full(request: Request) -> dict:
        _require_dashboard_authorized(request)
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

    @app.post("/admin/dashboard/actions/cleanup-gemini-files")
    async def admin_dashboard_cleanup_gemini_files(request: Request, force: bool = True) -> dict:
        _require_dashboard_authorized(request)
        cleanup_service = _get_or_init_gemini_files_cleanup_service(request)
        if cleanup_service is None:
            return {"ok": False, "error": "gemini files cleanup service unavailable"}
        result = cleanup_service.cleanup_to_target(reason="dashboard") if force else cleanup_service.cleanup_if_needed()
        payload = result.model_dump()
        temporary_service = _get_or_init_temporary_attachment_service(request)
        if temporary_service is not None:
            payload["temporary_attachments"] = temporary_service.cleanup_expired(
                limit=request.app.state.settings.gemini_files_cleanup_batch_size
            ).to_dict()
        return payload

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
        payload = result.model_dump()
        temporary_service = _get_or_init_temporary_attachment_service(request)
        if temporary_service is not None:
            payload["temporary_attachments"] = temporary_service.cleanup_expired(
                limit=request.app.state.settings.gemini_files_cleanup_batch_size
            ).to_dict()
        return payload

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


def _upload_html_response(message: str, *, form_action: str = "", status_code: int) -> HTMLResponse:
    escaped_message = (
        message.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    escaped_form_action = (
        form_action.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    form_html = ""
    if form_action:
        form_html = f"""
        <form id="uploadForm" method="post" action="{escaped_form_action}" enctype="multipart/form-data">
          <label class="drop-zone" id="dropZone" for="fileInput">
            <span class="drop-icon" aria-hidden="true">+</span>
            <span class="drop-title">여기에 파일을 드래그&amp;드롭하세요</span>
            <span class="drop-subtitle">또는 눌러서 파일을 선택하세요</span>
            <span class="file-types" aria-label="지원 파일 형식">
              <span>PDF</span>
              <span>JPG</span>
              <span>PNG</span>
              <span>WEBP</span>
            </span>
          </label>

          <div class="divider"><span>또는</span></div>

          <input id="fileInput" type="file" name="file" accept="application/pdf,image/jpeg,image/png,image/webp" required>
          <label class="file-input-label" for="fileInput" id="fileLabel">
            <span class="folder-icon" aria-hidden="true"></span>
            <span class="label-text">파일을 선택해 주세요</span>
            <span class="btn-label">찾아보기</span>
          </label>

          <div class="selected-file hidden" id="selectedFile">
            <span class="check-icon" aria-hidden="true"></span>
            <span class="file-name" id="fileName"></span>
            <button class="remove-btn" type="button" id="removeBtn" aria-label="파일 제거">x</button>
          </div>

          <button class="upload-btn" id="uploadBtn" type="submit" disabled>업로드</button>
        </form>
        """
    return HTMLResponse(
        content=f"""
        <!doctype html>
        <html lang="ko">
          <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>파일 업로드</title>
            <style>
              *, *::before, *::after {{ box-sizing: border-box; }}
              body {{
                min-height: 100vh;
                margin: 0;
                display: flex;
                align-items: center;
                justify-content: center;
                background: #f5f5f3;
                color: #1a1a18;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
                line-height: 1.5;
                padding: 24px;
              }}
              main {{
                width: 100%;
                max-width: 440px;
                background: #fff;
                border: 1px solid rgba(0, 0, 0, 0.12);
                border-radius: 20px;
                padding: 34px 28px;
              }}
              .upload-header {{
                text-align: center;
                margin-bottom: 24px;
              }}
              .upload-icon-wrap {{
                width: 64px;
                height: 64px;
                margin: 0 auto 16px;
                display: flex;
                align-items: center;
                justify-content: center;
                border-radius: 16px;
                background: #f5f5f3;
                border: 1px solid rgba(0, 0, 0, 0.1);
              }}
              .upload-icon {{
                width: 26px;
                height: 26px;
                position: relative;
                color: #888780;
              }}
              .upload-icon::before {{
                content: "";
                position: absolute;
                inset: 4px 3px 2px;
                border: 2px solid currentColor;
                border-top: 0;
                border-radius: 0 0 7px 7px;
              }}
              .upload-icon::after {{
                content: "";
                position: absolute;
                left: 50%;
                top: 1px;
                width: 10px;
                height: 10px;
                border-left: 2px solid currentColor;
                border-top: 2px solid currentColor;
                transform: translateX(-50%) rotate(45deg);
              }}
              h1 {{
                margin: 0 0 8px;
                font-size: 20px;
                font-weight: 600;
                letter-spacing: 0;
              }}
              .message {{
                margin: 0;
                font-size: 13px;
                color: #77766f;
                word-break: keep-all;
                white-space: pre-line;
              }}
              form {{
                display: flex;
                flex-direction: column;
                gap: 16px;
              }}
              .drop-zone {{
                width: 100%;
                min-height: 154px;
                border: 1.5px dashed rgba(0, 0, 0, 0.2);
                border-radius: 12px;
                padding: 26px 20px;
                display: flex;
                flex-direction: column;
                align-items: center;
                justify-content: center;
                gap: 8px;
                cursor: pointer;
                background: #f5f5f3;
                text-align: center;
                transition: background 0.15s, border-color 0.15s;
              }}
              .drop-zone:hover, .drop-zone.hover {{
                background: #e6f1fb;
                border-color: #378add;
              }}
              .drop-icon {{
                width: 28px;
                height: 28px;
                display: inline-flex;
                align-items: center;
                justify-content: center;
                border-radius: 999px;
                color: #888780;
                font-size: 24px;
                line-height: 1;
              }}
              .drop-title {{
                font-size: 13px;
                font-weight: 500;
                color: #185fa5;
              }}
              .drop-subtitle {{
                font-size: 12px;
                color: #888780;
              }}
              .file-types {{
                display: flex;
                flex-wrap: wrap;
                justify-content: center;
                gap: 6px;
                margin-top: 4px;
              }}
              .file-types span {{
                padding: 3px 8px;
                border-radius: 999px;
                background: #fff;
                border: 1px solid rgba(0, 0, 0, 0.12);
                color: #888780;
                font-size: 11px;
              }}
              .divider {{
                display: flex;
                align-items: center;
                gap: 10px;
                color: #b4b2a9;
                font-size: 12px;
              }}
              .divider::before, .divider::after {{
                content: "";
                flex: 1;
                height: 1px;
                background: rgba(0, 0, 0, 0.1);
              }}
              input[type="file"] {{
                position: absolute;
                width: 1px;
                height: 1px;
                opacity: 0;
                pointer-events: none;
              }}
              .file-input-label, .selected-file {{
                width: 100%;
                min-height: 46px;
                display: flex;
                align-items: center;
                gap: 10px;
                border-radius: 8px;
                padding: 10px 14px;
              }}
              .file-input-label {{
                border: 1px solid rgba(0, 0, 0, 0.18);
                cursor: pointer;
                background: #fff;
              }}
              .file-input-label:hover {{
                background: #f5f5f3;
              }}
              .folder-icon {{
                width: 18px;
                height: 14px;
                border: 2px solid #888780;
                border-radius: 3px;
                position: relative;
                flex: 0 0 auto;
              }}
              .folder-icon::before {{
                content: "";
                position: absolute;
                left: 1px;
                top: -6px;
                width: 8px;
                height: 5px;
                border: 2px solid #888780;
                border-bottom: 0;
                border-radius: 3px 3px 0 0;
              }}
              .label-text {{
                flex: 1;
                min-width: 0;
                color: #888780;
                font-size: 13px;
              }}
              .btn-label {{
                padding: 4px 10px;
                border: 1px solid #85b7eb;
                border-radius: 8px;
                background: #e6f1fb;
                color: #185fa5;
                font-size: 12px;
                font-weight: 500;
              }}
              .selected-file {{
                background: #eaf3de;
                border: 1px solid #97c459;
              }}
              .check-icon {{
                width: 18px;
                height: 18px;
                border-radius: 999px;
                background: #3b6d11;
                position: relative;
                flex: 0 0 auto;
              }}
              .check-icon::after {{
                content: "";
                position: absolute;
                left: 5px;
                top: 4px;
                width: 7px;
                height: 4px;
                border-left: 2px solid #fff;
                border-bottom: 2px solid #fff;
                transform: rotate(-45deg);
              }}
              .file-name {{
                flex: 1;
                min-width: 0;
                color: #3b6d11;
                font-size: 13px;
                font-weight: 500;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
              }}
              .remove-btn {{
                width: 28px;
                height: 28px;
                border: 0;
                border-radius: 6px;
                background: transparent;
                color: #3b6d11;
                cursor: pointer;
                font-size: 18px;
                line-height: 1;
              }}
              .upload-btn {{
                width: 100%;
                min-height: 48px;
                padding: 13px;
                border: 0;
                border-radius: 8px;
                background: #1a1a18;
                color: #fff;
                font: inherit;
                font-size: 14px;
                font-weight: 600;
                cursor: pointer;
                transition: opacity 0.15s, transform 0.1s;
              }}
              .upload-btn:hover {{ opacity: 0.86; }}
              .upload-btn:active {{ transform: scale(0.99); }}
              .upload-btn:disabled {{
                opacity: 0.35;
                cursor: not-allowed;
                transform: none;
              }}
              .hidden {{ display: none; }}
              @media (max-width: 480px) {{
                body {{ padding: 16px; align-items: stretch; }}
                main {{ margin: auto 0; padding: 30px 22px; }}
              }}
            </style>
          </head>
          <body>
            <main>
              <div class="upload-header">
                <div class="upload-icon-wrap">
                  <span class="upload-icon" aria-hidden="true"></span>
                </div>
                <h1>파일 업로드</h1>
                <p class="message">{escaped_message}</p>
              </div>
              {form_html}
            </main>
            <script>
              const uploadForm = document.getElementById('uploadForm');
              const fileInput = document.getElementById('fileInput');
              const dropZone = document.getElementById('dropZone');
              const fileLabel = document.getElementById('fileLabel');
              const selectedFile = document.getElementById('selectedFile');
              const fileNameEl = document.getElementById('fileName');
              const uploadBtn = document.getElementById('uploadBtn');
              const removeBtn = document.getElementById('removeBtn');

              function setFile(file) {{
                if (!fileInput || !file) return;
                if (fileNameEl) fileNameEl.textContent = file.name;
                if (selectedFile) selectedFile.classList.remove('hidden');
                if (fileLabel) fileLabel.classList.add('hidden');
                if (uploadBtn) uploadBtn.disabled = false;
              }}

              function clearFile() {{
                if (!fileInput) return;
                fileInput.value = '';
                if (selectedFile) selectedFile.classList.add('hidden');
                if (fileLabel) fileLabel.classList.remove('hidden');
                if (uploadBtn) {{
                  uploadBtn.disabled = true;
                  uploadBtn.textContent = '업로드';
                }}
              }}

              if (fileInput) {{
                fileInput.addEventListener('change', () => {{
                  if (fileInput.files && fileInput.files[0]) setFile(fileInput.files[0]);
                }});
              }}

              if (removeBtn) removeBtn.addEventListener('click', clearFile);

              if (dropZone) {{
                dropZone.addEventListener('dragover', (event) => {{
                  event.preventDefault();
                  dropZone.classList.add('hover');
                }});
                dropZone.addEventListener('dragleave', () => {{
                  dropZone.classList.remove('hover');
                }});
                dropZone.addEventListener('drop', (event) => {{
                  event.preventDefault();
                  dropZone.classList.remove('hover');
                  const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
                  if (!file || !fileInput) return;
                  let assigned = false;
                  try {{
                    const transfer = new DataTransfer();
                    transfer.items.add(file);
                    fileInput.files = transfer.files;
                    assigned = fileInput.files && fileInput.files.length > 0;
                  }} catch (error) {{}}
                  if (assigned) {{
                    setFile(file);
                  }} else {{
                    clearFile();
                  }}
                }});
              }}

              if (uploadForm) {{
                uploadForm.addEventListener('submit', () => {{
                  const button = uploadForm.querySelector('button[type=submit]');
                  if (button) {{
                    button.disabled = true;
                    button.textContent = '업로드 중...';
                  }}
                }});
              }}
            </script>
          </body>
        </html>
        """,
        status_code=status_code,
    )


def _admin_authorized(request: Request) -> bool:
    return is_admin_request(request, request.app.state.settings)


def _require_dashboard_authorized(request: Request) -> None:
    settings = request.app.state.settings
    if not settings.admin_dashboard_enabled:
        raise HTTPException(status_code=404, detail="dashboard is disabled")
    if is_dashboard_request(request, settings):
        return
    raise HTTPException(
        status_code=401,
        detail="dashboard authentication required",
        headers={"WWW-Authenticate": f'Basic realm="{DASHBOARD_BASIC_REALM}"'},
    )


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


def _get_or_init_temporary_attachment_service(request: Request) -> TemporaryAttachmentService | None:
    service = getattr(request.app.state, "temporary_attachment_service", None)
    if service is not None:
        return service
    repository = _get_or_init_medical_repository(request)
    if repository is None:
        return None
    try:
        request.app.state.temporary_attachment_service = build_default_temporary_attachment_service(
            settings=request.app.state.settings,
            repository=repository,
        )
    except Exception:
        logger.exception("Failed to initialize temporary attachment service")
        return None
    return request.app.state.temporary_attachment_service


def _get_or_init_admin_dashboard_service(request: Request) -> AdminDashboardService | None:
    service = getattr(request.app.state, "admin_dashboard_service", None)
    if service is not None:
        return service
    repository = _get_or_init_medical_repository(request)
    if repository is None:
        return None
    drive_gateway = _get_or_init_drive_gateway(request)
    request.app.state.admin_dashboard_service = AdminDashboardService(
        settings=request.app.state.settings,
        repository=repository,
        drive_gateway=drive_gateway,
    )
    return request.app.state.admin_dashboard_service


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
        temporary_attachment_service=_get_or_init_temporary_attachment_service(request),
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
    elif request.app.state.settings.callback_worker_mode == "cloud_tasks":
        callback_task_enqueue_service = _get_or_init_callback_task_enqueue_service(request)

    request.app.state.firestore_chatbot_service = FirestoreChatbotService(
        settings=request.app.state.settings,
        repository=repository,
        callback_job_processor=callback_job_processor,
        callback_task_enqueue_service=callback_task_enqueue_service,
        prewarm_enqueue_service=_get_or_init_gemini_file_prewarm_enqueue_service(request),
        temporary_attachment_service=_get_or_init_temporary_attachment_service(request),
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
