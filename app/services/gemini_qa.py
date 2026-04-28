from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from threading import Lock

from google import genai
from google.genai import types

from app.config import Settings
from app.prompts.gemini_qa import (
    build_gemini_qa_context_note,
    build_gemini_qa_question_prompt,
    build_gemini_qa_system_instruction,
)
from app.schemas import DriveFile, PatientRecordContext
from app.services.drive import DriveLookupService


logger = logging.getLogger(__name__)
PDF_MIME_TYPE = "application/pdf"
FILE_PROCESSING_POLL_SECONDS = 1
FILE_PROCESSING_MAX_ATTEMPTS = 30


class GeminiQaError(Exception):
    pass


class GeminiRecordNotFoundError(GeminiQaError):
    pass


@dataclass(frozen=True)
class GeminiDocument:
    name: str
    content: bytes
    mime_type: str = PDF_MIME_TYPE


@dataclass(frozen=True)
class UploadedGeminiFile:
    name: str
    uri: str
    display_name: str


@dataclass(frozen=True)
class CachedGeminiContext:
    name: str
    uploaded_files: tuple[UploadedGeminiFile, ...]


@dataclass(frozen=True)
class GeminiCachePayload:
    key: str
    document_descriptions: str
    documents: tuple[GeminiDocument, ...]


@dataclass
class GeminiGateway:
    def create_cached_context(
        self,
        *,
        model: str,
        system_instruction: str,
        context_note: str,
        cache_key: str,
        documents: tuple[GeminiDocument, ...],
        ttl_hours: int,
    ) -> CachedGeminiContext:
        raise NotImplementedError

    def generate_answer(
        self,
        *,
        model: str,
        prompt: str,
        cached_content_name: str,
    ) -> str:
        raise NotImplementedError

    def delete_cached_context(self, *, cached_context: CachedGeminiContext) -> None:
        raise NotImplementedError


class GoogleGeminiGateway(GeminiGateway):
    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key)

    def create_cached_context(
        self,
        *,
        model: str,
        system_instruction: str,
        context_note: str,
        cache_key: str,
        documents: tuple[GeminiDocument, ...],
        ttl_hours: int,
    ) -> CachedGeminiContext:
        uploaded_files = tuple(self._upload_document(document) for document in documents)
        try:
            cache = self.client.caches.create(
                model=model,
                config=types.CreateCachedContentConfig(
                    display_name=f"patient-context-{cache_key[:48]}",
                    ttl=f"{ttl_hours * 3600}s",
                    system_instruction=system_instruction,
                    contents=[
                        context_note,
                        *[
                            types.Part.from_uri(
                                file_uri=uploaded_file.uri,
                                mime_type=PDF_MIME_TYPE,
                            )
                            for uploaded_file in uploaded_files
                        ],
                    ],
                ),
            )
        except Exception:
            for uploaded_file in uploaded_files:
                self._delete_file(uploaded_file.name)
            raise

        if not cache.name:
            for uploaded_file in uploaded_files:
                self._delete_file(uploaded_file.name)
            raise GeminiQaError("Gemini context cache 생성에 실패했습니다.")
        return CachedGeminiContext(
            name=cache.name,
            uploaded_files=uploaded_files,
        )

    def generate_answer(
        self,
        *,
        model: str,
        prompt: str,
        cached_content_name: str,
    ) -> str:
        response = self.client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                cached_content=cached_content_name,
                temperature=0.1,
                max_output_tokens=700,
            ),
        )
        text = (response.text or "").strip()
        if not text:
            raise GeminiQaError("Gemini 응답이 비어 있습니다.")
        return text

    def delete_cached_context(self, *, cached_context: CachedGeminiContext) -> None:
        try:
            self.client.caches.delete(name=cached_context.name)
        finally:
            for uploaded_file in cached_context.uploaded_files:
                self._delete_file(uploaded_file.name)

    def _upload_document(self, document: GeminiDocument) -> UploadedGeminiFile:
        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".pdf",
                delete=False,
            ) as temp_file:
                temp_file.write(document.content)
                temp_path = temp_file.name

            uploaded = self.client.files.upload(
                path=temp_path,
                config=types.UploadFileConfig(
                    mime_type=document.mime_type,
                    display_name=document.name,
                ),
            )
            active_file = self._wait_until_file_active(uploaded.name)
            if not active_file.name or not active_file.uri:
                raise GeminiQaError("Gemini 파일 업로드 결과가 올바르지 않습니다.")
            return UploadedGeminiFile(
                name=active_file.name,
                uri=active_file.uri,
                display_name=document.name,
            )
        except GeminiQaError:
            raise
        except Exception as exc:
            raise GeminiQaError("Gemini 파일 업로드에 실패했습니다.") from exc
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)

    def _wait_until_file_active(self, file_name: str | None) -> types.File:
        if not file_name:
            raise GeminiQaError("Gemini 파일 이름이 없습니다.")

        for _ in range(FILE_PROCESSING_MAX_ATTEMPTS):
            file = self.client.files.get(name=file_name)
            if file.state == "ACTIVE":
                return file
            if file.state == "FAILED":
                raise GeminiQaError("Gemini 파일 처리에 실패했습니다.")
            time.sleep(FILE_PROCESSING_POLL_SECONDS)

        raise GeminiQaError("Gemini 파일 처리가 지연되고 있습니다.")

    def _delete_file(self, file_name: str) -> None:
        try:
            self.client.files.delete(name=file_name)
        except Exception:
            logger.warning("Failed to delete Gemini file", extra={"file_name": file_name})


