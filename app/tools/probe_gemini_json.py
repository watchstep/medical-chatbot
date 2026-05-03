from __future__ import annotations

import argparse
import logging
import sys
from typing import Sequence

from app.config import Settings, get_settings
from app.schemas import DocumentRegistryEntry, PatientDocumentRegistryContext, PatientIndexEntry
from app.services.cache import PatientDataCacheService
from app.services.chatbot import ChatbotService, dedupe_preserve_order
from app.services.drive import DriveLookupError, build_default_drive_service
from app.services.gemini_qa import (
    GeminiGenerateResult,
    GeminiGateway,
    GeminiQaError,
    GeminiQaService,
    GoogleGeminiGateway,
)
from app.services.kakao_callback import KakaoCallbackService
from app.sessions import InMemorySessionStore


class CapturingGeminiGateway(GeminiGateway):
    def __init__(self, delegate: GeminiGateway):
        self.delegate = delegate
        self.last_raw_text = ""
        self.last_grounding_source_ids: list[str] = []

    def generate_answer(
        self,
        *,
        model: str,
        system_instruction: str,
        prompt: str,
        file_search_store_name: str,
        response_json_schema: dict[str, object],
        temperature: float,
        max_output_tokens: int,
        thinking_level: str,
        file_search_top_k: int,
        log_retrieval: bool,
    ) -> GeminiGenerateResult:
        result = self.delegate.generate_answer(
            model=model,
            system_instruction=system_instruction,
            prompt=prompt,
            file_search_store_name=file_search_store_name,
            response_json_schema=response_json_schema,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            thinking_level=thinking_level,
            file_search_top_k=file_search_top_k,
            log_retrieval=log_retrieval,
        )
        self.last_raw_text = result.text
        self.last_grounding_source_ids = result.grounding_source_ids
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a live Gemini QA call and validate the model JSON contract.",
    )
    parser.add_argument(
        "--patient-id",
        required=True,
        help="patient_index.json에 있는 patient_id입니다. 예: P0001",
    )
    parser.add_argument(
        "--question",
        help="Gemini에 보낼 질문입니다. 생략하면 터미널에서 직접 입력받습니다.",
    )
    parser.add_argument(
        "--show-raw",
        action="store_true",
        help="Gemini 원본 JSON을 출력합니다. 개인정보가 포함될 수 있습니다.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Drive/Gemini 내부 로그를 함께 출력합니다.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(verbose=args.verbose)
    question = args.question or input("질문을 입력하세요: ").strip()
    if not question:
        print("질문이 비어 있습니다.", file=sys.stderr)
        return 1

    settings = get_settings()
    if not settings.gemini_api_key:
        print("GEMINI_API_KEY가 설정되어 있지 않습니다.", file=sys.stderr)
        return 1

    try:
        result = run_probe(
            settings=settings,
            patient_id=args.patient_id,
            question=question,
            show_raw=args.show_raw,
        )
    except (DriveLookupError, GeminiQaError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(result)
    return 0


def run_probe(
    *,
    settings: Settings,
    patient_id: str,
    question: str,
    show_raw: bool,
) -> str:
    drive_service = build_default_drive_service(settings)
    cache_service = PatientDataCacheService(settings=settings, drive_service=drive_service)
    patient = _find_patient_by_id(cache_service=cache_service, patient_id=patient_id)
    context = cache_service.get_patient_document_context(patient=patient)
    _validate_context(context)

    gateway = CapturingGeminiGateway(GoogleGeminiGateway(settings.gemini_api_key or ""))
    qa_service = GeminiQaService(settings=settings, gateway=gateway)
    chatbot_service = ChatbotService(
        patient_data_cache_service=cache_service,
        gemini_qa_service=qa_service,
        kakao_callback_service=KakaoCallbackService(),
        session_store=InMemorySessionStore(),
    )

    try:
        qa_answer = qa_service.answer_question(question=question, context=context)
    except GeminiQaError as exc:
        lines = [
            "MODEL STATUS",
            "generation_failed",
            "",
            "SCHEMA VALIDATION",
            "failed",
            "",
            "ERROR",
            str(exc),
        ]
        if show_raw and gateway.last_raw_text:
            lines.extend(["", "RAW MODEL JSON", gateway.last_raw_text])
        raise GeminiQaError("\n".join(lines)) from exc

    model_answer = qa_answer.model_answer
    source_report = _build_source_report(
        chatbot_service=chatbot_service,
        context=context,
        used_source_ids=model_answer.used_source_ids,
        grounding_source_ids=qa_answer.grounding_source_ids,
    )
    rendered_text = chatbot_service._validate_and_render_model_answer(
        model_answer=model_answer,
        grounding_source_ids=qa_answer.grounding_source_ids,
        context=context,
    )

    lines = [
        "MODEL STATUS",
        model_answer.status,
        "",
        "SCHEMA VALIDATION",
        "passed",
        "",
        "SOURCE VALIDATION",
        source_report,
        "",
        "USED SOURCE IDS",
        _format_list(dedupe_preserve_order(model_answer.used_source_ids)),
        "",
        "GROUNDING SOURCE IDS",
        _format_list(dedupe_preserve_order(qa_answer.grounding_source_ids)),
        "",
        "KAKAO RENDERED TEXT",
        rendered_text,
    ]
    if show_raw:
        lines.extend(["", "RAW MODEL JSON", gateway.last_raw_text])
    return "\n".join(lines)


def configure_logging(*, verbose: bool) -> None:
    if verbose:
        logging.basicConfig(level=logging.INFO)
        return
    logging.getLogger("app.services.drive").setLevel(logging.CRITICAL)
    logging.getLogger("app.services.gemini_qa").setLevel(logging.CRITICAL)


def _find_patient_by_id(
    *,
    cache_service: PatientDataCacheService,
    patient_id: str,
) -> PatientIndexEntry:
    patient_index = cache_service._get_patient_index()
    patient = next(
        (item for item in patient_index.patients if item.patient_id == patient_id),
        None,
    )
    if patient is None:
        raise ValueError(f"patient_index.json에서 patient_id를 찾지 못했습니다: {patient_id}")
    return patient


def _validate_context(context: PatientDocumentRegistryContext) -> None:
    ready_documents = [item for item in context.documents if item.sync_status == "READY"]
    if not ready_documents:
        raise ValueError("해당 환자의 READY 문서가 document_registry.json에 없습니다.")
    if not context.file_search_store_name:
        raise ValueError("해당 환자의 READY File Search Store 이름이 없습니다.")


def _build_source_report(
    *,
    chatbot_service: ChatbotService,
    context: PatientDocumentRegistryContext,
    used_source_ids: list[str],
    grounding_source_ids: list[str],
) -> str:
    ready_documents = [item for item in context.documents if item.sync_status == "READY"]
    ready_filenames = {item.filename for item in ready_documents}
    model_sources = dedupe_preserve_order(used_source_ids)
    if not model_sources:
        return "failed: model used_source_ids is empty"

    unknown_sources = [source_id for source_id in model_sources if source_id not in ready_filenames]
    if unknown_sources:
        return "failed: model used non-READY or unknown source ids: " + ", ".join(
            unknown_sources
        )

    normalized_grounding_sources = dedupe_preserve_order(
        [
            normalized
            for source_id in grounding_source_ids
            if (
                normalized := chatbot_service._normalize_grounding_source_id(
                    source_id=source_id,
                    ready_documents=ready_documents,
                )
            )
        ]
    )
    if not normalized_grounding_sources:
        return "failed: response grounding source ids are empty or not matched to READY documents"

    missing_in_grounding = [
        source_id
        for source_id in model_sources
        if source_id not in set(normalized_grounding_sources)
    ]
    if missing_in_grounding:
        return "failed: model source ids not supported by grounding: " + ", ".join(
            missing_in_grounding
        )

    ready_by_filename = {item.filename: item for item in ready_documents}
    final_sources = [ready_by_filename[source_id] for source_id in model_sources]
    return "passed: " + _format_sources(final_sources)


def _format_sources(sources: list[DocumentRegistryEntry]) -> str:
    if not sources:
        return "(none)"
    return ", ".join(source.filename for source in sources)


def _format_list(items: list[str]) -> str:
    if not items:
        return "(empty)"
    return "\n".join(f"- {item}" for item in items)


if __name__ == "__main__":
    raise SystemExit(main())
