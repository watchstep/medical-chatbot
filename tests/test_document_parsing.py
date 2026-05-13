from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from pypdf import PdfWriter
except ImportError:  # pragma: no cover - optional local test dependency
    PdfWriter = None  # type: ignore[assignment]

from app.config import Settings
from app.prompts.document_parsing import (
    build_document_parsing_prompt,
    build_document_parsing_system_instruction,
)
from app.services.document_parsing import (
    DocumentParsingError,
    DocumentParsingGateway,
    DocumentParsingService,
    PNG_MIME_TYPE,
    PageParsingPayload,
    ParsingDocument,
    build_document_parsing_result,
    concat_page_markdowns,
    default_parsed_markdown_path,
    extract_drive_file_id,
    render_page_document_to_image,
    render_page_markdown,
    split_pdf_document_pages,
    validate_merged_page_markers,
    validate_page_body_markdown,
    validate_split_page_documents,
    write_parsed_markdown,
)
from app.tools.parse_drive_document import parse_drive_document


class FakeDocumentParsingGateway(DocumentParsingGateway):
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.markdown_by_page = {
            1: (
                "# 검사 결과\n\n"
                "| 검사명 | 결과 | 단위 | 참고치 |\n"
                "|---|---:|---|---|\n"
                "| Hemoglobin | 12.0 | g/dL | 13.0-17.0 |"
            ),
            2: (
                "[REPEATED_HEADER: same as previous page]\n\n"
                "| 검사명 | 결과 | 단위 | 참고치 |\n"
                "|---|---:|---|---|\n"
                "| WBC | 5.1 | 10^3/uL | 4.0-10.0 |"
            ),
        }

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
    ) -> dict[str, str]:
        self.calls.append(
            {
                "model": model,
                "document": document,
                "system_instruction": system_instruction,
                "prompt": prompt,
                "max_output_tokens": max_output_tokens,
                "temperature": temperature,
                "top_k": top_k,
                "thinking_level": thinking_level,
            }
        )
        page_number_text = document.name.rsplit("=", 1)[1].removesuffix(".png")
        page_number = int(page_number_text)
        return {"markdown": self.markdown_by_page[page_number]}