@dataclass
class GeminiQaService:
    settings: Settings
    gateway: GeminiGateway
    _cache_entries: dict[str, CachedGeminiContext] = field(default_factory=dict)
    _patient_cache_keys: dict[str, str] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def answer_question(
        self,
        *,
        question: str,
        context: PatientRecordContext,
        drive_service: DriveLookupService,
    ) -> str:
        logger.info("gemini_qa answer_question patient_id=%s", context.patient.patient_id)
        payload = self._build_cache_payload(context=context, drive_service=drive_service)
        cached_context = self._ensure_cached_context(
            patient_id=context.patient.patient_id,
            payload=payload,
        )
        prompt = build_gemini_qa_question_prompt(question=question)
        try:
            return self.gateway.generate_answer(
                model=self.settings.gemini_model,
                prompt=prompt,
                cached_content_name=cached_context.name,
            )
        except GeminiQaError:
            raise
        except Exception as exc:
            logger.exception("Gemini generate_answer failed")
            raise GeminiQaError("Gemini 답변 생성에 실패했습니다.") from exc

    def _build_cache_payload(
        self,
        *,
        context: PatientRecordContext,
        drive_service: DriveLookupService,
    ) -> GeminiCachePayload:
        documents = self._load_documents(context=context, drive_service=drive_service)
        if not documents:
            raise GeminiRecordNotFoundError("질의응답에 사용할 진단기록 PDF가 없습니다.")

        document_descriptions = "\n".join(
            f"- {document.name} ({document.date or '날짜 미상'})"
            for document, _ in documents
        )
        return GeminiCachePayload(
            key=self._build_document_cache_key(
                patient_id=context.patient.patient_id,
                documents=documents,
            ),
            document_descriptions=document_descriptions,
            documents=tuple(
                GeminiDocument(
                    name=document.name,
                    content=content,
                )
                for document, content in documents
            ),
        )

    def _ensure_cached_context(
        self,
        *,
        patient_id: str,
        payload: GeminiCachePayload,
    ) -> CachedGeminiContext:
        stale_context: CachedGeminiContext | None = None
        with self._lock:
            existing = self._cache_entries.get(payload.key)
            if existing is not None:
                self._patient_cache_keys[patient_id] = payload.key
                logger.info("gemini_context_cache hit patient_id=%s", patient_id)
                return existing

            previous_key = self._patient_cache_keys.get(patient_id)
            if previous_key and previous_key != payload.key:
                stale_context = self._cache_entries.pop(previous_key, None)
                self._patient_cache_keys.pop(patient_id, None)

        if stale_context is not None:
            try:
                self.gateway.delete_cached_context(cached_context=stale_context)
            except Exception:
                logger.warning(
                    "Failed to delete stale Gemini context cache",
                    extra={"patient_id": patient_id},
                )

        created = self.gateway.create_cached_context(
            model=self.settings.gemini_model,
            system_instruction=build_gemini_qa_system_instruction(
                document_descriptions=payload.document_descriptions,
            ),
            context_note=build_gemini_qa_context_note(
                document_descriptions=payload.document_descriptions,
            ),
            cache_key=payload.key,
            documents=payload.documents,
            ttl_hours=self.settings.gemini_context_cache_ttl_hours,
        )
        with self._lock:
            self._cache_entries[payload.key] = created
            self._patient_cache_keys[patient_id] = payload.key
        logger.info("gemini_context_cache created patient_id=%s", patient_id)
        return created

    def _load_documents(
        self,
        *,
        context: PatientRecordContext,
        drive_service: DriveLookupService,
    ) -> list[tuple[DriveFile, bytes]]:
        documents: list[tuple[DriveFile, bytes]] = []
        for document in (context.latest_result, context.latest_chart):
            if document is None:
                continue
            documents.append((document, drive_service.download_drive_file_bytes(document)))
        return documents

    def _build_document_cache_key(
        self,
        *,
        patient_id: str,
        documents: list[tuple[DriveFile, bytes]],
    ) -> str:
        raw_key = "|".join(
            f"{document.file_id}:{document.name}:{document.date or ''}"
            for document, _ in documents
        )
        return hashlib.sha256(f"{patient_id}|{raw_key}".encode("utf-8")).hexdigest()


def build_default_gemini_qa_service(settings: Settings) -> GeminiQaService:
    if not settings.gemini_api_key:
        raise GeminiQaError("Gemini API key is not configured.")
    return GeminiQaService(
        settings=settings,
        gateway=GoogleGeminiGateway(settings.gemini_api_key),
    )
