from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ThinkingLevel = Literal["minimal", "low", "medium", "high"]
AdminAuthMode = Literal["token", "oidc", "token_or_oidc"]
WikiExtractionMode = Literal["gemini", "metadata"]
RouterMode = Literal["gemini"]
CallbackWorkerMode = Literal["background", "polling", "cloud_tasks"]
WikiRebuildWorkerMode = Literal["inline", "cloud_tasks"]


class Settings(BaseSettings):
    app_env: str = "local"
    app_name: str = "medical-chatbot"
    google_service_account_path: Path | None = None
    firestore_project_id: str | None = None
    firestore_database_id: str = "(default)"

    # Admin endpoint auth
    # - local/dev: ADMIN_AUTH_MODE=token + ADMIN_SYNC_TOKEN
    # - prod: ADMIN_AUTH_MODE=oidc + ADMIN_OIDC_AUDIENCE + ADMIN_OIDC_ALLOWED_EMAILS
    admin_auth_mode: AdminAuthMode = "token"
    admin_sync_token: str | None = None
    admin_oidc_audience: str | None = None
    admin_oidc_allowed_emails: str = ""
    admin_dashboard_enabled: bool = False
    admin_dashboard_username: str | None = None
    admin_dashboard_password: str | None = None

    # Temporary Kakao upload links
    upload_token_ttl_minutes: int = Field(default=15, ge=1, le=120)
    chat_attachment_ttl_minutes: int = Field(default=60, ge=1, le=1440)
    max_upload_file_bytes: int = Field(default=20971520, ge=1, le=104857600)
    upload_token_secret: str | None = None
    upload_base_url: str | None = None

    # Google Drive patient folder bootstrap
    # Root ID is preferred because Drive folder names can be duplicated.
    google_drive_root_folder_id: str | None = None
    google_drive_root_folder_name: str = "medical-chatbot"
    patients_folder_name: str = Field(
        default="patients",
        validation_alias=AliasChoices("PATIENTS_FOLDER_NAME", "GOOGLE_DRIVE_PATIENTS_FOLDER_NAME"),
    )
    system_folder_name: str = Field(
        default="_system",
        validation_alias=AliasChoices("SYSTEM_FOLDER_NAME", "GOOGLE_DRIVE_SYSTEM_FOLDER_NAME"),
    )
    patient_folder_name_regex: str = r"^(.+)_([12]\d{7})$"
    drive_patient_bootstrap_on_full_sync: bool = True

    drive_changes_scope_id: str = "main"
    max_changes_per_run: int = 20
    max_changes_pages_per_run: int = Field(default=5, ge=1, le=50)
    max_wiki_page_generations_per_run: int = 3
    max_wiki_backlog_per_run: int = Field(default=10, ge=0, le=500)
    # Medical Wiki page generation/rebuild worker 설정
    # - inline: Drive sync 요청 안에서 wiki page를 생성한다.
    # - cloud_tasks: Drive sync는 source/runtime만 갱신하고 Cloud Tasks가 rebuild endpoint를 호출한다.
    wiki_rebuild_worker_mode: WikiRebuildWorkerMode = "inline"
    wiki_rebuild_tasks_queue_name: str = "medical-chatbot-wiki-rebuild"
    wiki_rebuild_tasks_dispatch_deadline_seconds: int = Field(default=600, ge=15, le=1800)
    changes_sync_lock_lease_minutes: int = Field(default=2, ge=1, le=60)
    full_sync_lock_lease_minutes: int = Field(default=10, ge=1, le=180)
    folder_index_bootstrap_on_changes: bool = True
    file_ready_wait_seconds: int = 30
    file_ready_poll_interval_seconds: float = Field(default=3.0, ge=0.5, le=30.0)
    file_upload_lock_lease_seconds: int = Field(default=600, ge=10, le=600)
    file_upload_max_retry_count: int = Field(default=3, ge=0, le=10)
    file_expiration_margin_seconds: int = Field(default=300, ge=0, le=86400)

    # Gemini Files API runtime cache cleanup 설정
    # - soft limit 초과 시 cleanup을 시작하고 target 이하가 되면 중단한다.
    # - hard limit 이상이면 pre-warm 같은 best-effort upload는 중단하고 cleanup을 우선한다.
    gemini_files_storage_soft_limit_bytes: int = Field(default=15032385536, ge=0)
    gemini_files_storage_target_bytes: int = Field(default=12884901888, ge=0)
    gemini_files_storage_hard_limit_bytes: int = Field(default=18253611008, ge=0)
    gemini_files_cleanup_batch_size: int = Field(default=20, ge=1, le=500)
    # Gemini Files API pre-warm 설정
    # - 사용자 응답은 즉시 반환하고 Cloud Tasks worker가 latest source를 미리 prepare_file 한다.
    gemini_file_prewarm_enabled: bool = True
    gemini_file_prewarm_max_attempts: int = Field(default=3, ge=1, le=10)
    gemini_file_prewarm_lease_seconds: int = Field(default=570, ge=10, le=900)
    gemini_file_prewarm_idempotency_bucket_minutes: int = Field(default=60, ge=5, le=1440)
    gemini_file_prewarm_expiry_minutes: int = Field(default=30, ge=1, le=1440)
    gemini_file_prewarm_dispatch_deadline_seconds: int = Field(default=600, ge=15, le=1800)
    prewarm_tasks_queue_name: str = "medical-chatbot-prewarm"

    session_ttl_minutes: int = 1440

    # Kakao callback job worker 설정
    # - background: /kakao/chat 요청 직후 best-effort BackgroundTasks 처리
    # - polling: /admin/process-callback-jobs를 Cloud Scheduler가 주기적으로 처리
    # - cloud_tasks: Cloud Tasks가 /admin/process-callback-job/{job_id}를 호출하여 처리
    callback_worker_mode: CallbackWorkerMode = "background"
    callback_job_lease_seconds: int = Field(default=55, ge=5, le=600)
    callback_job_max_attempts: int = Field(default=5, ge=1, le=10)
    callback_job_batch_size: int = Field(default=10, ge=1, le=100)
    callback_job_expiry_minutes: int = Field(default=1, ge=1, le=60)
    callback_job_retry_backoff_seconds: int = Field(default=0, ge=0, le=3600)
    callback_processing_budget_seconds: int = Field(default=50, ge=5, le=55)
    kakao_callback_timeout_seconds: float = Field(default=5.0, ge=1.0, le=30.0)
    async_worker_processing_timeout_seconds: int = Field(default=540, ge=30, le=590)

    # Cloud Tasks 설정
    # - callback_worker_mode=cloud_tasks일 때 callback job enqueue에 사용한다.
    # - cloud_tasks_base_url과 cloud_tasks_audience는 Cloud Run service URL로 설정한다.
    cloud_tasks_project_id: str | None = None
    cloud_tasks_location: str = "asia-northeast3"
    cloud_tasks_service_account_email: str = ""
    cloud_tasks_base_url: str = ""
    cloud_tasks_audience: str = ""
    callback_tasks_queue_name: str = "medical-chatbot-callback-jobs"
    callback_tasks_dispatch_deadline_seconds: int = Field(default=60, ge=15, le=1800)
    gemini_http_timeout_ms: int = Field(default=540000, ge=1000, le=900000)

    gemini_api_key: str | None = None

    # QA 모델 설정
    gemini_model: str = "gemini-3.5-flash"
    gemini_temperature: float = 0.1
    gemini_max_output_tokens: int = 3072
    gemini_thinking_level: ThinkingLevel = "low"

    # Parsing 모델 설정
    gemini_parsing_model: str | None = None
    gemini_parsing_max_output_tokens: int = 32768
    gemini_parsing_temperature: float = Field(default=0.0, ge=0.0, le=3.0)
    gemini_parsing_top_k: int | None = Field(default=None, ge=1, le=1000)
    gemini_parsing_thinking_level: ThinkingLevel = "minimal"
    gemini_context_cache_ttl_hours: int = 24

    # Medical Wiki source_summary extraction 설정
    # - prod: MEDICAL_WIKI_EXTRACTION_MODE=gemini
    # - local/fake tests: MEDICAL_WIKI_EXTRACTION_MODE=metadata
    medical_wiki_extraction_mode: WikiExtractionMode = "gemini"
    gemini_wiki_model: str | None = "gemini-3.5-flash"
    gemini_wiki_temperature: float = Field(default=0.0, ge=0.0, le=3.0)
    gemini_wiki_max_output_tokens: int = 2048
    gemini_wiki_thinking_level: ThinkingLevel = "low"

    # Medical Wiki Router 설정
    router_mode: RouterMode = "gemini"
    gemini_router_model: str | None = "gemini-3.5-flash"
    gemini_router_temperature: float = Field(default=0.0, ge=0.0, le=3.0)
    gemini_router_max_output_tokens: int = 1024
    gemini_router_thinking_level: ThinkingLevel = "low"
    router_min_confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    router_max_catalog_pages: int = Field(default=30, ge=1, le=200)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    @field_validator("google_service_account_path", mode="before")
    @classmethod
    def empty_google_service_account_path_uses_adc(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "admin_sync_token",
        "admin_oidc_audience",
        "admin_dashboard_username",
        "admin_dashboard_password",
        "upload_token_secret",
        "upload_base_url",
        "google_drive_root_folder_id",
        "cloud_tasks_project_id",
        mode="before",
    )
    @classmethod
    def empty_optional_strings_are_none(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("gemini_parsing_top_k", mode="before")
    @classmethod
    def empty_optional_ints_are_none(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
