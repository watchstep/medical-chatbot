from __future__ import annotations

import sys
import types
import unittest
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

if "google" not in sys.modules:
    google_module = types.ModuleType("google")
    genai_module = types.ModuleType("google.genai")
    genai_types_module = types.ModuleType("google.genai.types")
    genai_errors_module = types.ModuleType("google.genai.errors")

    class GenerateContentConfig:  # noqa: D401 - minimal SDK config stub
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def model_dump(self, *, by_alias: bool = False, exclude_none: bool = False) -> dict[str, Any]:
            items = {
                key: value
                for key, value in self.kwargs.items()
                if not exclude_none or value is not None
            }
            if not by_alias:
                return items
            aliases = {
                "system_instruction": "systemInstruction",
                "response_mime_type": "responseMimeType",
                "response_json_schema": "responseJsonSchema",
                "temperature": "temperature",
                "max_output_tokens": "maxOutputTokens",
                "thinking_config": "thinkingConfig",
            }
            return {aliases.get(key, key): value for key, value in items.items()}

    class ThinkingConfig:  # noqa: D401 - minimal SDK config stub
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class Part:  # noqa: D401 - minimal SDK part stub
        @classmethod
        def from_bytes(cls, *, data: bytes, mime_type: str) -> Any:
            return types.SimpleNamespace(data=data, mime_type=mime_type)

    genai_module.Client = object
    genai_types_module.GenerateContentConfig = GenerateContentConfig
    genai_types_module.ThinkingConfig = ThinkingConfig
    genai_types_module.Part = Part
    genai_module.types = genai_types_module
    genai_module.errors = genai_errors_module
    google_module.genai = genai_module
    sys.modules["google"] = google_module
    sys.modules["google.genai"] = genai_module
    sys.modules["google.genai.types"] = genai_types_module
    sys.modules["google.genai.errors"] = genai_errors_module

    try:
        __import__("app.services.drive")
    except Exception:
        drive_module = types.ModuleType("app.services.drive")

        class DriveGateway:  # noqa: D401 - minimal test stub
            pass

        class DriveLookupError(Exception):
            pass

        class DriveLookupService:  # noqa: D401 - minimal test stub
            def __init__(self, *, settings: Any, gateway: Any) -> None:
                self.settings = settings
                self.gateway = gateway

        def build_default_drive_service(settings: Any) -> DriveLookupService:
            return DriveLookupService(settings=settings, gateway=DriveGateway())

        drive_module.DriveGateway = DriveGateway
        drive_module.DriveLookupError = DriveLookupError
        drive_module.DriveLookupService = DriveLookupService
        drive_module.build_default_drive_service = build_default_drive_service
        sys.modules["app.services.drive"] = drive_module

from app.config import Settings
from app.repositories import InMemoryMedicalRepository
from app.schemas import (
    FinalQaAnswer,
    GeminiFileRuntime,
    MedicalSource,
    MedicalSourceRuntime,
    MedicalWikiFrontmatter,
    MedicalWikiPage,
    RuntimeSyncState,
    SourceRef,
)
from app.services.gemini_files_qa import (
    GeminiFileNotReadyError,
    GeminiFileUploadLockError,
    GeminiFilesGateway,
    GeminiFilesQaError,
    GeminiFilesQaService,
    PreparedGeminiFile,
)


@dataclass
class FakeGeminiFile:
    name: str
    state: str = "ACTIVE"
    uri: str = "gemini://file"
    mime_type: str = "application/pdf"
    expiration_time: str = ""


class FakeDriveGateway:
    def __init__(self) -> None:
        self.download_count = 0

    def list_files_in_folder(self, folder_id: str) -> list[dict]:
        return []

    def download_file_bytes(self, file_id: str) -> bytes:
        self.download_count += 1
        return b"%PDF fake"

    def get_file(self, file_id: str) -> dict:
        return {}

    def get_start_page_token(self) -> str:
        return "token"

    def list_changes(self, page_token: str, *, page_size: int) -> dict:
        return {"changes": [], "newStartPageToken": "token2"}


