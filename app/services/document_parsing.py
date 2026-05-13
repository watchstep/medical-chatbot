from __future__ import annotations

import json
import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:  # pragma: no cover - optional dependency guard
    PdfReader = None  # type: ignore[assignment]
    PdfWriter = None  # type: ignore[assignment]

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover - optional dependency guard
    fitz = None  # type: ignore[assignment]

try:
    from google import genai
    from google.genai import errors, types
except ImportError:  # pragma: no cover - optional dependency guard
    genai = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]
    errors = None  # type: ignore[assignment]

from app.config import Settings
from app.prompts.document_parsing import (
    build_document_parsing_prompt,
    build_document_parsing_system_instruction,
    build_page_parsing_response_schema,
)


logger = logging.getLogger(__name__)

PDF_MIME_TYPE = "application/pdf"
PNG_MIME_TYPE = "image/png"
MARKDOWN_TABLE_ROW_RE = re.compile(r"^\s*\|.+\|\s*$", re.MULTILINE)
PAGE_MARKER_RE = re.compile(r"<!--\s*PAGE_START:\s*\d+\s*-->", re.IGNORECASE)
PAGE_START_RE = re.compile(r"<!--\s*PAGE_START:\s*(\d+)\s*-->", re.IGNORECASE)
PAGE_END_RE = re.compile(r"<!--\s*PAGE_END:\s*(\d+)\s*-->", re.IGNORECASE)

# 새 response schema는 markdown 하나만 반환한다.
ALLOWED_PAGE_JSON_KEYS = {"markdown"}

# Gemini/API 일시 오류 retry 대상.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
DEFAULT_MAX_GEMINI_ATTEMPTS = 5
DEFAULT_RETRY_BASE_DELAY_SECONDS = 3.0
DEFAULT_RETRY_MAX_DELAY_SECONDS = 60.0
DEFAULT_PAGE_RENDER_DPI = 300
DEFAULT_MAX_PAGE_WORKERS = 4

# 서버 단에서는 명확한 번호형 PHI만 한 번 더 마스킹한다.
# 환자 이름, 나이, 성별, 병원명은 파싱 결과에서 보존한다.
KOREAN_RRN_RE = re.compile(r"\b\d{6}[-\s]?[1-4]\d{6}\b")
PHONE_NUMBER_RE = re.compile(
    r"(?<!\d)(?:\+82[-\s]?)?(?:0(?:2|[3-6][1-5]|70|10|11|16|17|18|19))[-\s]?\d{3,4}[-\s]?\d{4}(?!\d)"
)


class DocumentParsingError(Exception):
    pass


@dataclass(frozen=True)
class ParsingDocument:
    name: str
    content: bytes
    mime_type: str = PDF_MIME_TYPE
    source_id: str = ""


@dataclass(frozen=True)
class ParsingGenerationConfig:
    model: str
    max_output_tokens: int
    temperature: float
    top_k: int | None
    thinking_level: str


@dataclass(frozen=True)
class PageParsingPayload:
    markdown: str


@dataclass(frozen=True)
class DocumentParsingResult:
    markdown: str
    character_count: int
    table_row_count: int
    page_marker_count: int
    elapsed_seconds: float
    generation_config: ParsingGenerationConfig


class DocumentParsingGateway:
    def parse_page_to_json(
        self,
        *,
        model: str,
        document: ParsingDocument,
        system_instruction: str,
        prompt: str,
        max_output_tokens: int,
        temperature: float,
        top_k: int | None,
        thinking_level: str,
    ) -> dict[str, Any]:
        raise NotImplementedError


