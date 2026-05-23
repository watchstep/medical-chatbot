from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import PurePath

from app.config import Settings
from app.medical_wiki_categories import MEDICAL_WIKI_CATEGORY_DEFINITIONS, medical_wiki_category_label
from app.repositories import MedicalRepository
from app.schemas import (
    MedicalSource,
    MedicalWikiExtractionResult,
    MedicalWikiFrontmatter,
    MedicalWikiIndex,
    MedicalWikiIndexPage,
    MedicalWikiNavigation,
    MedicalWikiPage,
    MedicalWikiQuality,
)
from app.services.drive import DriveGateway
from app.services.medical_wiki_extractor import (
    MedicalWikiExtractionError,
    MedicalWikiExtractor,
    WikiExtractionDocument,
)


KST = timezone(timedelta(hours=9))
FILENAME_DATE_RE = re.compile(r"(20\d{2}|19\d{2})[-_.년 ]?(0[1-9]|1[0-2])[-_.월 ]?([0-3]\d)")
KOREAN_RRN_RE = re.compile(r"\b\d{6}[-\s]?[1-4]\d{6}\b")
PHONE_NUMBER_RE = re.compile(
    r"(?<!\d)(?:\+82[-\s]?)?(?:0(?:2|[3-6][1-5]|70|10|11|16|17|18|19))[-\s]?\d{3,4}[-\s]?\d{4}(?!\d)"
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
MEDICAL_VALUE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:mg/dL|mmHg|IU/L|U/L|g/dL|mg|㎎|mEq/L|ng/mL|pg/mL|%|정|회)\b",
    re.IGNORECASE,
)
FILE_EXTENSION_RE = re.compile(r"\.(?:pdf|png|jpe?g|webp|heic|heif)\b", re.IGNORECASE)
LONG_NUMERIC_RESULT_RE = re.compile(r"\b(?:\d+(?:\.\d+)?\s*[,/]\s*){2,}\d+(?:\.\d+)?\b")

MAX_DESCRIPTION_LEN = 180
MAX_ITEM_LEN = 60
MAX_TAGS = 8
MAX_ANCHORS = 12
MAX_OPEN_WHEN = 6
MAX_SKIP_WHEN = 6
MAX_WARNINGS = 6


class UnsafeMedicalWikiExtractionError(MedicalWikiExtractionError):
    pass


def now_kst_iso() -> str:
    return datetime.now(KST).isoformat()


