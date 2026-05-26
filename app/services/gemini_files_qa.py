from __future__ import annotations

import json
import logging
import mimetypes
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from google import genai
from google.genai import types

from app.config import Settings
from app.prompts.gemini_files_qa import (
    build_gemini_files_qa_prompt,
    build_gemini_files_qa_response_schema,
    build_gemini_files_qa_system_instruction,
)
from app.prompts.medical_router import (
    build_medical_router_prompt,
    build_medical_router_response_schema,
    build_medical_router_system_instruction,
)
from app.repositories import MedicalRepository
from app.schemas import (
    FinalQaAnswer,
    GeminiFileRuntime,
    MedicalSource,
    MedicalSourceRuntime,
    MedicalWikiIndex,
    MedicalQuestionIntent,
    ModelAnswer,
    RouterSelection,
    RuntimeFailure,
)
from app.services.drive import DriveGateway
from app.services.medical_wiki import KST, format_medical_source_display_name, now_kst_iso
from app.services.timing import timing_done, timing_start


logger = logging.getLogger(__name__)


QA_INTENTS = {"OK"}
FIXED_RESPONSE_INTENTS = {"EMERGENCY", "PRIVACY_BLOCK", "COST_BLOCK", "OUT_OF_SCOPE"}
SOURCE_NOT_REQUIRED_INTENTS = FIXED_RESPONSE_INTENTS
ROUTER_CATEGORY_PRIORITY = {
    "mixed_medical_record": 100,
    "doctor_note": 90,
    "discharge_summary": 80,
    "diagnosis_certificate": 75,
    "health_checkup": 70,
    "lab_result": 60,
    "imaging_report": 60,
    "prescription": 55,
    "referral": 50,
    "unknown": 10,
}


class GeminiFilesQaError(Exception):
    pass


class GeminiFileNotReadyError(GeminiFilesQaError):
    pass


class GeminiFileUploadLockError(GeminiFileNotReadyError):
    pass


@dataclass(frozen=True)
class PreparedGeminiFile:
    source_id: str
    file_name: str
    uri: str
    mime_type: str
    file_object: Any


class GeminiFilesGateway:
    def upload_file(self, *, display_name: str, content: bytes, mime_type: str) -> Any:
        raise NotImplementedError

    def upload_file_path(self, *, display_name: str, path: str, mime_type: str) -> Any:
        with open(path, "rb") as source:
            return self.upload_file(
                display_name=display_name,
                content=source.read(),
                mime_type=mime_type,
            )

    def get_file(self, *, file_name: str) -> Any:
        raise NotImplementedError

    def delete_file(self, *, file_name: str) -> None:
        raise NotImplementedError

    def generate_json(
        self,
        *,
        model: str,
        system_instruction: str,
        contents: list[Any],
        response_schema: dict[str, Any],
        temperature: float,
        max_output_tokens: int,
        thinking_level: str,
    ) -> str:
        raise NotImplementedError


class GoogleGeminiFilesGateway(GeminiFilesGateway):
    def __init__(self, api_key: str, *, client: Any | None = None, timeout_ms: int | None = None):
        normalized_api_key = (api_key or "").strip().strip('"').strip("'")
        if not normalized_api_key:
            raise GeminiFilesQaError("Gemini API key is empty.")
        http_options = types.HttpOptions(timeout=timeout_ms) if timeout_ms else None
        self.client = client or genai.Client(api_key=normalized_api_key, http_options=http_options)

    def upload_file(self, *, display_name: str, content: bytes, mime_type: str) -> Any:
        temp_path: str | None = None
        try:
            suffix = os.path.splitext(display_name)[1] or mimetypes.guess_extension(mime_type) or ".bin"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
                temp_file.write(content)
                temp_path = temp_file.name
            return self.client.files.upload(
                file=temp_path,
                config={"display_name": display_name, "mime_type": mime_type},
            )
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)

    def upload_file_path(self, *, display_name: str, path: str, mime_type: str) -> Any:
        return self.client.files.upload(
            file=path,
            config={"display_name": display_name, "mime_type": mime_type},
        )

    def get_file(self, *, file_name: str) -> Any:
        return self.client.files.get(name=file_name)

    def delete_file(self, *, file_name: str) -> None:
        self.client.files.delete(name=file_name)

    def generate_json(
        self,
        *,
        model: str,
        system_instruction: str,
        contents: list[Any],
        response_schema: dict[str, Any],
        temperature: float,
        max_output_tokens: int,
        thinking_level: str,
    ) -> str:
        response = self.client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_json_schema=response_schema,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                thinking_config=types.ThinkingConfig(thinking_level=thinking_level),
            ),
        )
        text = getattr(response, "text", "") or ""
        if not text:
            raise GeminiFilesQaError("Gemini 응답이 비어 있습니다.")
        return text