class GoogleGeminiDocumentParsingGateway(DocumentParsingGateway):
    def __init__(self, api_key: str, *, client: Any | None = None):
        normalized_api_key = (api_key or "").strip().strip('"').strip("'")
        if not normalized_api_key:
            raise DocumentParsingError("Gemini API key is empty.")
        if genai is None or types is None:
            raise DocumentParsingError(
                "Gemini 문서 파싱에 필요한 google-genai 패키지가 없습니다."
            )
        self.client = client or genai.Client(api_key=normalized_api_key)

    def parse_page_to_json(
        self,
        *,
        model: str,
        document: ParsingDocument,
        system_instruction: str,
        prompt: str,
        max_output_tokens: int,
        temperature: float,
        top_k: int | None,
        thinking_level: str,
    ) -> dict[str, Any]:
        """단일 page image를 Gemini에 전달하고 JSON 객체를 반환한다.

        - 이 함수는 서버에서 렌더링한 1 page image를 받는 것을 전제로 한다.
        - Gemini 응답 schema는 {"markdown": string} 하나만 허용한다.
        - 429/5xx/503 등 일시 오류는 지수 backoff 후 재시도한다.
        """

        self._validate_document(document)
        response = None
        last_exc: Exception | None = None

        for attempt in range(1, DEFAULT_MAX_GEMINI_ATTEMPTS + 1):
            try:
                response = self.client.models.generate_content(
                    model=model,
                    contents=[
                        types.Part.from_bytes(
                            data=document.content,
                            mime_type=document.mime_type or PDF_MIME_TYPE,
                        ),
                        prompt,
                    ],
                    config=types.GenerateContentConfig(
                        system_instruction=system_instruction,
                        temperature=temperature,
                        top_k=top_k,
                        max_output_tokens=max_output_tokens,
                        response_mime_type="application/json",
                        response_schema=build_page_parsing_response_schema(),
                        thinking_config=types.ThinkingConfig(
                            thinking_level=thinking_level,
                        ),
                    ),
                )
                break
            except Exception as exc:  # noqa: BLE001 - SDK별 오류 래핑 차이를 흡수한다.
                last_exc = exc
                if not is_retryable_gemini_error(exc) or attempt >= DEFAULT_MAX_GEMINI_ATTEMPTS:
                    logger.exception(
                        "Gemini document parsing failed model=%s document=%s source_id=%s attempt=%s",
                        model,
                        document.name,
                        document.source_id,
                        attempt,
                    )
                    raise DocumentParsingError("Gemini 문서 파싱에 실패했습니다.") from exc

                delay = retry_delay_seconds(attempt)
                logger.warning(
                    "Retryable Gemini document parsing error. model=%s document=%s source_id=%s attempt=%s/%s delay=%.2fs error=%r",
                    model,
                    document.name,
                    document.source_id,
                    attempt,
                    DEFAULT_MAX_GEMINI_ATTEMPTS,
                    delay,
                    exc,
                )
                time.sleep(delay)

        if response is None:
            raise DocumentParsingError("Gemini 문서 파싱에 실패했습니다.") from last_exc

        return parse_gemini_json_response(response, document_name=document.name)

    def _validate_document(self, document: ParsingDocument) -> None:
        if not document.name:
            raise DocumentParsingError("파싱할 문서명이 비어 있습니다.")
        if not document.content:
            raise DocumentParsingError(f"파싱할 문서 내용이 비어 있습니다: {document.name}")
        if document.mime_type == PDF_MIME_TYPE and not document.content.startswith(b"%PDF"):
            raise DocumentParsingError(
                f"PDF 문서가 PDF 바이트로 보이지 않습니다: {document.name}"
            )
        if document.mime_type == PNG_MIME_TYPE and not document.content.startswith(
            b"\x89PNG\r\n\x1a\n"
        ):
            raise DocumentParsingError(
                f"PNG 문서가 PNG 바이트로 보이지 않습니다: {document.name}"
            )