def normalize_firestore_timestamp(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def format_medical_source_display_name(
    *,
    category: str | None,
    date: str | None = "",
    page_count: int | None = None,
) -> str:
    label = medical_wiki_category_label(category)
    parts = [part for part in [(date or "").strip(), f"{page_count}쪽" if page_count else ""] if part]
    if not parts:
        return label
    return f"{label} ({', '.join(parts)})"


def infer_document_date(source: MedicalSource) -> str:
    filename = source.source_ref.original_filename or ""
    match = FILENAME_DATE_RE.search(filename)
    if match:
        year, month, day = match.groups()
        return f"{year}-{month}-{day}"
    return ""


def infer_category(source: MedicalSource) -> str:
    name = (source.source_ref.original_filename or "").lower()
    if any(token in name for token in ["검진", "health", "checkup"]):
        return "health_checkup"
    if any(token in name for token in ["검사", "lab", "result", "혈액"]):
        return "lab_result"
    if any(token in name for token in ["처방", "약", "prescription"]):
        return "prescription"
    if any(token in name for token in ["영상", "ct", "mri", "xray", "x-ray", "image"]):
        return "imaging_report"
    if any(token in name for token in ["진료", "chart", "note", "기록"]):
        return "doctor_note"
    return "unknown"


def build_metadata_source_summary_page(
    source: MedicalSource,
    *,
    generated_by: str = "metadata",
) -> MedicalWikiPage:
    category = infer_category(source)
    date = infer_document_date(source)
    definition = MEDICAL_WIKI_CATEGORY_DEFINITIONS.get(category, MEDICAL_WIKI_CATEGORY_DEFINITIONS["unknown"])
    tags = list(definition.routing_hints[:MAX_TAGS])
    open_when = [f"{hint} 관련 질문" for hint in definition.routing_hints[:MAX_OPEN_WHEN]]
    skip_when = [f"{hint} 관련 질문" for hint in definition.exclusions[:MAX_SKIP_WHEN]]
    extraction = MedicalWikiExtractionResult(
        category=category,  # type: ignore[arg-type]
        date=date,
        date_source="filename" if date else "unknown",
        date_confidence=0.4 if date else 0.0,
        tags=tags,
        description=f"{definition.label} 내용을 확인해야 하는 질문에서 열어볼 수 있는 문서입니다.",
        anchors=tags,
        open_when=open_when,
        skip_when=skip_when,
        confidence=0.5,
        needs_review=False,
        warnings=["metadata 기반 source_summary입니다."],
    )
    return build_source_summary_page_from_extraction(
        source,
        extraction,
        generated_by=generated_by,
    )


# Backward-compatible alias for older tests/imports.
def build_source_summary_page(source: MedicalSource, *, generated_by: str = "metadata") -> MedicalWikiPage:
    return build_metadata_source_summary_page(source, generated_by=generated_by)


def build_source_summary_page_from_extraction(
    source: MedicalSource,
    extraction: MedicalWikiExtractionResult,
    *,
    generated_by: str,
) -> MedicalWikiPage:
    now = now_kst_iso()
    return MedicalWikiPage(
        page_id=f"PAGE_{source.source_id}",
        source_id=source.source_id,
        patient_id=source.patient_id,
        frontmatter=MedicalWikiFrontmatter(
            page_count=extraction.page_count,
            category=extraction.category,
            date=extraction.date,
            date_source=extraction.date_source,
            date_confidence=extraction.date_confidence,
            tags=extraction.tags,
        ),
        description=extraction.description,
        navigation=MedicalWikiNavigation(
            anchors=extraction.anchors,
            open_when=extraction.open_when,
            skip_when=extraction.skip_when,
        ),
        quality=MedicalWikiQuality(
            confidence=extraction.confidence,
            needs_review=extraction.needs_review,
            warnings=extraction.warnings,
        ),
        provenance={
            "generated_by": generated_by,
            "generated_at": now,
            "updated_at": now,
        },
    )


def validate_safe_wiki_extraction(
    *,
    source: MedicalSource,
    extraction: MedicalWikiExtractionResult,
) -> MedicalWikiExtractionResult:
    normalized = extraction.model_copy(
        update={
            "description": _clean_text(extraction.description),
            "tags": _normalize_string_list(extraction.tags, limit=MAX_TAGS),
            "anchors": _normalize_string_list(extraction.anchors, limit=MAX_ANCHORS),
            "open_when": _normalize_string_list(extraction.open_when, limit=MAX_OPEN_WHEN),
            "skip_when": _normalize_string_list(extraction.skip_when, limit=MAX_SKIP_WHEN),
            "warnings": _normalize_string_list(extraction.warnings, limit=MAX_WARNINGS),
        }
    )

    if len(normalized.description) > MAX_DESCRIPTION_LEN:
        raise UnsafeMedicalWikiExtractionError("wiki description이 너무 깁니다.")

    forbidden_tokens = _internal_forbidden_tokens(source)
    fields: list[tuple[str, str]] = [
        ("description", normalized.description),
    ]
    for list_name in ("tags", "anchors", "open_when", "skip_when", "warnings"):
        fields.extend((list_name, item) for item in getattr(normalized, list_name))

    for field_name, value in fields:
        if len(value) > MAX_ITEM_LEN and field_name not in {"description"}:
            raise UnsafeMedicalWikiExtractionError(f"{field_name} 항목이 너무 깁니다.")
        _assert_no_forbidden_text(value, field_name=field_name, forbidden_tokens=forbidden_tokens)

    if not normalized.description:
        normalized = normalized.model_copy(
            update={"description": "이 의료 문서 내용을 확인해야 하는 질문에서 열어볼 수 있는 문서입니다."}
        )
    if not normalized.tags:
        normalized = normalized.model_copy(update={"tags": ["의료 문서"]})
    if not normalized.anchors:
        normalized = normalized.model_copy(update={"anchors": normalized.tags})
    if not normalized.open_when:
        normalized = normalized.model_copy(update={"open_when": ["의료 문서 내용 확인이 필요한 질문"]})

    return normalized


def _clean_text(value: str) -> str:
    return " ".join((value or "").strip().split())


def _normalize_string_list(values: list[str], *, limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_text(str(value))
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
        if len(result) >= limit:
            break
    return result


def _internal_forbidden_tokens(source: MedicalSource) -> set[str]:
    tokens = {
        source.source_id,
        source.source_ref.drive_file_id,
        source.source_ref.drive_folder_id,
        source.source_ref.original_filename,
    }
    filename = source.source_ref.original_filename or ""
    if filename:
        tokens.add(PurePath(filename).name)
        tokens.add(PurePath(filename).stem)
    return {item.casefold() for item in tokens if item and len(item.strip()) >= 3}


def _assert_no_forbidden_text(
    value: str,
    *,
    field_name: str,
    forbidden_tokens: set[str],
) -> None:
    lowered = value.casefold()
    if any(token in lowered for token in forbidden_tokens):
        raise UnsafeMedicalWikiExtractionError(f"{field_name}에 내부 식별자 또는 원본 파일명이 포함되어 있습니다.")
    if FILE_EXTENSION_RE.search(value):
        raise UnsafeMedicalWikiExtractionError(f"{field_name}에 파일 확장자가 포함되어 있습니다.")
    if KOREAN_RRN_RE.search(value) or PHONE_NUMBER_RE.search(value) or EMAIL_RE.search(value):
        raise UnsafeMedicalWikiExtractionError(f"{field_name}에 개인정보 패턴이 포함되어 있습니다.")
    if MEDICAL_VALUE_RE.search(value) or LONG_NUMERIC_RESULT_RE.search(value):
        raise UnsafeMedicalWikiExtractionError(f"{field_name}에 검사 수치 또는 처방 상세로 보이는 값이 포함되어 있습니다.")


@dataclass
class MedicalWikiService:
    repository: MedicalRepository
    drive_gateway: DriveGateway | None = None
    extractor: MedicalWikiExtractor | None = None
    settings: Settings | None = None

    def rebuild_wiki_page(self, *, patient_id: str, source_id: str) -> MedicalWikiPage:
        source = self.repository.get_medical_source(patient_id, source_id)
        if source is None:
            raise ValueError(f"source not found: {patient_id}/{source_id}")
        if source.source_status in {"DELETED", "INACTIVE", "UNSUPPORTED"}:
            raise MedicalWikiExtractionError(f"source is not rebuildable: {source.source_status}")

        if self.settings is None or self.settings.medical_wiki_extraction_mode == "metadata":
            page = build_metadata_source_summary_page(source)
        else:
            if self.drive_gateway is None:
                raise MedicalWikiExtractionError("Drive gateway is required for Gemini Medical Wiki extraction.")
            if self.extractor is None:
                raise MedicalWikiExtractionError("Gemini Medical Wiki extractor is not configured.")
            content = self.drive_gateway.download_file_bytes(source.source_ref.drive_file_id)
            extraction = self.extractor.extract(
                WikiExtractionDocument(
                    content=content,
                    mime_type=source.source_ref.mime_type or "application/octet-stream",
                    source_id=source.source_id,
                )
            )
            safe_extraction = validate_safe_wiki_extraction(
                source=source,
                extraction=extraction,
            )
            page = build_source_summary_page_from_extraction(
                source,
                safe_extraction,
                generated_by=self.settings.gemini_wiki_model
                or self.settings.gemini_model
                or "gemini-source-summary",
            )

        self.repository.upsert_wiki_page(page)
        self.repository.append_wiki_log(
            patient_id,
            {
                "event_type": "page_rebuilt",
                "title": "source summary page rebuilt",
                "source_id": source_id,
                "page_ids": [page.page_id],
                "index_updated": False,
                "status": "success",
                "message": "source_summary page rebuilt",
                "created_at": now_kst_iso(),
            },
        )
        return page

    def compile_wiki_index(self, *, patient_id: str) -> MedicalWikiIndex:
        active_source_ids = self._ready_source_ids(patient_id)
        pages = [
            page
            for page in self.repository.list_wiki_pages(patient_id)
            if not page.quality.needs_review
            and page.source_id in active_source_ids
        ]
        entries = [
            MedicalWikiIndexPage(
                page_id=page.page_id,
                source_id=page.source_id,
                page_type=page.page_type,
                category=page.frontmatter.category,
                date=page.frontmatter.date,
                date_source=page.frontmatter.date_source,
                date_confidence=page.frontmatter.date_confidence,
                description=page.description,
                tags=page.frontmatter.tags,
                anchors=page.navigation.anchors,
                open_when=page.navigation.open_when,
                skip_when=page.navigation.skip_when,
                confidence=page.quality.confidence,
                needs_review=page.quality.needs_review,
                page_version=page.page_version,
            )
            for page in sorted(pages, key=lambda item: (item.frontmatter.date, item.page_id))
        ]
        index = MedicalWikiIndex(
            patient_id=patient_id,
            description="현재 환자의 medical_wiki_pages를 요약한 문서 catalog입니다.",
            pages=entries,
            groupings=self._build_groupings(entries),
            updated_at=now_kst_iso(),
        )
        self.repository.upsert_wiki_index_with_log(
            index=index,
            log_payload={
                "event_type": "index_compiled",
                "title": "medical wiki index compiled",
                "page_ids": [entry.page_id for entry in entries],
                "index_updated": True,
                "status": "success",
                "message": "medical_wiki_index compiled",
                "created_at": now_kst_iso(),
            },
        )
        return index

    def _ready_source_ids(self, patient_id: str) -> set[str]:
        sources = {
            source.source_id: source
            for source in self.repository.list_medical_sources(patient_id)
        }
        runtimes = {
            runtime.source_id: runtime
            for runtime in self.repository.list_source_runtimes(patient_id)
        }
        ready_source_ids: set[str] = set()
        for source_id, source in sources.items():
            runtime = runtimes.get(source_id)
            if (
                source.source_status == "ACTIVE"
                and runtime is not None
                and runtime.sync.status == "READY"
                and runtime.wiki_sync.status == "READY"
            ):
                ready_source_ids.add(source_id)
        return ready_source_ids

    def _source_is_active(self, *, patient_id: str, source_id: str) -> bool:
        source = self.repository.get_medical_source(patient_id, source_id)
        runtime = self.repository.get_source_runtime(patient_id, source_id)
        return (
            source is not None
            and source.source_status == "ACTIVE"
            and runtime is not None
            and runtime.sync.status == "READY"
            and runtime.wiki_sync.status == "READY"
        )

    def _build_groupings(self, pages: list[MedicalWikiIndexPage]) -> dict[str, dict[str, list[str]]]:
        by_category: dict[str, list[str]] = {}
        by_tag: dict[str, list[str]] = {}
        by_date: dict[str, list[str]] = {}
        for page in pages:
            by_category.setdefault(page.category or "unknown", []).append(page.page_id)
            for tag in page.tags:
                by_tag.setdefault(tag, []).append(page.page_id)
            if len(page.date) >= 7:
                by_date.setdefault(page.date[:7], []).append(page.page_id)
        return {
            "by_category": by_category,
            "by_tag": by_tag,
            "by_date": by_date,
        }
