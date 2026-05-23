from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class KakaoUser(BaseModel):
    id: str


class KakaoUserRequest(BaseModel):
    user: KakaoUser
    utterance: str = ""
    callback_url: str | None = Field(default=None, alias="callbackUrl")


class KakaoSkillRequest(BaseModel):
    user_request: KakaoUserRequest = Field(alias="userRequest")

    model_config = {"populate_by_name": True}


Status = Literal[
    "ok",
    "blocked",
    "cannot_verify",
    "emergency",
    "out_of_scope",
    "cost_block",
]
MedicalQuestionIntent = Literal[
    "OK",
    "EMERGENCY",
    "PRIVACY_BLOCK",
    "COST_BLOCK",
    "OUT_OF_SCOPE",
]

class ModelAnswer(BaseModel):
    evidence: str = ""
    status: Status
    kakaotalk_render: str = ""
    used_source_ids: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


SourceStatus = Literal["ACTIVE", "STALE", "DELETED", "INACTIVE", "UNSUPPORTED"]
RuntimeSyncStatus = Literal[
    "DISCOVERED",
    "WIKI_PENDING",
    "WIKI_PROCESSING",
    "READY",
    "UPLOADING",
    "PROCESSING",
    "STALE",
    "EXPIRED",
    "FAILED",
]
GeminiFileState = Literal["NONE", "UPLOADING", "PROCESSING", "ACTIVE", "EXPIRED", "FAILED"]
CallbackJobStatus = Literal["PENDING", "PROCESSING", "CALLBACK_SENT", "FAILED", "EXPIRED"]
GeminiFilePrewarmJobStatus = Literal["PENDING", "PROCESSING", "DONE", "SKIPPED", "FAILED", "EXPIRED"]
MedicalWikiCategory = Literal[
    "health_checkup",
    "lab_result",
    "prescription",
    "doctor_note",
    "diagnosis_certificate",
    "imaging_report",
    "discharge_summary",
    "referral",
    "mixed_medical_record",
    "unknown",
]
MedicalWikiDateSource = Literal["content", "filename", "manual", "drive_metadata", "unknown"]


class PatientProfile(BaseModel):
    patient_id: str
    name: str
    birth: str
    name_key: str = ""
    birth_key: str = ""
    drive_folder_id: str
    drive_folder_name: str = ""
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""


class KakaoUserMapping(BaseModel):
    kakao_user_id_hash: str
    patient_id: str
    authenticated_at: str = ""
    expires_at: str = ""
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""


class SourceRef(BaseModel):
    drive_file_id: str
    drive_folder_id: str = ""
    original_filename: str = ""
    mime_type: str = ""
    file_size_bytes: int | None = None
    file_hash: str = ""
    drive_modified_at: str = ""


class MedicalSource(BaseModel):
    source_id: str
    patient_id: str
    source_type: str = "google_drive_file"
    source_ref: SourceRef
    source_status: SourceStatus = "ACTIVE"
    last_sync_at: str = ""
    created_at: str = ""
    updated_at: str = ""


class MedicalWikiFrontmatter(BaseModel):
    page_count: int | None = None
    category: str = "unknown"
    date: str = ""
    date_source: str = "unknown"
    date_confidence: float = 0.0
    tags: list[str] = Field(default_factory=list)


class MedicalWikiNavigation(BaseModel):
    anchors: list[str] = Field(default_factory=list)
    open_when: list[str] = Field(default_factory=list)
    skip_when: list[str] = Field(default_factory=list)


class MedicalWikiQuality(BaseModel):
    confidence: float = 0.0
    needs_review: bool = False
    warnings: list[str] = Field(default_factory=list)


class MedicalWikiPage(BaseModel):
    page_id: str
    source_id: str
    patient_id: str
    page_type: str = "source_summary"
    schema_version: int = 1
    page_version: int = 1
    frontmatter: MedicalWikiFrontmatter = Field(default_factory=MedicalWikiFrontmatter)
    description: str = ""
    navigation: MedicalWikiNavigation = Field(default_factory=MedicalWikiNavigation)
    quality: MedicalWikiQuality = Field(default_factory=MedicalWikiQuality)
    provenance: dict[str, Any] = Field(default_factory=dict)


class MedicalWikiIndexPage(BaseModel):
    page_id: str
    source_id: str
    page_type: str = "source_summary"
    category: str = "unknown"
    date: str = ""
    date_source: str = "unknown"
    date_confidence: float = 0.0
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    anchors: list[str] = Field(default_factory=list)
    open_when: list[str] = Field(default_factory=list)
    skip_when: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_review: bool = False
    page_version: int = 1


class MedicalWikiIndex(BaseModel):
    index_id: str = "main"
    patient_id: str
    index_type: str = "medical_wiki_index"
    schema_version: int = 1
    index_version: int = 1
    description: str = ""
    pages: list[MedicalWikiIndexPage] = Field(default_factory=list)
    groupings: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    updated_at: str = ""


class MedicalWikiExtractionResult(BaseModel):
    """Gemini가 생성한 source_summary 후보.

    이 모델은 medical_wiki_pages에 저장하기 전 단계의 엄격한 검증용 schema다.
    원문 전문, 검사 수치 전체, 처방 상세, 파일명, Drive ID 같은 필드는 허용하지 않는다.
    """

    page_count: int | None = Field(default=None, ge=1)
    category: MedicalWikiCategory = "unknown"
    date: str = ""
    date_source: MedicalWikiDateSource = "unknown"
    date_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)
    description: str = ""
    anchors: list[str] = Field(default_factory=list)
    open_when: list[str] = Field(default_factory=list)
    skip_when: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_review: bool = False
    warnings: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class RuntimeFailure(BaseModel):
    code: str = ""
    message: str = ""
    failed_at: str = ""