@dataclass
class DocumentParsingService:
    settings: Settings
    gateway: DocumentParsingGateway

    def parse_document(
        self,
        *,
        document: ParsingDocument,
        model: str | None = None,
        max_output_tokens: int | None = None,
    ) -> DocumentParsingResult:
        """PDF를 page 단위로 분할하고, 각 page image를 Markdown으로 변환한다."""

        model_name = (
            model or self.settings.gemini_parsing_model or self.settings.gemini_model
        )
        token_limit = max_output_tokens or self.settings.gemini_parsing_max_output_tokens
        generation_config = ParsingGenerationConfig(
            model=model_name,
            max_output_tokens=token_limit,
            temperature=self.settings.gemini_parsing_temperature,
            top_k=self.settings.gemini_parsing_top_k,
            thinking_level=self.settings.gemini_parsing_thinking_level,
        )

        logger.info(
            "Start document parsing model=%s document=%s source_id=%s",
            generation_config.model,
            document.name,
            document.source_id,
        )

        parse_started_at = time.perf_counter()
        markdown_pages: list[tuple[int, str]] = []

        # 1. PDF를 먼저 page 단위의 1-page PDF들로 분할한다.
        page_documents = split_pdf_document_pages(document)
        validate_split_page_documents(page_documents)
        total_page_count = len(page_documents)

        max_workers = max(1, min(DEFAULT_MAX_PAGE_WORKERS, total_page_count))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self._parse_page,
                    page_number=page_number,
                    page_document=page_document,
                    generation_config=generation_config,
                ): page_number
                for page_number, page_document in page_documents
            }
            for future in as_completed(futures):
                page_number = futures[future]
                try:
                    parsed_page_number, page_markdown = future.result()
                    markdown_pages.append((parsed_page_number, page_markdown))
                except Exception as exc:
                    raise DocumentParsingError(
                        f"{page_number} page Markdown 파싱에 실패했습니다."
                    ) from exc

        # 4. 서버 단에서 page 번호 순으로 정렬한 뒤 boundary marker를 검증하며 병합한다.
        markdown = concat_page_markdowns(
            markdown_pages,
            expected_page_count=total_page_count,
        )
        validate_merged_page_markers(markdown, expected_page_count=total_page_count)
        elapsed_seconds = time.perf_counter() - parse_started_at

        return build_document_parsing_result(
            markdown,
            elapsed_seconds=elapsed_seconds,
            generation_config=generation_config,
        )

    def _parse_page(
        self,
        *,
        page_number: int,
        page_document: ParsingDocument,
        generation_config: ParsingGenerationConfig,
    ) -> tuple[int, str]:
        image_document = render_page_document_to_image(
            page_document,
            page_number=page_number,
        )
        page_json = self.gateway.parse_page_to_json(
            model=generation_config.model,
            document=image_document,
            system_instruction=build_document_parsing_system_instruction(),
            prompt=build_document_parsing_prompt(),
            max_output_tokens=generation_config.max_output_tokens,
            temperature=generation_config.temperature,
            top_k=generation_config.top_k,
            thinking_level=generation_config.thinking_level,
        )
        payload = validate_page_json_payload(
            page_json,
            page_number=page_number,
        )
        return page_number, render_page_markdown(payload, page_number=page_number)


def is_retryable_gemini_error(exc: Exception) -> bool:
    if errors is None or not hasattr(errors, "APIError"):
        return False
    if not isinstance(exc, errors.APIError):
        return False
    status_code = getattr(exc, "status_code", None)
    return status_code in RETRYABLE_STATUS_CODES


def retry_delay_seconds(attempt: int) -> float:
    delay = min(
        DEFAULT_RETRY_MAX_DELAY_SECONDS,
        DEFAULT_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)),
    )
    jitter = random.uniform(0.0, delay * 0.25)
    return delay + jitter


def parse_gemini_json_response(response: Any, *, document_name: str) -> dict[str, Any]:
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, dict):
        return parsed

    text = (getattr(response, "text", "") or "").strip()
    if not text:
        raise DocumentParsingError(f"Gemini JSON 응답이 비어 있습니다: {document_name}")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DocumentParsingError(
            f"Gemini 응답이 유효한 JSON이 아닙니다: {document_name}"
        ) from exc
    if not isinstance(payload, dict):
        raise DocumentParsingError(
            f"Gemini JSON 응답이 객체가 아닙니다: {document_name}"
        )
    return payload


def split_pdf_document_pages(document: ParsingDocument) -> list[tuple[int, ParsingDocument]]:
    if document.mime_type != PDF_MIME_TYPE:
        return [(1, document)]
    if PdfReader is None or PdfWriter is None:
        raise DocumentParsingError("PDF page 분리에 필요한 pypdf 패키지가 없습니다.")
    try:
        reader = PdfReader(BytesIO(document.content))
    except Exception as exc:
        raise DocumentParsingError(f"PDF page를 읽을 수 없습니다: {document.name}") from exc
    if not reader.pages:
        raise DocumentParsingError(f"PDF에 page가 없습니다: {document.name}")

    page_documents: list[tuple[int, ParsingDocument]] = []
    for index, page in enumerate(reader.pages, start=1):
        writer = PdfWriter()
        writer.add_page(page)
        output = BytesIO()
        writer.write(output)
        page_documents.append(
            (
                index,
                ParsingDocument(
                    name=f"{document.name}#page={index}",
                    content=output.getvalue(),
                    mime_type=document.mime_type,
                    source_id=document.source_id,
                ),
            )
        )
    return page_documents