class DocumentParsingServiceTest(unittest.TestCase):
    def _build_test_pdf(self, *, page_count: int) -> bytes:
        if PdfWriter is None:
            self.skipTest("pypdf is not installed in this test environment.")
        writer = PdfWriter()
        for _ in range(page_count):
            writer.add_blank_page(width=72, height=72)
        with tempfile.NamedTemporaryFile() as tmp:
            writer.write(tmp)
            tmp.seek(0)
            return tmp.read()

    def test_prompt_requires_markdown_transcription_rules(self) -> None:
        system_instruction = build_document_parsing_system_instruction()
        prompt = build_document_parsing_prompt()

        self.assertIn("단일 페이지 이미지", system_instruction)
        self.assertIn("주민등록번호, 주소, 전화번호만", system_instruction)
        self.assertIn("환자 이름, 환자명, 나이, 성별, 병원명", system_instruction)
        self.assertIn("원문 그대로 유지", system_instruction)
        self.assertIn("[unreadable]", system_instruction)
        self.assertIn("PAGE_START", system_instruction)
        self.assertIn("오직 JSON 데이터만 출력", prompt)

    def test_parse_document_uses_parsing_model_and_returns_quality_counts(self) -> None:
        settings = Settings(
            google_service_account_path="credentials/google-service-account.json",
            gemini_model="gemini-default",
            gemini_parsing_model="gemini-parsing",
            gemini_parsing_max_output_tokens=1234,
            gemini_parsing_temperature=0.2,
            gemini_parsing_top_k=3,
            gemini_parsing_thinking_level="high",
            gemini_thinking_level="low",
        )
        gateway = FakeDocumentParsingGateway()
        service = DocumentParsingService(settings=settings, gateway=gateway)

        document = ParsingDocument(
            name="result.pdf",
            content=b"%PDF fake bytes",
            source_id="drive-file-1",
        )
        page_documents = [
            (1, ParsingDocument(name="result.pdf#page=1", content=b"%PDF page 1")),
            (2, ParsingDocument(name="result.pdf#page=2", content=b"%PDF page 2")),
        ]
        with patch(
            "app.services.document_parsing.split_pdf_document_pages",
            return_value=page_documents,
        ), patch(
            "app.services.document_parsing.render_page_document_to_image",
            side_effect=lambda doc, page_number: ParsingDocument(
                name=f"{doc.name}.png",
                content=b"\x89PNG\r\n\x1a\nfake image",
                mime_type=PNG_MIME_TYPE,
                source_id=doc.source_id,
            ),
        ):
            result = service.parse_document(document=document)

        self.assertEqual(result.page_marker_count, 2)
        self.assertIn("<!-- PAGE_START: 0001 -->\n# 검사 결과", result.markdown)
        self.assertIn("10.0 |\n<!-- PAGE_END: 0002 -->", result.markdown)
        self.assertEqual(result.table_row_count, 6)
        self.assertGreater(result.character_count, 20)
        self.assertEqual(len(gateway.calls), 2)
        self.assertEqual(gateway.calls[0]["model"], "gemini-parsing")
        self.assertEqual(gateway.calls[0]["document"].mime_type, PNG_MIME_TYPE)
        self.assertEqual(gateway.calls[1]["document"].mime_type, PNG_MIME_TYPE)
        self.assertEqual(gateway.calls[0]["max_output_tokens"], 1234)
        self.assertEqual(gateway.calls[0]["temperature"], 0.2)
        self.assertEqual(gateway.calls[0]["top_k"], 3)
        self.assertEqual(gateway.calls[0]["thinking_level"], "high")
        self.assertGreaterEqual(result.elapsed_seconds, 0.0)
        self.assertEqual(result.generation_config.model, "gemini-parsing")
        self.assertEqual(result.generation_config.max_output_tokens, 1234)
        self.assertEqual(result.generation_config.temperature, 0.2)
        self.assertEqual(result.generation_config.top_k, 3)
        self.assertEqual(result.generation_config.thinking_level, "high")

    def test_parse_document_runs_page_parsing_in_parallel_and_merges_by_page_number(self) -> None:
        class SlowOutOfOrderGateway(DocumentParsingGateway):
            def __init__(self) -> None:
                self.lock = threading.Lock()
                self.active_calls = 0
                self.max_active_calls = 0

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
            ) -> dict[str, str]:
                page_number_text = document.name.rsplit("=", 1)[1].removesuffix(".png")
                page_number = int(page_number_text)
                with self.lock:
                    self.active_calls += 1
                    self.max_active_calls = max(self.max_active_calls, self.active_calls)
                try:
                    time.sleep(0.04 if page_number == 1 else 0.01)
                    return {"markdown": f"# Page {page_number}"}
                finally:
                    with self.lock:
                        self.active_calls -= 1

        settings = Settings(
            google_service_account_path="credentials/google-service-account.json",
            gemini_model="gemini-default",
        )
        gateway = SlowOutOfOrderGateway()
        service = DocumentParsingService(settings=settings, gateway=gateway)
        document = ParsingDocument(
            name="result.pdf",
            content=b"%PDF fake bytes",
            source_id="drive-file-1",
        )
        page_documents = [
            (1, ParsingDocument(name="result.pdf#page=1", content=b"%PDF page 1")),
            (2, ParsingDocument(name="result.pdf#page=2", content=b"%PDF page 2")),
            (3, ParsingDocument(name="result.pdf#page=3", content=b"%PDF page 3")),
        ]
        with patch(
            "app.services.document_parsing.split_pdf_document_pages",
            return_value=page_documents,
        ), patch(
            "app.services.document_parsing.render_page_document_to_image",
            side_effect=lambda doc, page_number: ParsingDocument(
                name=f"{doc.name}.png",
                content=b"\x89PNG\r\n\x1a\nfake image",
                mime_type=PNG_MIME_TYPE,
                source_id=doc.source_id,
            ),
        ):
            result = service.parse_document(document=document)

        self.assertGreaterEqual(gateway.max_active_calls, 2)
        self.assertLess(
            result.markdown.index("<!-- PAGE_START: 0001 -->"),
            result.markdown.index("<!-- PAGE_START: 0002 -->"),
        )
        self.assertLess(
            result.markdown.index("<!-- PAGE_START: 0002 -->"),
            result.markdown.index("<!-- PAGE_START: 0003 -->"),
        )
        self.assertEqual(result.page_marker_count, 3)

    def test_settings_defaults_keep_qa_and_parsing_generation_options_separate(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings(_env_file=None)

        self.assertEqual(settings.gemini_thinking_level, "low")
        self.assertEqual(settings.gemini_parsing_thinking_level, "minimal")
        self.assertEqual(settings.gemini_temperature, 0.1)
        self.assertEqual(settings.gemini_parsing_temperature, 0.0)
        self.assertEqual(settings.gemini_parsing_max_output_tokens, 32768)
        self.assertIsNone(settings.gemini_parsing_top_k)

    def test_build_document_parsing_result_rejects_empty_markdown(self) -> None:
        with self.assertRaises(DocumentParsingError):
            build_document_parsing_result("  \n")

    def test_validate_page_body_markdown_rejects_page_boundary_marker(self) -> None:
        with self.assertRaises(DocumentParsingError):
            validate_page_body_markdown(
                "<!-- PAGE_START: 1 -->\n# Parsed\n<!-- PAGE_END: 1 -->",
                page_number=1,
            )

    def test_validate_page_body_markdown_redacts_only_number_phi(self) -> None:
        markdown = validate_page_body_markdown(
            "\n".join(
                [
                    "환자명: 홍길동",
                    "나이: 45세",
                    "성별: 남",
                    "병원명: 서울대학교병원",
                    "주민등록번호: 900101-1234567",
                    "전화번호: 010-1234-5678",
                ]
            ),
            page_number=1,
        )

        self.assertIn("환자명: 홍길동", markdown)
        self.assertIn("나이: 45세", markdown)
        self.assertIn("성별: 남", markdown)
        self.assertIn("병원명: 서울대학교병원", markdown)
        self.assertNotIn("900101-1234567", markdown)
        self.assertNotIn("010-1234-5678", markdown)
        self.assertEqual(markdown.count("***"), 2)

    def test_split_pdf_document_pages_returns_sequential_nonempty_pdf_pages(self) -> None:
        page_documents = split_pdf_document_pages(
            ParsingDocument(
                name="multi.pdf",
                content=self._build_test_pdf(page_count=3),
                source_id="drive-file-1",
            )
        )

        self.assertEqual([page_number for page_number, _ in page_documents], [1, 2, 3])
        for page_number, page_document in page_documents:
            self.assertEqual(page_document.name, f"multi.pdf#page={page_number}")
            self.assertTrue(page_document.content.startswith(b"%PDF"))
            self.assertGreater(len(page_document.content), 20)

    def test_render_page_document_to_image_returns_png_page_image(self) -> None:
        page_document = split_pdf_document_pages(
            ParsingDocument(
                name="single.pdf",
                content=self._build_test_pdf(page_count=1),
                source_id="drive-file-1",
            )
        )[0][1]

        image_document = render_page_document_to_image(page_document, page_number=1)

        self.assertEqual(image_document.name, "single.pdf#page=1.png")
        self.assertEqual(image_document.mime_type, PNG_MIME_TYPE)
        self.assertTrue(image_document.content.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(image_document.content), 20)

    def test_validate_split_page_documents_rejects_non_sequential_pages(self) -> None:
        with self.assertRaises(DocumentParsingError):
            validate_split_page_documents(
                [
                    (1, ParsingDocument(name="page-1.pdf", content=b"%PDF page 1")),
                    (3, ParsingDocument(name="page-3.pdf", content=b"%PDF page 3")),
                ]
            )

    def test_validate_split_page_documents_rejects_invalid_pdf_page_bytes(self) -> None:
        with self.assertRaises(DocumentParsingError):
            validate_split_page_documents(
                [
                    (1, ParsingDocument(name="page-1.pdf", content=b"not pdf")),
                ]
            )

    def test_add_page_markers_wraps_valid_body_markdown(self) -> None:
        self.assertEqual(
            render_page_markdown(PageParsingPayload("# Parsed"), page_number=3),
            "<!-- PAGE_START: 0003 -->\n# Parsed\n<!-- PAGE_END: 0003 -->",
        )

    def test_concat_page_markdowns_sorts_code_marked_pages_by_page_number(self) -> None:
        markdown = concat_page_markdowns(
            [
                (2, "<!-- PAGE_START: 2 -->\n# Two\n<!-- PAGE_END: 2 -->"),
                (1, "<!-- PAGE_START: 1 -->\n# One\n<!-- PAGE_END: 1 -->"),
            ],
            expected_page_count=2,
        )

        self.assertEqual(
            markdown,
            "<!-- PAGE_START: 1 -->\n# One\n<!-- PAGE_END: 1 -->\n\n"
            "<!-- PAGE_START: 2 -->\n# Two\n<!-- PAGE_END: 2 -->",
        )

    def test_validate_merged_page_markers_rejects_missing_marker(self) -> None:
        with self.assertRaises(DocumentParsingError):
            validate_merged_page_markers(
                "<!-- PAGE_START: 1 -->\n# One\n<!-- PAGE_END: 1 -->\n\n"
                "<!-- PAGE_START: 2 -->\n# Two",
                expected_page_count=2,
            )

    def test_validate_merged_page_markers_rejects_duplicate_marker(self) -> None:
        with self.assertRaises(DocumentParsingError):
            validate_merged_page_markers(
                "<!-- PAGE_START: 1 -->\n# One\n<!-- PAGE_END: 1 -->\n\n"
                "<!-- PAGE_START: 1 -->\n# Duplicate\n<!-- PAGE_END: 1 -->",
                expected_page_count=2,
            )

    def test_validate_merged_page_markers_rejects_out_of_order_marker(self) -> None:
        with self.assertRaises(DocumentParsingError):
            validate_merged_page_markers(
                "<!-- PAGE_START: 2 -->\n# Two\n<!-- PAGE_END: 2 -->\n\n"
                "<!-- PAGE_START: 1 -->\n# One\n<!-- PAGE_END: 1 -->",
                expected_page_count=2,
            )

    def test_extract_drive_file_id_accepts_id_and_url(self) -> None:
        self.assertEqual(extract_drive_file_id("abcDEF_1234567890"), "abcDEF_1234567890")
        self.assertEqual(
            extract_drive_file_id("https://drive.google.com/file/d/file_1234567890/view"),
            "file_1234567890",
        )

    def test_default_path_and_write_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = default_parsed_markdown_path(
                source_id="drive/file id",
                base_dir=tmpdir,
            )
            self.assertEqual(path.name, "parsed.md")
            write_parsed_markdown(path, "# Parsed")

            self.assertEqual(Path(path).read_text(encoding="utf-8"), "# Parsed\n")

    def test_parse_drive_document_downloads_drive_pdf_and_writes_markdown(self) -> None:
        class FakeParsingService:
            def parse_document(self, *, document, model, max_output_tokens):
                self.document = document
                self.model = model
                self.max_output_tokens = max_output_tokens
                return build_document_parsing_result(
                    "<!-- PAGE_START: 1 -->\n# Parsed\n<!-- PAGE_END: 1 -->"
                )

        fake_parsing_service = FakeParsingService()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch(
                "app.tools.parse_drive_document.build_drive_client",
                return_value=object(),
            ), patch(
                "app.tools.parse_drive_document.download_drive_pdf",
                return_value=ParsingDocument(
                    name="result.pdf",
                    content=b"%PDF fake bytes",
                    source_id="file_1234567890",
                ),
            ), patch(
                "app.tools.parse_drive_document.build_default_document_parsing_service",
                return_value=fake_parsing_service,
            ):
                markdown_path, metadata_path = parse_drive_document(
                    file_id_or_url="https://drive.google.com/file/d/file_1234567890/view",
                    settings=Settings(),
                    output_base_dir=tmpdir,
                    markdown_filename="parsed.md",
                    model="gemini-test",
                    max_output_tokens=999,
                )
            written_markdown = markdown_path.read_text(encoding="utf-8")
            written_metadata = metadata_path.read_text(encoding="utf-8")

        self.assertEqual(markdown_path.name, "parsed.md")
        self.assertEqual(metadata_path.name, "parsed_metadata.json")
        self.assertEqual(
            written_markdown,
            "<!-- PAGE_START: 1 -->\n# Parsed\n<!-- PAGE_END: 1 -->\n",
        )
        self.assertIn('"elapsed_seconds"', written_metadata)
        self.assertIn('"generation_config"', written_metadata)
        self.assertEqual(fake_parsing_service.document.name, "result.pdf")
        self.assertEqual(fake_parsing_service.model, "gemini-test")
        self.assertEqual(fake_parsing_service.max_output_tokens, 999)


if __name__ == "__main__":
    unittest.main()