@dataclass
class MedicalRouterService:
    settings: Settings
    gateway: GeminiFilesGateway | None = None

    def select_source(
        self,
        *,
        question: str,
        prior_context: str,
        wiki_index: MedicalWikiIndex,
    ) -> RouterSelection:
        catalog = self._build_router_catalog(wiki_index)
        allowed_source_ids = {str(page["source_id"]) for page in catalog["pages"]}

        if self.settings.router_mode == "keyword":
            return self._select_source_keyword(question=question, wiki_index=wiki_index)

        if self.gateway is None:
            raise GeminiFilesQaError("Gemini Router gateway is not configured")

        raw_selection = self.gateway.generate_json(
            model=self.settings.gemini_router_model or self.settings.gemini_model,
            system_instruction=build_medical_router_system_instruction(),
            contents=[
                build_medical_router_prompt(
                    question=question,
                    prior_context=prior_context,
                    catalog=catalog,
                )
            ],
            response_schema=build_medical_router_response_schema(),
            temperature=self.settings.gemini_router_temperature,
            max_output_tokens=self.settings.gemini_router_max_output_tokens,
            thinking_level=self.settings.gemini_router_thinking_level,
        )
        selection = RouterSelection.model_validate_json(raw_selection)
        return self._validate_router_selection(
            selection,
            question=question,
            catalog_pages=catalog["pages"],
            allowed_source_ids=allowed_source_ids,
        )

    def _build_router_catalog(self, wiki_index: MedicalWikiIndex) -> dict[str, Any]:
        pages: list[dict[str, Any]] = []
        for page in wiki_index.pages:
            if page.needs_review:
                continue
            if page.confidence < 0.0:
                continue
            pages.append(
                {
                    "page_id": page.page_id,
                    "source_id": page.source_id,
                    "category": page.category,
                    "date": page.date,
                    "date_source": page.date_source,
                    "date_confidence": page.date_confidence,
                    "description": page.description,
                    "tags": list(page.tags)[:8],
                    "anchors": list(page.anchors)[:12],
                    "open_when": list(page.open_when)[:6],
                    "skip_when": list(getattr(page, "skip_when", []))[:6],
                    "confidence": page.confidence,
                    "page_version": page.page_version,
                }
            )
        pages.sort(
            key=lambda item: (
                str(item.get("date") or ""),
                float(item.get("confidence") or 0.0),
                str(item.get("page_id") or ""),
            ),
            reverse=True,
        )
        return {"pages": pages[: self.settings.router_max_catalog_pages]}

    def _validate_router_selection(
        self,
        selection: RouterSelection,
        *,
        question: str,
        catalog_pages: list[dict[str, Any]],
        allowed_source_ids: set[str],
    ) -> RouterSelection:
        """Normalize and validate Router output before callback branching.

        The Gemini Router may return a source even when the intent is a fixed
        response. Backend logic is the final guardrail: fixed-response intents
        never carry a source_id into file preparation, and only OK may select a
        catalog source.
        """
        if selection.intent in SOURCE_NOT_REQUIRED_INTENTS:
            return selection.model_copy(
                update={
                    "selection_status": "insufficient",
                    "primary_source_id": "",
                    "reason": selection.reason or "source not required for intent",
                }
            )

        if selection.intent != "OK":
            return selection.model_copy(
                update={
                    "selection_status": "insufficient",
                    "primary_source_id": "",
                    "reason": "unsupported intent for source selection",
                }
            )

        if selection.selection_status == "insufficient":
            return self._best_effort_fallback_selection(
                selection.model_copy(update={"primary_source_id": ""}),
                question=question,
                catalog_pages=catalog_pages,
            )

        if selection.primary_source_id not in allowed_source_ids:
            return selection.model_copy(
                update={
                    "selection_status": "insufficient",
                    "primary_source_id": "",
                    "confidence": 0.0,
                    "reason": "router selected source outside catalog",
                }
            )

        if selection.confidence < self.settings.router_min_confidence:
            return selection.model_copy(
                update={
                    "selection_status": "insufficient",
                    "primary_source_id": "",
                    "reason": "router confidence below threshold",
                }
            )

        return selection

    def _best_effort_fallback_selection(
        self,
        selection: RouterSelection,
        *,
        question: str,
        catalog_pages: list[dict[str, Any]],
    ) -> RouterSelection:
        if selection.intent != "OK":
            return selection
        if not catalog_pages:
            return selection
        if self._ok_question_does_not_need_source(reason=selection.reason):
            return selection

        candidates = [
            page
            for page in catalog_pages
            if not self._strong_skip_match(question, page.get("skip_when", []))
        ]
        if not candidates:
            return selection

        best = max(
            candidates,
            key=lambda page: (
                ROUTER_CATEGORY_PRIORITY.get(str(page.get("category") or "unknown"), 0),
                self._catalog_overlap_score(question=question, page=page),
                str(page.get("date") or ""),
                float(page.get("confidence") or 0.0),
                str(page.get("page_id") or ""),
            ),
        )
        source_id = str(best.get("source_id") or "")
        if not source_id:
            return selection
        return RouterSelection(
            selection_status="selected",
            intent="OK",
            primary_source_id=source_id,
            confidence=max(self.settings.router_min_confidence, 0.55),
            reason="best-effort catalog source selected for original record verification",
        )

    def _ok_question_does_not_need_source(self, *, reason: str) -> bool:
        normalized_reason = (reason or "").casefold()
        if any(token in normalized_reason for token in ["source not required", "general medical", "general answer"]):
            return True
        if any(token in normalized_reason for token in ["일반 의학", "일반적인", "개념 설명", "문서 없이"]):
            return True
        return False

    def _catalog_overlap_score(self, *, question: str, page: dict[str, Any]) -> int:
        question_tokens = set(self._tokens(question.lower()))
        if not question_tokens:
            return 0
        haystack = self._catalog_haystack(page)
        return sum(1 for token in question_tokens if token in haystack)

    def _catalog_haystack(self, page: dict[str, Any]) -> str:
        values = [
            page.get("category", ""),
            page.get("description", ""),
            self._join_catalog_values(page.get("tags", [])),
            self._join_catalog_values(page.get("anchors", [])),
            self._join_catalog_values(page.get("open_when", [])),
        ]
        return " ".join(str(value) for value in values).lower()

    def _join_catalog_values(self, value: object) -> str:
        if not isinstance(value, list):
            return ""
        return " ".join(str(item) for item in value)

    def _strong_skip_match(self, question: str, skip_when: object) -> bool:
        if not isinstance(skip_when, list):
            return False
        normalized_question = question.casefold().replace(" ", "")
        question_tokens = set(self._tokens(question.casefold()))
        for item in skip_when:
            cleaned = " ".join(str(item).strip().casefold().split())
            if not cleaned:
                continue
            normalized_item = cleaned.replace(" ", "")
            if len(normalized_item) >= 4 and normalized_item in normalized_question:
                return True
            item_tokens = set(self._tokens(cleaned))
            if len(item_tokens) >= 2 and len(question_tokens & item_tokens) >= 2:
                return True
        return False

    def _select_source_keyword(
        self,
        *,
        question: str,
        wiki_index: MedicalWikiIndex,
    ) -> RouterSelection:
        intent = self._classify_question_intent(question)
        if intent in SOURCE_NOT_REQUIRED_INTENTS:
            return RouterSelection(
                selection_status="insufficient",
                intent=intent,
                confidence=0.8,
                reason="fixed-response intent does not use source selection",
            )
        candidates = [
            page
            for page in wiki_index.pages
            if not page.needs_review and page.confidence >= 0.0
        ]
        if not candidates:
            return RouterSelection(
                selection_status="insufficient",
                intent=intent,
                reason="no usable pages",
            )
        scored: list[tuple[int, str, str]] = []
        normalized_question = question.lower()
        for page in candidates:
            haystack = " ".join(
                [
                    page.category,
                    page.description,
                    " ".join(page.tags),
                    " ".join(page.anchors),
                    " ".join(page.open_when),
                ]
            ).lower()
            score = sum(1 for token in self._tokens(normalized_question) if token in haystack)
            skip_haystack = " ".join(getattr(page, "skip_when", [])).lower()
            score -= sum(1 for token in self._tokens(normalized_question) if token in skip_haystack)
            scored.append((score, page.date, page.source_id))
        scored.sort(reverse=True)
        best_score, _, source_id = scored[0]
        if best_score <= 0 and len(candidates) > 1:
            return RouterSelection(
                selection_status="insufficient",
                intent=intent,
                reason="no matching catalog entry",
            )
        selection = RouterSelection(
            selection_status="selected",
            intent=intent,
            primary_source_id=source_id,
            confidence=0.6 if best_score > 0 else 0.35,
            reason="catalog metadata matched user question" if best_score > 0 else "single usable source",
        )
        return self._validate_router_selection(
            selection,
            question=question,
            catalog_pages=[
                {
                    "page_id": page.page_id,
                    "source_id": page.source_id,
                    "category": page.category,
                    "date": page.date,
                    "description": page.description,
                    "tags": list(page.tags),
                    "anchors": list(page.anchors),
                    "open_when": list(page.open_when),
                    "skip_when": list(getattr(page, "skip_when", [])),
                    "confidence": page.confidence,
                }
                for page in candidates
            ],
            allowed_source_ids={page.source_id for page in candidates},
        )

    def _tokens(self, text: str) -> list[str]:
        return [token for token in text.replace("?", " ").replace(",", " ").split() if len(token) >= 2]

    def _classify_question_intent(self, question: str) -> MedicalQuestionIntent:
        normalized = question.strip().lower().replace(" ", "")
        if not normalized:
            return "OUT_OF_SCOPE"

        emergency_terms = [
            "흉통",
            "가슴통증",
            "가슴이아프",
            "숨이차",
            "숨차",
            "호흡곤란",
            "의식저하",
            "의식이없",
            "실신",
            "기절",
            "심한출혈",
            "피가멈추지",
            "극심한통증",
            "마비",
            "말이어눌",
        ]
        if any(term in normalized for term in emergency_terms):
            return "EMERGENCY"

        privacy_terms = [
            "주민번호",
            "주민등록번호",
            "주소",
            "전화번호",
            "연락처",
            "환자번호",
            "등록번호",
            "신분증",
            "계좌",
            "카드번호",
        ]
        if any(term in normalized for term in privacy_terms):
            return "PRIVACY_BLOCK"

        cost_terms = [
            "비용",
            "금액",
            "가격",
            "진료비",
            "병원비",
            "검사비",
            "보험료",
            "보험",
            "결제",
            "청구",
            "수납",
        ]
        if any(term in normalized for term in cost_terms):
            return "COST_BLOCK"

        medical_terms = [
            "고혈압",
            "당뇨",
            "혈당",
            "콜레스테롤",
            "간수치",
            "신장",
            "검사",
            "진단",
            "처방",
            "약",
            "복용",
            "증상",
            "질병",
            "질환",
            "건강",
            "혈압",
            "수치",
            "결과",
            "의학",
            "의료",
            "통증",
            "아프",
        ]
        if not any(term in normalized for term in medical_terms):
            return "OUT_OF_SCOPE"

        return "OK"


