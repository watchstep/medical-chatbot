from __future__ import annotations

import unittest

from app.config import Settings
from app.schemas import DriveFile, PatientIndexEntry, PatientMeta, PatientRecordContext
from app.services.gemini_qa import (
    CachedGeminiContext,
    GeminiDocument,
    GeminiGateway,
    GeminiQaError,
    GeminiQaService,
    GeminiRecordNotFoundError,
    UploadedGeminiFile,
)


class FakeGeminiGateway(GeminiGateway):
    def __init__(self) -> None:
        self.answer = (
            "핵심 답변: 제공된 진단 기록 문서에서 해당 내용을 확인하지 못했습니다.\n"
            "근거 문서: result_20260421.pdf, chart_20260421.pdf\n"
            "확인할 점: 담당 의료진에게 직접 확인해 주세요."
        )
        self.raise_error: Exception | None = None
        self.create_calls: list[dict] = []
        self.generate_calls: list[dict] = []
        self.deleted_contexts: list[str] = []

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
        self.create_calls.append(
            {
                "model": model,
                "system_instruction": system_instruction,
                "context_note": context_note,
                "cache_key": cache_key,
                "documents": documents,
                "ttl_hours": ttl_hours,
            }
        )
        return CachedGeminiContext(
            name=f"cachedContents/{cache_key}",
            uploaded_files=tuple(
                UploadedGeminiFile(
                    name=f"files/{index}",
                    uri=f"gs://fake/{index}",
                    display_name=document.name,
                )
                for index, document in enumerate(documents, start=1)
            ),
        )

    def generate_answer(
        self,
        *,
        model: str,
        prompt: str,
        cached_content_name: str,
    ) -> str:
        self.generate_calls.append(
            {
                "model": model,
                "prompt": prompt,
                "cached_content_name": cached_content_name,
            }
        )
        if self.raise_error is not None:
            raise self.raise_error
        return self.answer

    def delete_cached_context(self, *, cached_context: CachedGeminiContext) -> None:
        self.deleted_contexts.append(cached_context.name)


class FakeDriveService:
    def __init__(self) -> None:
        self.contents = {
            "result-new": b"result pdf bytes",
            "chart-new": b"chart pdf bytes",
            "chart-next": b"new chart pdf bytes",
        }

    def download_drive_file_bytes(self, drive_file: DriveFile) -> bytes:
        return self.contents[drive_file.file_id]


class GeminiQaServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            google_service_account_path="credentials/google-service-account.json",
            gemini_api_key="test-key",
            gemini_model="gemini-test-model",
            gemini_context_cache_ttl_hours=24,
        )
        self.gateway = FakeGeminiGateway()
        self.service = GeminiQaService(settings=self.settings, gateway=self.gateway)
        self.drive_service = FakeDriveService()

    def test_answer_question_creates_cache_with_latest_documents(self) -> None:
        context = self._build_context(
            latest_result=DriveFile(
                file_id="result-new",
                name="result_20260421.pdf",
                mime_type="application/pdf",
                date="20260421",
                type="result",
            ),
            latest_chart=DriveFile(
                file_id="chart-new",
                name="chart_20260421.pdf",
                mime_type="application/pdf",
                date="20260421",
                type="chart",
            ),
        )

        answer = self.service.answer_question(
            question="이번 기록에서 혈액검사 이상이 있나요?",
            context=context,
            drive_service=self.drive_service,
        )

        self.assertEqual(answer, self.gateway.answer)
        self.assertEqual(len(self.gateway.create_calls), 1)
        self.assertEqual(len(self.gateway.generate_calls), 1)
        create_call = self.gateway.create_calls[0]
        self.assertEqual(create_call["model"], "gemini-test-model")
        self.assertEqual(create_call["ttl_hours"], 24)
        self.assertEqual(
            [(document.name, document.content) for document in create_call["documents"]],
            [
                ("result_20260421.pdf", b"result pdf bytes"),
                ("chart_20260421.pdf", b"chart pdf bytes"),
            ],
        )
        self.assertIn("현재 참고 가능한 문서", create_call["system_instruction"])
        self.assertIn("사용자 질문", self.gateway.generate_calls[0]["prompt"])

    def test_answer_question_reuses_cache_for_same_documents(self) -> None:
        context = self._build_context(
            latest_result=DriveFile(
                file_id="result-new",
                name="result_20260421.pdf",
                mime_type="application/pdf",
                date="20260421",
                type="result",
            ),
            latest_chart=DriveFile(
                file_id="chart-new",
                name="chart_20260421.pdf",
                mime_type="application/pdf",
                date="20260421",
                type="chart",
            ),
        )

        self.service.answer_question(
            question="첫 질문",
            context=context,
            drive_service=self.drive_service,
        )
        self.service.answer_question(
            question="두 번째 질문",
            context=context,
            drive_service=self.drive_service,
        )

        self.assertEqual(len(self.gateway.create_calls), 1)
        self.assertEqual(len(self.gateway.generate_calls), 2)

    def test_answer_question_replaces_cache_when_documents_change(self) -> None:
        first_context = self._build_context(
            latest_result=DriveFile(
                file_id="result-new",
                name="result_20260421.pdf",
                mime_type="application/pdf",
                date="20260421",
                type="result",
            ),
            latest_chart=DriveFile(
                file_id="chart-new",
                name="chart_20260421.pdf",
                mime_type="application/pdf",
                date="20260421",
                type="chart",
            ),
        )
        second_context = self._build_context(
            latest_result=DriveFile(
                file_id="result-new",
                name="result_20260421.pdf",
                mime_type="application/pdf",
                date="20260421",
                type="result",
            ),
            latest_chart=DriveFile(
                file_id="chart-next",
                name="chart_20260422.pdf",
                mime_type="application/pdf",
                date="20260422",
                type="chart",
            ),
        )

        self.service.answer_question(
            question="첫 질문",
            context=first_context,
            drive_service=self.drive_service,
        )
        self.service.answer_question(
            question="두 번째 질문",
            context=second_context,
            drive_service=self.drive_service,
        )

        self.assertEqual(len(self.gateway.create_calls), 2)
        self.assertEqual(len(self.gateway.deleted_contexts), 1)

    def test_answer_question_uses_single_available_document(self) -> None:
        context = self._build_context(
            latest_result=DriveFile(
                file_id="result-new",
                name="result_20260421.pdf",
                mime_type="application/pdf",
                date="20260421",
                type="result",
            ),
            latest_chart=None,
        )

        self.service.answer_question(
            question="이번 기록에서 혈액검사 이상이 있나요?",
            context=context,
            drive_service=self.drive_service,
        )

        create_call = self.gateway.create_calls[0]
        self.assertEqual(
            [(document.name, document.content) for document in create_call["documents"]],
            [("result_20260421.pdf", b"result pdf bytes")],
        )

    def test_answer_question_raises_when_no_documents_exist(self) -> None:
        context = self._build_context(latest_result=None, latest_chart=None)

        with self.assertRaises(GeminiRecordNotFoundError):
            self.service.answer_question(
                question="이번 기록에서 혈액검사 이상이 있나요?",
                context=context,
                drive_service=self.drive_service,
            )

    def test_answer_question_wraps_gateway_error(self) -> None:
        context = self._build_context(
            latest_result=DriveFile(
                file_id="result-new",
                name="result_20260421.pdf",
                mime_type="application/pdf",
                date="20260421",
                type="result",
            ),
            latest_chart=None,
        )
        self.gateway.raise_error = RuntimeError("gateway failed")

        with self.assertRaises(GeminiQaError):
            self.service.answer_question(
                question="이번 기록에서 혈액검사 이상이 있나요?",
                context=context,
                drive_service=self.drive_service,
            )

    def _build_context(
        self,
        *,
        latest_result: DriveFile | None,
        latest_chart: DriveFile | None,
    ) -> PatientRecordContext:
        patient = PatientIndexEntry(
            patient_id="P0001",
            name="손창선",
            birth="19461230",
            folder_name="P0001_손창선_19461230",
        )
        meta = PatientMeta(
            patient_id="P0001",
            name="손창선",
            birth="19461230",
            files=[],
        )
        return PatientRecordContext(
            patient=patient,
            meta=meta,
            latest_result=latest_result,
            latest_chart=latest_chart,
        )


if __name__ == "__main__":
    unittest.main()