def render_page_document_to_image(
    page_document: ParsingDocument,
    *,
    page_number: int,
    dpi: int = DEFAULT_PAGE_RENDER_DPI,
) -> ParsingDocument:
    """1-page PDF를 Gemini가 시각적으로 읽을 PNG image로 렌더링한다."""

    if page_document.mime_type != PDF_MIME_TYPE:
        validate_rendered_page_image_document(page_document, page_number=page_number)
        return page_document
    if fitz is None:
        raise DocumentParsingError(
            "PDF page image 렌더링에 필요한 pymupdf 패키지가 없습니다."
        )
    if not page_document.content.startswith(b"%PDF"):
        raise DocumentParsingError(f"{page_number} page PDF가 PDF 바이트로 보이지 않습니다.")
    if dpi < 72:
        raise DocumentParsingError(f"PDF page image 렌더링 DPI가 너무 낮습니다: {dpi}")

    try:
        pdf = fitz.open(stream=page_document.content, filetype="pdf")
    except Exception as exc:
        raise DocumentParsingError(f"{page_number} page PDF를 렌더링할 수 없습니다.") from exc

    try:
        if pdf.page_count != 1:
            raise DocumentParsingError(
                f"{page_number} page 렌더링 입력은 1-page PDF여야 합니다: {pdf.page_count}"
            )
        page = pdf.load_page(0)
        pixmap = page.get_pixmap(dpi=dpi, alpha=False)
        image_bytes = pixmap.tobytes("png")
    except DocumentParsingError:
        raise
    except Exception as exc:
        raise DocumentParsingError(f"{page_number} page image 렌더링에 실패했습니다.") from exc
    finally:
        pdf.close()

    image_document = ParsingDocument(
        name=f"{page_document.name}.png",
        content=image_bytes,
        mime_type=PNG_MIME_TYPE,
        source_id=page_document.source_id,
    )
    validate_rendered_page_image_document(image_document, page_number=page_number)
    return image_document


def validate_rendered_page_image_document(
    image_document: ParsingDocument,
    *,
    page_number: int,
) -> None:
    if not image_document.content:
        raise DocumentParsingError(f"{page_number} page image 내용이 비어 있습니다.")
    if image_document.mime_type != PNG_MIME_TYPE:
        raise DocumentParsingError(
            f"{page_number} page image MIME type이 올바르지 않습니다: {image_document.mime_type}"
        )
    if not image_document.content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise DocumentParsingError(f"{page_number} page image가 PNG 바이트로 보이지 않습니다.")


def validate_split_page_documents(page_documents: list[tuple[int, ParsingDocument]]) -> None:
    if not page_documents:
        raise DocumentParsingError("분리된 PDF page가 없습니다.")
    expected_page_numbers = list(range(1, len(page_documents) + 1))
    actual_page_numbers = [page_number for page_number, _ in page_documents]
    if actual_page_numbers != expected_page_numbers:
        raise DocumentParsingError("분리된 PDF page 번호가 연속적이지 않습니다.")

    for page_number, page_document in page_documents:
        if not page_document.content:
            raise DocumentParsingError(f"{page_number} page PDF 내용이 비어 있습니다.")
        if (
            page_document.mime_type == PDF_MIME_TYPE
            and not page_document.content.startswith(b"%PDF")
        ):
            raise DocumentParsingError(
                f"{page_number} page PDF가 PDF 바이트로 보이지 않습니다."
            )


def expected_page_start_marker(page_number: int) -> str:
    return f"<!-- PAGE_START: {page_number:04d} -->"


def expected_page_end_marker(page_number: int) -> str:
    return f"<!-- PAGE_END: {page_number:04d} -->"


def validate_page_json_payload(
    payload: dict[str, Any],
    *,
    page_number: int,
) -> PageParsingPayload:
    extra_keys = set(payload) - ALLOWED_PAGE_JSON_KEYS
    if extra_keys:
        raise DocumentParsingError(
            f"{page_number} page JSON에 허용되지 않은 필드가 있습니다: {sorted(extra_keys)}"
        )

    markdown = payload.get("markdown")
    if not isinstance(markdown, str):
        raise DocumentParsingError(f"{page_number} page JSON markdown이 문자열이 아닙니다.")

    normalized_markdown = validate_page_body_markdown(
        markdown,
        page_number=page_number,
    )
    return PageParsingPayload(markdown=normalized_markdown)


def validate_page_body_markdown(markdown: str, *, page_number: int) -> str:
    normalized = normalize_page_markdown(markdown)
    if not normalized:
        raise DocumentParsingError(f"{page_number} page Markdown 파싱 결과가 비어 있습니다.")

    if PAGE_START_RE.search(normalized) or PAGE_END_RE.search(normalized):
        raise DocumentParsingError(
            f"{page_number} page Markdown 본문에 page boundary marker가 포함되어 있습니다."
        )
    return normalized


