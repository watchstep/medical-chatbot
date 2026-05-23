from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

try:
    from google import genai
    from google.genai import types
except ImportError:  # pragma: no cover - optional dependency guard
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]

from app.config import Settings
from app.prompts.medical_wiki import (
    build_medical_wiki_extraction_prompt,
    build_medical_wiki_extraction_response_schema,
    build_medical_wiki_extraction_system_instruction,
)
from app.schemas import MedicalWikiExtractionResult


logger = logging.getLogger(__name__)


class MedicalWikiExtractionError(Exception):
    pass


@dataclass(frozen=True)
class WikiExtractionDocument:
    content: bytes
    mime_type: str
    source_id: str = ""


class MedicalWikiExtractionGateway:
    def extract_source_summary(
        self,
        *,
        model: str,
        document: WikiExtractionDocument,
        system_instruction: str,
        prompt: str,
        response_schema: dict[str, Any],
        temperature: float,
        max_output_tokens: int,
        thinking_level: str,
    ) -> dict[str, Any]:
        raise NotImplementedError


class GoogleGeminiMedicalWikiExtractionGateway(MedicalWikiExtractionGateway):
    def __init__(self, api_key: str, *, client: Any | None = None, timeout_ms: int | None = None):
        normalized_api_key = (api_key or "").strip().strip('"').strip("'")
        if not normalized_api_key:
            raise MedicalWikiExtractionError("Gemini API key is empty.")
        if genai is None or types is None:
            raise MedicalWikiExtractionError(
                "Medical Wiki extraction에 필요한 google-genai 패키지가 없습니다."
            )
        http_options = types.HttpOptions(timeout=timeout_ms) if timeout_ms else None
        self.client = client or genai.Client(api_key=normalized_api_key, http_options=http_options)

    def extract_source_summary(
        self,
        *,
        model: str,
        document: WikiExtractionDocument,
        system_instruction: str,
        prompt: str,
        response_schema: dict[str, Any],
        temperature: float,
        max_output_tokens: int,
        thinking_level: str,
    ) -> dict[str, Any]:
        if not document.content:
            raise MedicalWikiExtractionError("Wiki extraction 문서 내용이 비어 있습니다.")
        if not document.mime_type:
            raise MedicalWikiExtractionError("Wiki extraction 문서 MIME type이 비어 있습니다.")

        try:
            response = self.client.models.generate_content(
                model=model,
                contents=[
                    types.Part.from_bytes(
                        data=document.content,
                        mime_type=document.mime_type,
                    ),
                    prompt,
                ],
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    response_mime_type="application/json",
                    response_json_schema=response_schema,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    thinking_config=types.ThinkingConfig(thinking_level=thinking_level),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - SDK별 예외 래핑 차이를 흡수한다.
            logger.exception("Gemini Medical Wiki extraction failed source_id=%s", document.source_id)
            raise MedicalWikiExtractionError("Gemini Medical Wiki extraction에 실패했습니다.") from exc

        return parse_gemini_json_response(response)


@dataclass
class MedicalWikiExtractor:
    settings: Settings
    gateway: MedicalWikiExtractionGateway

    def extract(self, document: WikiExtractionDocument) -> MedicalWikiExtractionResult:
        model = self.settings.gemini_wiki_model or self.settings.gemini_model
        raw_payload = self.gateway.extract_source_summary(
            model=model,
            document=document,
            system_instruction=build_medical_wiki_extraction_system_instruction(),
            prompt=build_medical_wiki_extraction_prompt(),
            response_schema=build_medical_wiki_extraction_response_schema(),
            temperature=self.settings.gemini_wiki_temperature,
            max_output_tokens=self.settings.gemini_wiki_max_output_tokens,
            thinking_level=self.settings.gemini_wiki_thinking_level,
        )
        try:
            return MedicalWikiExtractionResult.model_validate(raw_payload)
        except Exception as exc:
            raise MedicalWikiExtractionError("Medical Wiki extraction JSON schema가 올바르지 않습니다.") from exc


def parse_gemini_json_response(response: Any) -> dict[str, Any]:
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, MedicalWikiExtractionResult):
        return parsed.model_dump(exclude_none=True)

    text = (getattr(response, "text", "") or "").strip()
    if not text:
        raise MedicalWikiExtractionError("Gemini Medical Wiki extraction 응답이 비어 있습니다.")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MedicalWikiExtractionError("Gemini Medical Wiki extraction 응답이 JSON이 아닙니다.") from exc
    if not isinstance(payload, dict):
        raise MedicalWikiExtractionError("Gemini Medical Wiki extraction 응답이 객체가 아닙니다.")
    return payload


def build_default_medical_wiki_extractor(settings: Settings) -> MedicalWikiExtractor:
    if settings.medical_wiki_extraction_mode == "metadata":
        raise MedicalWikiExtractionError("metadata mode에서는 Gemini extractor를 생성하지 않습니다.")
    if not settings.gemini_api_key:
        raise MedicalWikiExtractionError("Gemini API key is empty.")
    return MedicalWikiExtractor(
        settings=settings,
        gateway=GoogleGeminiMedicalWikiExtractionGateway(
            settings.gemini_api_key,
            timeout_ms=settings.gemini_http_timeout_ms,
        ),
    )