@dataclass
class GeminiFilesQaService:
    settings: Settings
    repository: MedicalRepository
    drive_gateway: DriveGateway
    gateway: GeminiFilesGateway

    def prepare_file(
        self, 
        *, 
        patient_id: str, 
        source_id: str,
        timing_context: dict[str, str] | None = None,
    ) -> PreparedGeminiFile:
        timing_context = timing_context or {}
        source, runtime = self._load_source_runtime(patient_id=patient_id, source_id=source_id)

        reusable = self._try_reuse_active_file(source=source, runtime=runtime, timing_context=timing_context)
        if reusable is not None:
            return reusable

        runtime = self.repository.get_source_runtime(patient_id, source_id) or runtime
        if runtime.gemini_file.state in {"UPLOADING", "PROCESSING"} and runtime.gemini_file.file_name:
            return self._wait_for_file_ready(source=source, runtime=runtime, timing_context=timing_context)

        return self._upload_with_lock(source=source, runtime=runtime, timing_context=timing_context)

    def _load_source_runtime(self, *, patient_id: str, source_id: str) -> tuple[MedicalSource, MedicalSourceRuntime]:
        source = self.repository.get_medical_source(patient_id, source_id)
        runtime = self.repository.get_source_runtime(patient_id, source_id)
        if source is None or runtime is None:
            raise GeminiFilesQaError("선택된 source runtime을 찾지 못했습니다.")
        if source.patient_id != patient_id or source.source_status != "ACTIVE":
            raise GeminiFilesQaError("선택된 source가 현재 환자 문서가 아닙니다.")
        if runtime.wiki_sync.status != "READY":
            raise GeminiFilesQaError("선택된 source의 wiki가 아직 준비되지 않았습니다.")
        return source, runtime

    def _try_reuse_active_file(
        self,
        *,
        source: MedicalSource,
        runtime: MedicalSourceRuntime,
        timing_context: dict[str, str] | None = None,
    ) -> PreparedGeminiFile | None:
        timing_context = timing_context or {}
        reuse_started_at = timing_start(
            logger,
            "file_reuse.start",
            job_id=timing_context.get("job_id"),
            patient_id=source.patient_id,
            source_id=source.source_id,
        )
        reuse_status = "miss"
        file_state = runtime.gemini_file.state
        error_code = ""
        try:
            return self._try_reuse_active_file_inner(source=source, runtime=runtime)
        except Exception:
            reuse_status = "error"
            error_code = "FILE_REUSE_FAILED"
            raise
        finally:
            latest_runtime = self.repository.get_source_runtime(source.patient_id, source.source_id) or runtime
            latest_state = latest_runtime.gemini_file.state or file_state
            if reuse_status == "miss":
                if latest_state == "ACTIVE" and latest_runtime.gemini_file.file_name:
                    reuse_status = "reused"
                elif latest_state in {"EXPIRED", "FAILED"}:
                    reuse_status = latest_state.lower()
            timing_done(
                logger,
                "file_reuse.done",
                reuse_started_at,
                status=reuse_status,
                job_id=timing_context.get("job_id"),
                patient_id=source.patient_id,
                source_id=source.source_id,
                file_state=latest_state,
                error_code=error_code,
            )

    def _try_reuse_active_file_inner(
        self,
        *,
        source: MedicalSource,
        runtime: MedicalSourceRuntime,
    ) -> PreparedGeminiFile | None:
        gemini_file = runtime.gemini_file
        if gemini_file.state != "ACTIVE" or not gemini_file.file_name:
            return None
        if not self._source_snapshot_matches(source=source, gemini_file=gemini_file):
            self._mark_runtime_file_expired(source=source, runtime=runtime, reason="source snapshot changed")
            return None
        if not self._expiration_is_valid(gemini_file.expiration_time):
            self._mark_runtime_file_expired(source=source, runtime=runtime, reason="Gemini file expired")
            return None

        try:
            file_object = self.gateway.get_file(file_name=gemini_file.file_name)
        except Exception as exc:  # noqa: BLE001
            self._record_file_failure(
                source=source,
                runtime=runtime,
                code="FILE_GET_FAILED",
                message=str(exc),
                state="FAILED",
            )
            return None

        state = self._normalize_file_state(file_object)
        refreshed = self._runtime_with_file_object(
            source=source,
            runtime=runtime,
            file_object=file_object,
            state=state,
            sync_status="READY" if state == "ACTIVE" else state,
        )
        self.repository.upsert_source_runtime(refreshed)

        if state == "ACTIVE" and self._expiration_is_valid(refreshed.gemini_file.expiration_time):
            return PreparedGeminiFile(
                source_id=source.source_id,
                file_name=refreshed.gemini_file.file_name,
                uri=refreshed.gemini_file.uri,
                mime_type=refreshed.gemini_file.mime_type,
                file_object=file_object,
            )
        if state in {"FAILED", "EXPIRED"}:
            self._record_file_failure(
                source=source,
                runtime=refreshed,
                code="FILE_PROCESSING_FAILED" if state == "FAILED" else "FILE_EXPIRED",
                message=f"Gemini file state is {state}",
                state=state,
            )
        return None

    def _upload_with_lock(
        self,
        *,
        source: MedicalSource,
        runtime: MedicalSourceRuntime,
        timing_context: dict[str, str] | None = None,
    ) -> PreparedGeminiFile:
        timing_context = timing_context or {}
        if runtime.sync.retry_count >= self.settings.file_upload_max_retry_count:
            self._record_file_failure(
                source=source,
                runtime=runtime,
                code="FILE_UPLOAD_RETRY_EXCEEDED",
                message="Gemini Files API upload retry limit exceeded.",
                state="FAILED",
            )
            raise GeminiFilesQaError("문서 준비 재시도 한도를 초과했습니다.")

        lock_owner = f"file-upload-{uuid.uuid4().hex[:12]}"
        now = now_kst_iso()
        lease_expires_at = (datetime.now(KST) + timedelta(seconds=self.settings.file_upload_lock_lease_seconds)).isoformat()
        locked_runtime = self.repository.try_acquire_source_runtime_lock(
            source.patient_id,
            source.source_id,
            lock_owner=lock_owner,
            lease_expires_at=lease_expires_at,
            now=now,
        )
        if locked_runtime is None:
            latest_runtime = self.repository.get_source_runtime(source.patient_id, source.source_id) or runtime
            if latest_runtime.gemini_file.file_name and latest_runtime.gemini_file.state in {"UPLOADING", "PROCESSING"}:
                return self._wait_for_file_ready(source=source, runtime=latest_runtime, timing_context=timing_context)
            raise GeminiFileUploadLockError("다른 요청이 문서를 준비 중입니다.")

        reusable = self._try_reuse_active_file(source=source, runtime=locked_runtime, timing_context=timing_context)
        if reusable is not None:
            self.repository.release_source_runtime_lock(
                source.patient_id,
                source.source_id,
                lock_owner=lock_owner,
                now=now_kst_iso(),
                sync_status="READY",
            )
            return reusable

        uploading_runtime = locked_runtime.model_copy(
            update={
                "gemini_file": GeminiFileRuntime(
                    mime_type=source.source_ref.mime_type or "application/octet-stream",
                    state="UPLOADING",
                    uploaded_at=now_kst_iso(),
                    last_checked_at=now_kst_iso(),
                    source_drive_modified_at=source.source_ref.drive_modified_at,
                    source_file_hash=source.source_ref.file_hash,
                    source_file_size_bytes=source.source_ref.file_size_bytes,
                ),
                "sync": locked_runtime.sync.model_copy(
                    update={
                        "status": "UPLOADING",
                        "lock_owner": lock_owner,
                        "lease_expires_at": lease_expires_at,
                    }
                ),
                "updated_at": now_kst_iso(),
            }
        )
        self.repository.upsert_source_runtime(uploading_runtime)

        try:
            temp_path: str | None = None
            try:
                suffix = mimetypes.guess_extension(source.source_ref.mime_type or "") or ".bin"
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
                    temp_path = temp_file.name
                drive_download_started_at = timing_start(
                    logger,
                    "drive_download.start",
                    job_id=timing_context.get("job_id"),
                    patient_id=source.patient_id,
                    source_id=source.source_id,
                )
                try:
                    self._download_source_to_path(source.source_ref.drive_file_id, temp_path)
                except Exception:
                    timing_done(
                        logger,
                        "drive_download.done",
                        drive_download_started_at,
                        status="error",
                        job_id=timing_context.get("job_id"),
                        patient_id=source.patient_id,
                        source_id=source.source_id,
                        error_code="DRIVE_DOWNLOAD_FAILED",
                    )
                    raise
                timing_done(
                    logger,
                    "drive_download.done",
                    drive_download_started_at,
                    status="success",
                    job_id=timing_context.get("job_id"),
                    patient_id=source.patient_id,
                    source_id=source.source_id,
                )

                file_upload_started_at = timing_start(
                    logger,
                    "file_upload.start",
                    job_id=timing_context.get("job_id"),
                    patient_id=source.patient_id,
                    source_id=source.source_id,
                )
                try:
                    uploaded_file = self.gateway.upload_file_path(
                        display_name="medical-document",
                        path=temp_path,
                        mime_type=source.source_ref.mime_type or "application/octet-stream",
                    )
                except Exception:
                    timing_done(
                        logger,
                        "file_upload.done",
                        file_upload_started_at,
                        status="error",
                        job_id=timing_context.get("job_id"),
                        patient_id=source.patient_id,
                        source_id=source.source_id,
                        error_code="FILE_UPLOAD_FAILED",
                    )
                    raise
                timing_done(
                    logger,
                    "file_upload.done",
                    file_upload_started_at,
                    status="success",
                    job_id=timing_context.get("job_id"),
                    patient_id=source.patient_id,
                    source_id=source.source_id,
                )
            finally:
                if temp_path and os.path.exists(temp_path):
                    os.unlink(temp_path)
            state = self._normalize_file_state(uploaded_file)
            if not self._file_name(uploaded_file):
                raise GeminiFilesQaError("Gemini Files API 업로드 결과 file_name이 없습니다.")

            uploaded_runtime = self._runtime_with_file_object(
                source=source,
                runtime=uploading_runtime,
                file_object=uploaded_file,
                state=state,
                sync_status="READY" if state == "ACTIVE" else "PROCESSING",
                lock_owner=lock_owner,
                lease_expires_at=lease_expires_at,
            )
            self.repository.upsert_source_runtime(uploaded_runtime)

            if state == "ACTIVE" and self._expiration_is_valid(uploaded_runtime.gemini_file.expiration_time):
                ready_runtime = self._runtime_ready(uploaded_runtime)
                self.repository.upsert_source_runtime(ready_runtime)
                return PreparedGeminiFile(
                    source_id=source.source_id,
                    file_name=ready_runtime.gemini_file.file_name,
                    uri=ready_runtime.gemini_file.uri,
                    mime_type=ready_runtime.gemini_file.mime_type,
                    file_object=uploaded_file,
                )

            if state in {"UPLOADING", "PROCESSING"}:
                try:
                    return self._wait_for_file_ready(
                        source=source,
                        runtime=uploaded_runtime,
                        lock_owner=lock_owner,
                        lease_expires_at=lease_expires_at,
                        timing_context=timing_context,
                    )
                except GeminiFileNotReadyError:
                    latest = self.repository.get_source_runtime(source.patient_id, source.source_id) or uploaded_runtime
                    processing = latest.model_copy(
                        update={
                            "sync": latest.sync.model_copy(
                                update={"status": "PROCESSING", "lock_owner": None, "lease_expires_at": None}
                            ),
                            "updated_at": now_kst_iso(),
                        }
                    )
                    self.repository.upsert_source_runtime(processing)
                    raise

            self._record_file_failure(
                source=source,
                runtime=uploaded_runtime,
                code="FILE_PROCESSING_FAILED",
                message=f"Gemini file state is {state}",
                state="FAILED",
            )
            raise GeminiFilesQaError("Gemini Files API 파일 처리에 실패했습니다.")
        except GeminiFileNotReadyError:
            raise
        except Exception as exc:  # noqa: BLE001
            latest = self.repository.get_source_runtime(source.patient_id, source.source_id) or uploading_runtime
            self._record_file_failure(
                source=source,
                runtime=latest,
                code="FILE_UPLOAD_FAILED",
                message=str(exc),
                state="FAILED",
            )
            raise GeminiFilesQaError("Gemini Files API 업로드에 실패했습니다.") from exc

    def _wait_for_file_ready(
        self,
        *,
        source: MedicalSource,
        runtime: MedicalSourceRuntime,
        lock_owner: str | None = None,
        lease_expires_at: str | None = None,
        timing_context: dict[str, str] | None = None,
    ) -> PreparedGeminiFile:
        timing_context = timing_context or {}
        file_name = runtime.gemini_file.file_name
        if not file_name:
            raise GeminiFileNotReadyError("Gemini 파일 이름이 아직 준비되지 않았습니다.")

        polling_started_at = timing_start(
            logger,
            "file_polling.start",
            job_id=timing_context.get("job_id"),
            patient_id=source.patient_id,
            source_id=source.source_id,
        )
        polling_status = "not_ready"
        file_state = runtime.gemini_file.state
        error_code = ""
        try:
            deadline = time.monotonic() + self.settings.file_ready_wait_seconds
            last_runtime = runtime
            while time.monotonic() <= deadline:
                try:
                    file_object = self.gateway.get_file(file_name=file_name)
                except Exception as exc:  # noqa: BLE001
                    self._record_file_failure(
                        source=source,
                        runtime=last_runtime,
                        code="FILE_GET_FAILED",
                        message=str(exc),
                        state="FAILED",
                    )
                    polling_status = "error"
                    error_code = "FILE_GET_FAILED"
                    raise GeminiFilesQaError("Gemini Files API 파일 상태 조회에 실패했습니다.") from exc

                state = self._normalize_file_state(file_object)
                file_state = state
                last_runtime = self._runtime_with_file_object(
                    source=source,
                    runtime=last_runtime,
                    file_object=file_object,
                    state=state,
                    sync_status="READY" if state == "ACTIVE" else "PROCESSING",
                    lock_owner=lock_owner,
                    lease_expires_at=lease_expires_at,
                )
                self.repository.upsert_source_runtime(last_runtime)

                if state == "ACTIVE" and self._expiration_is_valid(last_runtime.gemini_file.expiration_time):
                    ready_runtime = self._runtime_ready(last_runtime)
                    self.repository.upsert_source_runtime(ready_runtime)
                    polling_status = "success"
                    return PreparedGeminiFile(
                        source_id=source.source_id,
                        file_name=ready_runtime.gemini_file.file_name,
                        uri=ready_runtime.gemini_file.uri,
                        mime_type=ready_runtime.gemini_file.mime_type,
                        file_object=file_object,
                    )
                if state in {"FAILED", "EXPIRED"}:
                    self._record_file_failure(
                        source=source,
                        runtime=last_runtime,
                        code="FILE_PROCESSING_FAILED" if state == "FAILED" else "FILE_EXPIRED",
                        message=f"Gemini file state is {state}",
                        state=state,
                    )
                    polling_status = "error"
                    error_code = "FILE_PROCESSING_FAILED" if state == "FAILED" else "FILE_EXPIRED"
                    raise GeminiFilesQaError("Gemini Files API 파일 처리에 실패했습니다.")

                time.sleep(self.settings.file_ready_poll_interval_seconds)

            timeout_runtime = last_runtime.model_copy(
                update={
                    "sync": last_runtime.sync.model_copy(
                        update={"status": "PROCESSING", "lock_owner": None, "lease_expires_at": None}
                    ),
                    "updated_at": now_kst_iso(),
                }
            )
            self.repository.upsert_source_runtime(timeout_runtime)
            error_code = "FILE_NOT_READY"
            raise GeminiFileNotReadyError("Gemini Files API 파일이 아직 ACTIVE 상태가 아닙니다.")
        except Exception:
            if not error_code:
                polling_status = "error"
                error_code = "FILE_POLLING_FAILED"
            raise
        finally:
            timing_done(
                logger,
                "file_polling.done",
                polling_started_at,
                status=polling_status,
                job_id=timing_context.get("job_id"),
                patient_id=source.patient_id,
                source_id=source.source_id,
                file_state=file_state,
                error_code=error_code,
            )

    def _runtime_with_file_object(
        self,
        *,
        source: MedicalSource,
        runtime: MedicalSourceRuntime,
        file_object: Any,
        state: str,
        sync_status: str,
        lock_owner: str | None = None,
        lease_expires_at: str | None = None,
    ) -> MedicalSourceRuntime:
        now = now_kst_iso()
        gemini_file = GeminiFileRuntime(
            file_name=self._file_name(file_object) or runtime.gemini_file.file_name,
            uri=getattr(file_object, "uri", "") or runtime.gemini_file.uri,
            mime_type=getattr(file_object, "mime_type", "") or runtime.gemini_file.mime_type or source.source_ref.mime_type,
            state=self._coerce_file_state(state),
            expiration_time=self._file_expiration_time(file_object) or runtime.gemini_file.expiration_time,
            uploaded_at=runtime.gemini_file.uploaded_at or now,
            last_checked_at=now,
            error=None,
            source_drive_modified_at=source.source_ref.drive_modified_at,
            source_file_hash=source.source_ref.file_hash,
            source_file_size_bytes=source.source_ref.file_size_bytes,
        )
        return runtime.model_copy(
            update={
                "gemini_file": gemini_file,
                "sync": runtime.sync.model_copy(
                    update={
                        "status": sync_status,
                        "lock_owner": lock_owner,
                        "lease_expires_at": lease_expires_at,
                    }
                ),
                "updated_at": now,
            }
        )

    def _download_source_to_path(self, drive_file_id: str, destination_path: str) -> None:
        download_to_path = getattr(self.drive_gateway, "download_file_to_path", None)
        if callable(download_to_path):
            download_to_path(drive_file_id, destination_path)
            return
        with open(destination_path, "wb") as destination:
            destination.write(self.drive_gateway.download_file_bytes(drive_file_id))

    def _runtime_ready(self, runtime: MedicalSourceRuntime) -> MedicalSourceRuntime:
        return runtime.model_copy(
            update={
                "sync": runtime.sync.model_copy(
                    update={
                        "status": "READY",
                        "lock_owner": None,
                        "lease_expires_at": None,
                        "retry_count": 0,
                        "last_failure": None,
                        "last_synced_at": now_kst_iso(),
                    }
                ),
                "updated_at": now_kst_iso(),
            }
        )

    def _mark_runtime_file_expired(self, *, source: MedicalSource, runtime: MedicalSourceRuntime, reason: str) -> None:
        expired = runtime.model_copy(
            update={
                "gemini_file": runtime.gemini_file.model_copy(
                    update={"state": "EXPIRED", "error": reason, "last_checked_at": now_kst_iso()}
                ),
                "sync": runtime.sync.model_copy(update={"status": "EXPIRED", "lock_owner": None, "lease_expires_at": None}),
                "updated_at": now_kst_iso(),
            }
        )
        self.repository.upsert_source_runtime(expired)

    def _record_file_failure(
        self,
        *,
        source: MedicalSource,
        runtime: MedicalSourceRuntime,
        code: str,
        message: str,
        state: str = "FAILED",
    ) -> None:
        failure = RuntimeFailure(
            code=code,
            message=self._safe_error_message(message),
            failed_at=now_kst_iso(),
        )
        failed = runtime.model_copy(
            update={
                "gemini_file": runtime.gemini_file.model_copy(
                    update={
                        "state": self._coerce_file_state(state),
                        "error": code,
                        "last_checked_at": now_kst_iso(),
                        "source_drive_modified_at": source.source_ref.drive_modified_at,
                        "source_file_hash": source.source_ref.file_hash,
                        "source_file_size_bytes": source.source_ref.file_size_bytes,
                    }
                ),
                "sync": runtime.sync.model_copy(
                    update={
                        "status": "FAILED",
                        "lock_owner": None,
                        "lease_expires_at": None,
                        "retry_count": runtime.sync.retry_count + 1,
                        "last_failure": failure,
                    }
                ),
                "updated_at": now_kst_iso(),
            }
        )
        self.repository.upsert_source_runtime(failed)

    def _source_snapshot_matches(self, *, source: MedicalSource, gemini_file: GeminiFileRuntime) -> bool:
        return (
            gemini_file.source_drive_modified_at == source.source_ref.drive_modified_at
            and gemini_file.source_file_hash == source.source_ref.file_hash
            and gemini_file.source_file_size_bytes == source.source_ref.file_size_bytes
        )

    def _expiration_is_valid(self, expiration_time: str) -> bool:
        if not expiration_time:
            return False
        try:
            expiration = datetime.fromisoformat(expiration_time.replace("Z", "+00:00"))
        except ValueError:
            return False
        if expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=timezone.utc)
        margin = timedelta(seconds=self.settings.file_expiration_margin_seconds)
        return expiration > datetime.now(timezone.utc) + margin

    def _normalize_file_state(self, file_object: Any) -> str:
        raw = getattr(file_object, "state", "") or ""
        if hasattr(raw, "name"):
            raw = raw.name
        value = str(raw).split(".")[-1].upper()
        if value in {"ACTIVE", "UPLOADING", "PROCESSING", "EXPIRED", "FAILED"}:
            return value
        return "PROCESSING"

    def _coerce_file_state(self, state: str) -> str:
        value = (state or "").upper()
        if value in {"NONE", "UPLOADING", "PROCESSING", "ACTIVE", "EXPIRED", "FAILED"}:
            return value
        return "PROCESSING"

    def _file_name(self, file_object: Any) -> str:
        return str(getattr(file_object, "name", "") or getattr(file_object, "file_name", "") or "")

    def _file_expiration_time(self, file_object: Any) -> str:
        value = getattr(file_object, "expiration_time", "") or ""
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    def _safe_error_message(self, message: str) -> str:
        return " ".join(str(message).split())[:300]

    def answer_question(
        self,
        *,
        patient_id: str,
        question: str,
        prior_context: str,
        prepared_file: PreparedGeminiFile | None = None,
        intent: MedicalQuestionIntent = "OK",
    ) -> FinalQaAnswer:
        source_id = prepared_file.source_id if prepared_file is not None else ""
        prompt = build_gemini_files_qa_prompt(
            question=question,
            prior_context=prior_context,
            source_id=source_id,
            intent=intent,
        )
        contents = [prompt]
        if prepared_file is not None:
            contents = [prepared_file.file_object, prompt]
        raw_answer = self.gateway.generate_json(
            model=self.settings.gemini_model,
            system_instruction=build_gemini_files_qa_system_instruction(),
            contents=contents,
            response_schema=build_gemini_files_qa_response_schema(),
            temperature=self.settings.gemini_temperature,
            max_output_tokens=self.settings.gemini_max_output_tokens,
            thinking_level=self.settings.gemini_thinking_level,
        )
        answer = FinalQaAnswer.model_validate_json(raw_answer)
        self._validate_answer(
            patient_id=patient_id,
            answer=answer,
            expected_source_id=source_id,
            intent=intent,
        )
        return answer

    def render_answer(
        self,
        *,
        patient_id: str,
        answer: FinalQaAnswer,
        intent: MedicalQuestionIntent = "OK",
        selected_source_id: str = "",
    ) -> str:
        fixed_messages = {
            "emergency": "🧑‍⚕️ 증상이 지속된다면 의료기관을 찾아 전문의와 상의해 보시길 권합니다.",
            "blocked": "🔒 개인정보 보호 정책에 따라 세부 개인정보는 안내해 드리지 않습니다.",
            "cost_block": "💳 비용 관련 정보는 해당 의료기관에 직접 문의하셔야 합니다.",
            "out_of_scope": "💬 의료 기록과 관련된 질문에만 답변을 드릴 수 있습니다.",
            "cannot_verify": "🔍 해당 내용은 제공된 의료 기록에서 확인하기 어렵습니다.",
        }
        if answer.status != "ok":
            return fixed_messages[answer.status]

        body = self._render_answer_body(
            answer=answer,
            intent=intent,
            selected_source_id=selected_source_id,
        )
        if not self._should_show_sources(
            answer=answer,
            intent=intent,
            selected_source_id=selected_source_id,
        ):
            return body

        source_lines: list[str] = []
        for source_id in self._ordered_used_source_ids(answer.used_source_ids):
            page = self.repository.get_wiki_page_by_source(patient_id, source_id)
            source_lines.append(
                format_medical_source_display_name(
                    category=page.frontmatter.category if page is not None else "unknown",
                    date=page.frontmatter.date if page is not None else "",
                    page_count=page.frontmatter.page_count if page is not None else None,
                )
            )

        if not source_lines:
            return fixed_messages["cannot_verify"]
        rendered_source_lines = [
            f"{index}. {source_line}"
            for index, source_line in enumerate(source_lines, start=1)
        ]
        return body + "\n\n" + "📄 출처\n" + "\n".join(rendered_source_lines)

    def _render_answer_body(
        self,
        *,
        answer: FinalQaAnswer,
        intent: MedicalQuestionIntent,
        selected_source_id: str,
    ) -> str:
        del intent, selected_source_id
        return answer.kakaotalk_render.strip()

    def _strip_patient_record_section(self, body: str) -> str:
        stripped = (body or "").strip()
        if not stripped:
            return ""
        markers = [
            "📋 내 기록 확인",
            "📋 내 진료 기록 확인",
            "📋 내 진단 기록 확인",
            "내 기록 확인",
            "내 진료 기록 확인",
            "내 진단 기록 확인",
        ]
        earliest = len(stripped)
        for marker in markers:
            index = stripped.find(marker)
            if index >= 0:
                earliest = min(earliest, index)
        return stripped[:earliest].strip() if earliest != len(stripped) else stripped

    def _has_used_source(
        self,
        *,
        answer: FinalQaAnswer,
        selected_source_id: str,
    ) -> bool:
        if not selected_source_id:
            return False
        return selected_source_id in set(answer.used_source_ids)

    def _should_show_sources(
        self,
        *,
        answer: FinalQaAnswer,
        intent: MedicalQuestionIntent,
        selected_source_id: str,
    ) -> bool:
        if answer.status != "ok":
            return False
        if intent != "OK":
            return False
        return self._has_used_source(
            answer=answer,
            selected_source_id=selected_source_id,
        )

    def _ordered_used_source_ids(self, source_ids: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for source_id in source_ids:
            if not source_id or source_id in seen:
                continue
            seen.add(source_id)
            ordered.append(source_id)
        return ordered

    def to_model_answer(self, answer: FinalQaAnswer) -> ModelAnswer:
        return ModelAnswer(
            status=answer.status if answer.status != "cannot_verify" else "cannot_verify",
            evidence="",
            kakaotalk_render=answer.kakaotalk_render,
            used_source_ids=answer.used_source_ids,
        )

    def _validate_answer(
        self,
        *,
        patient_id: str,
        answer: FinalQaAnswer,
        expected_source_id: str,
        intent: MedicalQuestionIntent,
    ) -> None:
        if answer.status != "ok":
            if answer.used_source_ids:
                raise GeminiFilesQaError("ok가 아닌 응답에 source가 포함되었습니다.")
            return
        if not answer.kakaotalk_render.strip():
            raise GeminiFilesQaError("ok 응답의 kakaotalk_render가 비어 있습니다.")

        if intent != "OK":
            raise GeminiFilesQaError("Final QA는 OK intent만 처리할 수 있습니다.")

        if not expected_source_id and answer.used_source_ids:
            raise GeminiFilesQaError("source 없는 답변에 source가 포함되었습니다.")

        allowed_source_ids = {expected_source_id} if expected_source_id else set()
        if len(answer.used_source_ids) != len(set(answer.used_source_ids)):
            raise GeminiFilesQaError("답변 source가 중복되었습니다.")
        if len(answer.used_source_ids) > 1:
            raise GeminiFilesQaError("Final QA는 단일 source만 사용할 수 있습니다.")
        for source_id in answer.used_source_ids:
            if source_id not in allowed_source_ids:
                raise GeminiFilesQaError("답변 source가 최종 QA에 전달된 문서와 일치하지 않습니다.")
            self._validate_source(patient_id=patient_id, source_id=source_id)

    def _validate_source(self, *, patient_id: str, source_id: str) -> MedicalSource:
        source = self.repository.get_medical_source(patient_id, source_id)
        runtime = self.repository.get_source_runtime(patient_id, source_id)
        if source is None or runtime is None:
            raise GeminiFilesQaError("답변 source가 현재 환자 문서가 아닙니다.")
        if source.patient_id != patient_id or source.source_status != "ACTIVE":
            raise GeminiFilesQaError("답변 source 소유권이 올바르지 않습니다.")
        return source

def build_default_gemini_files_qa_service(
    *,
    settings: Settings,
    repository: MedicalRepository,
    drive_gateway: DriveGateway,
) -> GeminiFilesQaService:
    if not settings.gemini_api_key:
        raise GeminiFilesQaError("Gemini API key is not configured.")
    return GeminiFilesQaService(
        settings=settings,
        repository=repository,
        drive_gateway=drive_gateway,
        gateway=GoogleGeminiFilesGateway(
            settings.gemini_api_key,
            timeout_ms=settings.gemini_http_timeout_ms,
        ),
    )