def normalize_page_markdown(markdown: str) -> str:
    """Gemini가 반환한 page markdown을 서버 단에서 정규화한다.

    - JSON 안 markdown 값이 실수로 ```markdown fence를 포함하면 제거한다.
    - 주민등록번호/전화번호 같은 명확한 번호형 PHI는 한 번 더 ***로 치환한다.
    - 환자 이름, 나이, 성별, 병원명은 보존한다.
    - 페이지 boundary marker는 render_page_markdown에서만 붙인다.
    """

    normalized = markdown.strip()
    normalized = strip_markdown_code_fence(normalized)
    normalized = redact_obvious_phi(normalized)
    return normalized.strip()


def strip_markdown_code_fence(text: str) -> str:
    stripped = text.strip()
    fence_match = re.fullmatch(r"```(?:markdown|md)?\s*\n(?P<body>.*)\n```", stripped, re.DOTALL | re.IGNORECASE)
    if fence_match:
        return fence_match.group("body").strip()
    return stripped


def redact_obvious_phi(text: str) -> str:
    redacted = KOREAN_RRN_RE.sub("***", text)
    redacted = PHONE_NUMBER_RE.sub("***", redacted)
    return redacted


def render_page_markdown(payload: PageParsingPayload, *, page_number: int) -> str:
    body = validate_page_body_markdown(payload.markdown, page_number=page_number)
    return (
        f"{expected_page_start_marker(page_number)}\n"
        f"{body}\n"
        f"{expected_page_end_marker(page_number)}"
    )


def concat_page_markdowns(
    markdown_pages: list[tuple[int, str]],
    *,
    expected_page_count: int,
) -> str:
    if not markdown_pages:
        raise DocumentParsingError("Markdown page 파싱 결과가 비어 있습니다.")
    ordered_pages = sorted(markdown_pages, key=lambda item: item[0])
    actual_page_numbers = [page_number for page_number, _ in ordered_pages]
    expected_page_numbers = list(range(1, expected_page_count + 1))
    if actual_page_numbers != expected_page_numbers:
        raise DocumentParsingError(
            f"Markdown page 결과 번호가 올바르지 않습니다: {actual_page_numbers}"
        )
    return "\n\n".join(markdown for _, markdown in ordered_pages)


def validate_merged_page_markers(markdown: str, *, expected_page_count: int) -> None:
    starts = [int(value) for value in PAGE_START_RE.findall(markdown)]
    ends = [int(value) for value in PAGE_END_RE.findall(markdown)]
    expected = list(range(1, expected_page_count + 1))
    if starts != expected or ends != expected:
        raise DocumentParsingError(
            "병합 Markdown page marker 개수가 올바르지 않습니다."
        )


def build_document_parsing_result(
    markdown: str,
    *,
    elapsed_seconds: float = 0.0,
    generation_config: ParsingGenerationConfig | None = None,
) -> DocumentParsingResult:
    normalized = markdown.strip()
    if not normalized:
        raise DocumentParsingError("Markdown 파싱 결과가 비어 있습니다.")
    config = generation_config or ParsingGenerationConfig(
        model="",
        max_output_tokens=0,
        temperature=0.0,
        top_k=None,
        thinking_level="",
    )
    return DocumentParsingResult(
        markdown=normalized,
        character_count=len(normalized),
        table_row_count=len(MARKDOWN_TABLE_ROW_RE.findall(normalized)),
        page_marker_count=len(PAGE_MARKER_RE.findall(normalized)),
        elapsed_seconds=elapsed_seconds,
        generation_config=config,
    )


def build_default_document_parsing_service(settings: Settings) -> DocumentParsingService:
    if not settings.gemini_api_key:
        raise DocumentParsingError("Gemini API key is not configured.")
    return DocumentParsingService(
        settings=settings,
        gateway=GoogleGeminiDocumentParsingGateway(settings.gemini_api_key),
    )


def default_parsed_markdown_path(*, source_id: str, base_dir: str = "artifacts/parsing") -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", source_id).strip("._") or "document"
    return Path(base_dir) / safe_id / "parsed.md"


def extract_drive_file_id(file_id_or_url: str) -> str:
    value = file_id_or_url.strip()
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"[?&]id=([a-zA-Z0-9_-]+)",
        r"^([a-zA-Z0-9_-]{10,})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    raise DocumentParsingError(f"Google Drive file id를 찾을 수 없습니다: {file_id_or_url}")


def write_parsed_markdown(path: Path, markdown: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown.rstrip() + "\n", encoding="utf-8")


def write_parsing_metadata(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parsing_result_metadata(result: DocumentParsingResult) -> dict[str, object]:
    return {
        "character_count": result.character_count,
        "table_row_count": result.table_row_count,
        "page_marker_count": result.page_marker_count,
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "generation_config": asdict(result.generation_config),
    }