class GeminiFileRuntime(BaseModel):
    file_name: str = ""
    uri: str = ""
    mime_type: str = ""
    state: GeminiFileState = "NONE"
    expiration_time: str = ""
    uploaded_at: str = ""
    last_checked_at: str = ""
    error: str | None = None
    source_drive_modified_at: str = ""
    source_file_hash: str = ""
    source_file_size_bytes: int | None = None


class GeminiFilesCleanupDeletedItem(BaseModel):
    patient_id: str
    source_id: str
    state: str = ""
    size_bytes: int = 0
    reason: str = ""


class GeminiFilesCleanupError(BaseModel):
    patient_id: str
    source_id: str
    state: str = ""
    error: str = ""


class GeminiFilesCleanupResult(BaseModel):
    ok: bool = True
    triggered: bool = False
    reason: str = ""
    before_usage_bytes: int = 0
    after_usage_bytes: int = 0
    soft_limit_bytes: int = 0
    target_bytes: int = 0
    hard_limit_bytes: int = 0
    deleted_count: int = 0
    skipped_count: int = 0
    deleted_items: list[GeminiFilesCleanupDeletedItem] = Field(default_factory=list)
    errors: list[GeminiFilesCleanupError] = Field(default_factory=list)


class RuntimeSyncState(BaseModel):
    status: RuntimeSyncStatus = "DISCOVERED"
    lock_owner: str | None = None
    lease_expires_at: str | None = None
    retry_count: int = 0
    last_failure: RuntimeFailure | None = None
    last_synced_at: str = ""
    last_generated_at: str = ""


class MedicalSourceRuntime(BaseModel):
    source_id: str
    patient_id: str
    gemini_file: GeminiFileRuntime = Field(default_factory=GeminiFileRuntime)
    sync: RuntimeSyncState = Field(default_factory=RuntimeSyncState)
    wiki_sync: RuntimeSyncState = Field(default_factory=RuntimeSyncState)
    updated_at: str = ""


class DriveSyncState(BaseModel):
    scope_id: str = "main"
    scope_type: str = "my_drive"
    saved_page_token: str = ""
    last_success_page_token: str = ""
    sync_status: str = "BOOTSTRAP_REQUIRED"
    lock_owner: str | None = None
    lease_expires_at: str | None = None
    retry_count: int = 0
    last_failure: RuntimeFailure | None = None
    last_synced_at: str = ""
    last_full_scan_at: str = ""
    folder_index_bootstrapped_at: str = ""
    updated_at: str = ""


class DriveFolderIndexEntry(BaseModel):
    drive_folder_id: str
    patient_id: str
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""


class DriveFileIndexEntry(BaseModel):
    drive_file_id: str
    patient_id: str
    source_id: str
    drive_folder_id: str = ""
    status: str = "ACTIVE"
    created_at: str = ""
    updated_at: str = ""


class FinalQaAnswer(BaseModel):
    status: Literal["ok", "cannot_verify", "out_of_scope", "emergency", "blocked", "cost_block"]
    kakaotalk_render: str = ""
    used_source_ids: list[str] = Field(default_factory=list)

    model_config = {"extra": "forbid"}


class RouterSelection(BaseModel):
    selection_status: Literal["selected", "insufficient"]
    intent: MedicalQuestionIntent = "OK"
    primary_source_id: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = Field(default="", max_length=300)

    model_config = {"extra": "forbid"}


class ChatSession(BaseModel):
    patient_id: str
    kakao_user_id_hash: str
    recent_messages: list[dict[str, Any]] = Field(default_factory=list)
    updated_at: str = ""
    expires_at: str = ""


class ChatLog(BaseModel):
    log_id: str
    patient_id: str
    kakao_user_id_hash: str
    role: str = "user"
    message: str
    message_type: str = "question"
    job_id: str | None = None
    created_at: str = ""


class GeminiFilePrewarmJob(BaseModel):
    job_id: str
    patient_id: str
    source_id: str = ""
    reason: str = ""
    idempotency_key: str = ""
    status: GeminiFilePrewarmJobStatus = "PENDING"

    # Durable worker fields.
    runnable: bool = True
    retry_count: int = 0
    max_attempts: int = 3
    next_run_at: str = ""
    lock_owner: str | None = None
    lease_expires_at: str | None = None

    skip_reason: str = ""
    last_failure: RuntimeFailure | None = None

    created_at: str = ""
    updated_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    expires_at: str = ""


class KakaoCallbackJob(BaseModel):
    job_id: str
    patient_id: str
    kakao_user_id_hash: str
    chat_log_id: str
    status: CallbackJobStatus = "PENDING"
    callback_url: str = ""

    # Durable callback worker fields.
    # retry_count is treated as the number of processing attempts.
    retry_count: int = 0
    max_attempts: int = 3
    processing_started_at: str = ""
    sent_at: str = ""
    lock_owner: str | None = None
    lease_expires_at: str | None = None
    idempotency_key: str = ""
    sent_text_type: str = ""
    final_text_hash: str = ""
    callback_sent_count: int = 0

    # Query-friendly worker fields for Firestore production.
    # runnable=True means the job can be picked by a polling worker.
    # next_run_at is an ISO string used for simple indexed retry/backoff queries.
    runnable: bool = True
    next_run_at: str = ""

    last_failure: RuntimeFailure | None = None
    created_at: str = ""
    updated_at: str = ""
    expires_at: str = ""