class FakeGeminiFilesGateway(GeminiFilesGateway):
    def __init__(self) -> None:
        self.upload_results: list[Any] = []
        self.get_file_results: list[Any] = []
        self.generate_calls: list[list[Any]] = []
        self.generate_result = ""
        self.upload_count = 0
        self.get_count = 0

    def upload_file(self, *, display_name: str, content: bytes, mime_type: str) -> Any:
        self.upload_count += 1
        if not self.upload_results:
            raise AssertionError("upload_file called without a configured result")
        result = self.upload_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def get_file(self, *, file_name: str) -> Any:
        self.get_count += 1
        if self.get_file_results:
            result = self.get_file_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return FakeGeminiFile(name=file_name, state="ACTIVE", expiration_time=future_expiration())

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
        self.generate_calls.append(contents)
        if self.generate_result:
            return self.generate_result
        raise AssertionError("generate_json is not used in these tests")


def future_expiration(minutes: int = 60) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def past_expiration() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()


class GeminiFilesRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            _env_file=None,
            gemini_api_key="test-key",
            medical_wiki_extraction_mode="metadata",
            file_ready_wait_seconds=1,
            file_ready_poll_interval_seconds=0.5,
            file_upload_lock_lease_seconds=30,
            file_upload_max_retry_count=3,
            file_expiration_margin_seconds=0,
        )
        self.repository = InMemoryMedicalRepository()
        self.drive_gateway = FakeDriveGateway()
        self.gemini_gateway = FakeGeminiFilesGateway()
        self.service = GeminiFilesQaService(
            settings=self.settings,
            repository=self.repository,
            drive_gateway=self.drive_gateway,
            gateway=self.gemini_gateway,
        )
        self.source = MedicalSource(
            source_id="SRC_P1_A",
            patient_id="P1",
            source_ref=SourceRef(
                drive_file_id="drive-file-1",
                drive_folder_id="folder-p1",
                original_filename="검사결과.pdf",
                mime_type="application/pdf",
                file_size_bytes=1234,
                file_hash="hash-1",
                drive_modified_at="2026-04-21T10:00:00Z",
            ),
            source_status="ACTIVE",
        )
        self.repository.upsert_medical_source(self.source)
        self.repository.upsert_source_runtime(
            MedicalSourceRuntime(
                source_id=self.source.source_id,
                patient_id=self.source.patient_id,
                sync=RuntimeSyncState(status="READY"),
                wiki_sync=RuntimeSyncState(status="READY"),
            )
        )

    def _runtime(self) -> MedicalSourceRuntime:
        runtime = self.repository.get_source_runtime("P1", "SRC_P1_A")
        assert runtime is not None
        return runtime

    def _set_gemini_file(self, **updates: Any) -> None:
        runtime = self._runtime()
        base = GeminiFileRuntime(
            file_name="files/old",
            uri="gemini://old",
            mime_type="application/pdf",
            state="ACTIVE",
            expiration_time=future_expiration(),
            source_drive_modified_at=self.source.source_ref.drive_modified_at,
            source_file_hash=self.source.source_ref.file_hash,
            source_file_size_bytes=self.source.source_ref.file_size_bytes,
        )
        runtime = runtime.model_copy(update={"gemini_file": base.model_copy(update=updates)})
        self.repository.upsert_source_runtime(runtime)

    def test_reuses_active_file_when_snapshot_and_expiration_are_valid(self) -> None:
        self._set_gemini_file()
        self.gemini_gateway.get_file_results = [
            FakeGeminiFile(name="files/old", state="ACTIVE", expiration_time=future_expiration())
        ]

        prepared = self.service.prepare_file(patient_id="P1", source_id="SRC_P1_A")

        self.assertEqual(prepared.file_name, "files/old")
        self.assertEqual(self.gemini_gateway.upload_count, 0)
        self.assertEqual(self.drive_gateway.download_count, 0)
        self.assertEqual(self.gemini_gateway.get_count, 1)
        self.assertEqual(self._runtime().sync.status, "READY")

    def test_reuse_path_writes_timing_logs(self) -> None:
        self._set_gemini_file()
        self.gemini_gateway.get_file_results = [
            FakeGeminiFile(name="files/old", state="ACTIVE", expiration_time=future_expiration())
        ]

        with self.assertLogs("app.services.gemini_files_qa", level="INFO") as logs:
            prepared = self.service.prepare_file(patient_id="P1", source_id="SRC_P1_A")

        self.assertEqual(prepared.file_name, "files/old")
        output = "\n".join(logs.output)
        self.assertIn("event=file_reuse.start", output)
        self.assertIn("event=file_reuse.done", output)
        self.assertIn("duration_ms=", output)

    def test_expired_file_is_uploaded_again(self) -> None:
        self._set_gemini_file(expiration_time=past_expiration())
        self.gemini_gateway.upload_results = [
            FakeGeminiFile(name="files/new", state="ACTIVE", expiration_time=future_expiration())
        ]

        prepared = self.service.prepare_file(patient_id="P1", source_id="SRC_P1_A")

        self.assertEqual(prepared.file_name, "files/new")
        self.assertEqual(self.gemini_gateway.upload_count, 1)
        self.assertEqual(self.drive_gateway.download_count, 1)
        self.assertEqual(self._runtime().gemini_file.file_name, "files/new")
        self.assertEqual(self._runtime().sync.retry_count, 0)

    def test_upload_path_writes_timing_logs(self) -> None:
        self.gemini_gateway.upload_results = [
            FakeGeminiFile(name="files/new", state="ACTIVE", expiration_time=future_expiration())
        ]

        with self.assertLogs("app.services.gemini_files_qa", level="INFO") as logs:
            prepared = self.service.prepare_file(patient_id="P1", source_id="SRC_P1_A")

        self.assertEqual(prepared.file_name, "files/new")
        output = "\n".join(logs.output)
        for event in [
            "file_reuse.start",
            "file_reuse.done",
            "drive_download.start",
            "drive_download.done",
            "file_upload.start",
            "file_upload.done",
        ]:
            self.assertIn(f"event={event}", output)
        self.assertIn("duration_ms=", output)

    def test_source_snapshot_mismatch_is_uploaded_again(self) -> None:
        self._set_gemini_file(source_file_hash="old-hash")
        self.gemini_gateway.upload_results = [
            FakeGeminiFile(name="files/new", state="ACTIVE", expiration_time=future_expiration())
        ]

        prepared = self.service.prepare_file(patient_id="P1", source_id="SRC_P1_A")

        self.assertEqual(prepared.file_name, "files/new")
        self.assertEqual(self.gemini_gateway.upload_count, 1)
        self.assertEqual(self._runtime().gemini_file.source_file_hash, "hash-1")

    def test_processing_upload_is_polled_until_active(self) -> None:
        self.gemini_gateway.upload_results = [
            FakeGeminiFile(name="files/new", state="PROCESSING", expiration_time=future_expiration())
        ]
        self.gemini_gateway.get_file_results = [
            FakeGeminiFile(name="files/new", state="PROCESSING", expiration_time=future_expiration()),
            FakeGeminiFile(name="files/new", state="ACTIVE", expiration_time=future_expiration()),
        ]

        prepared = self.service.prepare_file(patient_id="P1", source_id="SRC_P1_A")

        self.assertEqual(prepared.file_name, "files/new")
        self.assertEqual(self._runtime().gemini_file.state, "ACTIVE")
        self.assertEqual(self._runtime().sync.status, "READY")
        self.assertIsNone(self._runtime().sync.lock_owner)

    def test_polling_path_writes_timing_logs(self) -> None:
        self.gemini_gateway.upload_results = [
            FakeGeminiFile(name="files/new", state="PROCESSING", expiration_time=future_expiration())
        ]
        self.gemini_gateway.get_file_results = [
            FakeGeminiFile(name="files/new", state="ACTIVE", expiration_time=future_expiration()),
        ]

        with self.assertLogs("app.services.gemini_files_qa", level="INFO") as logs:
            prepared = self.service.prepare_file(patient_id="P1", source_id="SRC_P1_A")

        self.assertEqual(prepared.file_name, "files/new")
        output = "\n".join(logs.output)
        self.assertIn("event=file_polling.start", output)
        self.assertIn("event=file_polling.done", output)
        self.assertIn("duration_ms=", output)

    def test_processing_timeout_leaves_runtime_processing(self) -> None:
        self.gemini_gateway.upload_results = [
            FakeGeminiFile(name="files/new", state="PROCESSING", expiration_time=future_expiration())
        ]
        self.gemini_gateway.get_file_results = [
            FakeGeminiFile(name="files/new", state="PROCESSING", expiration_time=future_expiration())
            for _ in range(5)
        ]

        with self.assertRaises(GeminiFileNotReadyError):
            self.service.prepare_file(patient_id="P1", source_id="SRC_P1_A")

        runtime = self._runtime()
        self.assertEqual(runtime.gemini_file.state, "PROCESSING")
        self.assertEqual(runtime.sync.status, "PROCESSING")
        self.assertIsNone(runtime.sync.lock_owner)

    def test_active_upload_lock_without_file_name_blocks_duplicate_upload(self) -> None:
        runtime = self._runtime()
        runtime = runtime.model_copy(
            update={
                "sync": runtime.sync.model_copy(
                    update={
                        "status": "UPLOADING",
                        "lock_owner": "other-worker",
                        "lease_expires_at": future_expiration(),
                    }
                )
            }
        )
        self.repository.upsert_source_runtime(runtime)

        with self.assertRaises(GeminiFileUploadLockError):
            self.service.prepare_file(patient_id="P1", source_id="SRC_P1_A")

        self.assertEqual(self.gemini_gateway.upload_count, 0)
        self.assertEqual(self.drive_gateway.download_count, 0)

    def test_upload_failure_records_runtime_failure_and_releases_lock(self) -> None:
        self.gemini_gateway.upload_results = [RuntimeError("upload boom")]

        with self.assertRaises(GeminiFilesQaError):
            self.service.prepare_file(patient_id="P1", source_id="SRC_P1_A")

        runtime = self._runtime()
        self.assertEqual(runtime.sync.status, "FAILED")
        self.assertEqual(runtime.sync.retry_count, 1)
        assert runtime.sync.last_failure is not None
        self.assertEqual(runtime.sync.last_failure.code, "FILE_UPLOAD_FAILED")
        self.assertEqual(runtime.gemini_file.state, "FAILED")
        self.assertIsNone(runtime.sync.lock_owner)

    def test_retry_limit_blocks_upload(self) -> None:
        runtime = self._runtime()
        runtime = runtime.model_copy(update={"sync": runtime.sync.model_copy(update={"retry_count": 3})})
        self.repository.upsert_source_runtime(runtime)

        with self.assertRaises(GeminiFilesQaError):
            self.service.prepare_file(patient_id="P1", source_id="SRC_P1_A")

        self.assertEqual(self.gemini_gateway.upload_count, 0)
        self.assertEqual(self._runtime().sync.last_failure.code, "FILE_UPLOAD_RETRY_EXCEEDED")

    def test_answer_rejects_source_not_in_prepared_file(self) -> None:
        foreign_source = MedicalSource(
            source_id="SRC_P1_B",
            patient_id="P1",
            source_ref=SourceRef(
                drive_file_id="drive-file-2",
                drive_folder_id="folder-p1",
                original_filename="다른검사.pdf",
                mime_type="application/pdf",
            ),
            source_status="ACTIVE",
        )
        self.repository.upsert_medical_source(foreign_source)
        self.repository.upsert_source_runtime(
            MedicalSourceRuntime(
                source_id=foreign_source.source_id,
                patient_id=foreign_source.patient_id,
                sync=RuntimeSyncState(status="READY"),
                wiki_sync=RuntimeSyncState(status="READY"),
            )
        )
        self.gemini_gateway.generate_result = json.dumps(
            {
                "status": "ok",
                "kakaotalk_render": "다른 문서를 근거로 한 답변",
                "used_source_ids": [foreign_source.source_id],
            },
            ensure_ascii=False,
        )

        with self.assertRaises(GeminiFilesQaError):
            self.service.answer_question(
                patient_id="P1",
                question="검사 결과 알려줘",
                prior_context="",
                prepared_file=PreparedGeminiFile(
                    source_id=self.source.source_id,
                    file_name="files/test",
                    uri="",
                    mime_type="application/pdf",
                    file_object=FakeGeminiFile(name="files/test"),
                ),
            )

    def _upsert_wiki_page(
        self,
        *,
        category: str = "lab_result",
        date: str = "2026-04-21",
        page_count: int | None = 5,
    ) -> None:
        self.repository.upsert_wiki_page(
            MedicalWikiPage(
                page_id=f"PAGE_{self.source.source_id}",
                source_id=self.source.source_id,
                patient_id=self.source.patient_id,
                frontmatter=MedicalWikiFrontmatter(
                    page_count=page_count,
                    category=category,
                    date=date,
                ),
            )
        )

    def test_render_answer_appends_document_title_for_used_source(self) -> None:
        self._upsert_wiki_page(page_count=5)
        answer = FinalQaAnswer(
            status="ok",
            kakaotalk_render="검사 결과 답변입니다. (2쪽)",
            used_source_ids=[self.source.source_id],
        )

        rendered = self.service.render_answer(
            patient_id="P1",
            answer=answer,
            intent="OK",
            selected_source_id=self.source.source_id,
        )

        self.assertIn("📄 출처", rendered)
        self.assertIn("검사 결과 답변입니다. (2쪽)", rendered)
        self.assertIn("1. 검사 결과 (2026-04-21, 5쪽)", rendered)
        self.assertNotIn("관련 문서, 2쪽", rendered)

    def test_render_answer_keeps_model_page_markers_in_body_only(self) -> None:
        self._upsert_wiki_page(page_count=5)
        answer = FinalQaAnswer(
            status="ok",
            kakaotalk_render="첫 번째 근거입니다. (2쪽)\n두 번째 근거입니다. (2쪽, 5쪽)",
            used_source_ids=[self.source.source_id],
        )

        rendered = self.service.render_answer(
            patient_id="P1",
            answer=answer,
            intent="OK",
            selected_source_id=self.source.source_id,
        )

        self.assertIn("첫 번째 근거입니다. (2쪽)", rendered)
        self.assertIn("두 번째 근거입니다. (2쪽, 5쪽)", rendered)
        self.assertIn("1. 검사 결과 (2026-04-21, 5쪽)", rendered)
        self.assertNotIn("관련 문서, 2-5쪽", rendered)

    def test_render_answer_uses_page_count_for_source_section_metadata(self) -> None:
        self._upsert_wiki_page(page_count=5)
        answer = FinalQaAnswer(
            status="ok",
            kakaotalk_render="검사 결과 답변입니다.",
            used_source_ids=[self.source.source_id],
        )

        rendered = self.service.render_answer(
            patient_id="P1",
            answer=answer,
            intent="OK",
            selected_source_id=self.source.source_id,
        )

        self.assertIn("1. 검사 결과 (2026-04-21, 5쪽)", rendered)

    def test_render_answer_omits_missing_source_display_parts(self) -> None:
        self._upsert_wiki_page(date="", page_count=5)
        answer = FinalQaAnswer(
            status="ok",
            kakaotalk_render="검사 결과 답변입니다.",
            used_source_ids=[self.source.source_id],
        )

        rendered = self.service.render_answer(
            patient_id="P1",
            answer=answer,
            intent="OK",
            selected_source_id=self.source.source_id,
        )

        self.assertIn("1. 검사 결과 (5쪽)", rendered)

    def test_render_answer_uses_unknown_category_fallback(self) -> None:
        self._upsert_wiki_page(category="unknown", date="", page_count=None)
        answer = FinalQaAnswer(
            status="ok",
            kakaotalk_render="검사 결과 답변입니다.",
            used_source_ids=[self.source.source_id],
        )

        rendered = self.service.render_answer(
            patient_id="P1",
            answer=answer,
            intent="OK",
            selected_source_id=self.source.source_id,
        )

        self.assertIn("1. 의료 문서", rendered)

    def test_render_answer_omits_sources_when_used_source_ids_empty(self) -> None:
        answer = FinalQaAnswer(
            status="ok",
            kakaotalk_render="고혈압은 혈압이 지속적으로 높은 상태를 말합니다.",
            used_source_ids=[],
        )

        rendered = self.service.render_answer(
            patient_id="P1",
            answer=answer,
            intent="OK",
        )

        self.assertEqual(rendered, "고혈압은 혈압이 지속적으로 높은 상태를 말합니다.")
        self.assertNotIn("📄 출처", rendered)

    def test_answer_question_without_file_allows_empty_used_sources(self) -> None:
        self.gemini_gateway.generate_result = json.dumps(
            {
                "status": "ok",
                "kakaotalk_render": "고혈압은 혈압이 지속적으로 높은 상태를 말합니다.",
                "used_source_ids": [],
            },
            ensure_ascii=False,
        )

        answer = self.service.answer_question(
            patient_id="P1",
            question="고혈압이 뭐야?",
            prior_context="",
        )

        self.assertEqual(answer.status, "ok")
        self.assertEqual(answer.used_source_ids, [])
        self.assertEqual(len(self.gemini_gateway.generate_calls[0]), 1)

    def test_answer_question_with_file_allows_general_only_ok(self) -> None:
        self.gemini_gateway.generate_result = json.dumps(
            {
                "status": "ok",
                "kakaotalk_render": "고혈압은 혈압이 지속적으로 높은 상태를 말합니다.",
                "used_source_ids": [],
            },
            ensure_ascii=False,
        )

        answer = self.service.answer_question(
            patient_id="P1",
            question="고혈압이 뭐야?",
            prior_context="",
            prepared_file=PreparedGeminiFile(
                source_id=self.source.source_id,
                file_name="files/test",
                uri="",
                mime_type="application/pdf",
                file_object=FakeGeminiFile(name="files/test"),
            ),
        )

        self.assertEqual(answer.status, "ok")
        self.assertEqual(answer.used_source_ids, [])
        self.assertEqual(len(self.gemini_gateway.generate_calls[0]), 2)

    def test_source_missing_answer_with_used_source_is_rejected(self) -> None:
        self.gemini_gateway.generate_result = json.dumps(
            {
                "status": "ok",
                "kakaotalk_render": "기록에서 확인됩니다.",
                "used_source_ids": [self.source.source_id],
            },
            ensure_ascii=False,
        )

        with self.assertRaises(GeminiFilesQaError):
            self.service.answer_question(
                patient_id="P1",
                question="나 고혈압이야?",
                prior_context="",
            )

    def test_ok_source_missing_renders_general_answer(self) -> None:
        answer = FinalQaAnswer(
            status="ok",
            kakaotalk_render="고혈압은 혈압이 지속적으로 높은 상태를 말합니다.",
            used_source_ids=[],
        )

        rendered = self.service.render_answer(
            patient_id="P1",
            answer=answer,
            intent="OK",
            selected_source_id="",
        )

        self.assertIn("고혈압은 혈압이 지속적으로 높은 상태를 말합니다.", rendered)
        self.assertNotIn("📄 출처", rendered)

    def test_answer_question_with_file_allows_matching_used_source(self) -> None:
        self.gemini_gateway.generate_result = json.dumps(
            {
                "status": "ok",
                "kakaotalk_render": "기록에서 고혈압 진단이 확인됩니다. (1쪽)",
                "used_source_ids": [self.source.source_id],
            },
            ensure_ascii=False,
        )

        answer = self.service.answer_question(
            patient_id="P1",
            question="고혈압이 뭔지 알려주고 내 기록에도 있는지 봐줘",
            prior_context="",
            prepared_file=PreparedGeminiFile(
                source_id=self.source.source_id,
                file_name="files/test",
                uri="",
                mime_type="application/pdf",
                file_object=FakeGeminiFile(name="files/test"),
            ),
        )

        self.assertEqual(answer.used_source_ids, [self.source.source_id])

    def test_non_ok_answer_with_used_source_is_rejected(self) -> None:
        self.gemini_gateway.generate_result = json.dumps(
            {
                "status": "cannot_verify",
                "kakaotalk_render": "",
                "used_source_ids": [self.source.source_id],
            },
            ensure_ascii=False,
        )

        with self.assertRaises(GeminiFilesQaError):
            self.service.answer_question(
                patient_id="P1",
                question="고혈압이 뭔지 알려주고 내 기록에도 있는지 봐줘",
                prior_context="",
                prepared_file=PreparedGeminiFile(
                    source_id=self.source.source_id,
                    file_name="files/test",
                    uri="",
                    mime_type="application/pdf",
                    file_object=FakeGeminiFile(name="files/test"),
                ),
            )

    def test_ok_cannot_verify_status_omits_sources(self) -> None:
        self.gemini_gateway.generate_result = json.dumps(
            {
                "status": "cannot_verify",
                "kakaotalk_render": "",
                "used_source_ids": [],
            },
            ensure_ascii=False,
        )

        answer = self.service.answer_question(
            patient_id="P1",
            question="고혈압이 뭔지 알려주고 내 기록에도 있는지 봐줘",
            prior_context="",
            prepared_file=PreparedGeminiFile(
                source_id=self.source.source_id,
                file_name="files/test",
                uri="",
                mime_type="application/pdf",
                file_object=FakeGeminiFile(name="files/test"),
            ),
        )

        rendered = self.service.render_answer(
            patient_id="P1",
            answer=answer,
            intent="OK",
            selected_source_id=self.source.source_id,
        )

        self.assertIn("의료 기록에서 확인하기 어렵습니다", rendered)
        self.assertNotIn("📄 출처", rendered)


if __name__ == "__main__":
    unittest.main()
