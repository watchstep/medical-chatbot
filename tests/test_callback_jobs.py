from __future__ import annotations

import json
import unittest
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

# The unit tests use fake services and do not call the real Google SDKs.
# Stub google/genai/googleapiclient only in minimal test environments.
try:
    import google.genai  # noqa: F401
    import googleapiclient.discovery  # noqa: F401
except ImportError:
    google_mod = types.ModuleType("google")
    google_mod.__path__ = []  # mark as package

    auth_mod = types.ModuleType("google.auth")
    auth_mod.default = lambda *args, **kwargs: (object(), None)

    oauth2_mod = types.ModuleType("google.oauth2")
    service_account_mod = types.ModuleType("google.oauth2.service_account")

    class _DummyCredentials:
        @classmethod
        def from_service_account_file(cls, *args, **kwargs):
            return cls()

    service_account_mod.Credentials = _DummyCredentials
    oauth2_mod.service_account = service_account_mod

    genai_mod = types.ModuleType("google.genai")
    genai_types_mod = types.ModuleType("google.genai.types")

    class _DummyClient:
        def __init__(self, *args, **kwargs):
            pass

    class _DummyGenerateContentConfig:
        def __init__(self, *args, **kwargs):
            pass

    class _DummyThinkingConfig:
        def __init__(self, *args, **kwargs):
            pass

    genai_mod.Client = _DummyClient
    genai_types_mod.GenerateContentConfig = _DummyGenerateContentConfig
    genai_types_mod.ThinkingConfig = _DummyThinkingConfig
    genai_mod.types = genai_types_mod

    googleapiclient_mod = types.ModuleType("googleapiclient")
    discovery_mod = types.ModuleType("googleapiclient.discovery")
    errors_mod = types.ModuleType("googleapiclient.errors")
    http_mod = types.ModuleType("googleapiclient.http")

    discovery_mod.build = lambda *args, **kwargs: object()

    class _DummyHttpError(Exception):
        pass

    class _DummyMediaIoBaseDownload:
        def __init__(self, *args, **kwargs):
            pass

    errors_mod.HttpError = _DummyHttpError
    http_mod.MediaIoBaseDownload = _DummyMediaIoBaseDownload

    google_mod.auth = auth_mod
    google_mod.oauth2 = oauth2_mod
    google_mod.genai = genai_mod
    sys.modules["google"] = google_mod
    sys.modules["google.auth"] = auth_mod
    sys.modules["google.oauth2"] = oauth2_mod
    sys.modules["google.oauth2.service_account"] = service_account_mod
    sys.modules["google.genai"] = genai_mod
    sys.modules["google.genai.types"] = genai_types_mod
    sys.modules["googleapiclient"] = googleapiclient_mod
    sys.modules["googleapiclient.discovery"] = discovery_mod
    sys.modules["googleapiclient.errors"] = errors_mod
    sys.modules["googleapiclient.http"] = http_mod

from app.config import Settings
from app.repositories import InMemoryMedicalRepository
from app.schemas import (
    ChatLog,
    ChatSession,
    FinalQaAnswer,
    KakaoCallbackJob,
    MedicalSource,
    MedicalSourceRuntime,
    MedicalWikiIndex,
    MedicalWikiIndexPage,
    PatientProfile,
    RuntimeSyncState,
    SourceRef,
)
from app.services.callback_jobs import CallbackJobProcessor
from app.services.gemini_files_qa import (
    GeminiFileNotReadyError,
    MedicalRouterService,
    PreparedGeminiFile,
)
from app.services.medical_wiki import now_kst_iso


KST = timezone(timedelta(hours=9))


class FakeRouterGateway:
    def __init__(self, source_id: str) -> None:
        self.source_id = source_id

    def generate_json(
        self,
        *,
        model: str,
        system_instruction: str,
        contents: list,
        response_schema: dict,
        temperature: float,
        max_output_tokens: int,
        thinking_level: str,
    ) -> str:
        del model, system_instruction, response_schema, temperature, max_output_tokens, thinking_level
        question = json.loads(contents[0])["question"]
        if question == "고혈압이 뭐야?":
            payload = {
                "selection_status": "insufficient",
                "intent": "OK",
                "primary_source_id": "",
                "confidence": 0.95,
                "reason": "general medical question",
            }
        elif question == "나 고혈압이야?":
            payload = {
                "selection_status": "insufficient",
                "intent": "OK",
                "primary_source_id": "",
                "confidence": 0.6,
                "reason": "no matching source",
            }
        elif question == "가슴이 아프고 숨이 차요":
            payload = {
                "selection_status": "insufficient",
                "intent": "EMERGENCY",
                "primary_source_id": "",
                "confidence": 0.95,
                "reason": "emergency signal",
            }
        elif question == "진료비 얼마야?":
            payload = {
                "selection_status": "insufficient",
                "intent": "COST_BLOCK",
                "primary_source_id": "",
                "confidence": 0.95,
                "reason": "cost question",
            }
        elif question == "주민번호 알려줘":
            payload = {
                "selection_status": "insufficient",
                "intent": "PRIVACY_BLOCK",
                "primary_source_id": "",
                "confidence": 0.95,
                "reason": "privacy question",
            }
        elif question == "오늘 날씨 어때?":
            payload = {
                "selection_status": "insufficient",
                "intent": "OUT_OF_SCOPE",
                "primary_source_id": "",
                "confidence": 0.95,
                "reason": "out of scope",
            }
        else:
            payload = {
                "selection_status": "selected",
                "intent": "OK",
                "primary_source_id": self.source_id,
                "confidence": 0.95,
                "reason": "test router selection",
            }
        return json.dumps(payload, ensure_ascii=False)


class FakeGeminiFilesQaService:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.file_not_ready = False

    def prepare_file(self, *, patient_id: str, source_id: str, timing_context: dict[str, str] | None = None) -> PreparedGeminiFile:
        self.calls.append("prepare")
        if self.file_not_ready:
            raise GeminiFileNotReadyError("still processing")
        return PreparedGeminiFile(
            source_id=source_id,
            file_name="files/test",
            uri="",
            mime_type="application/pdf",
            file_object=SimpleNamespace(name="files/test"),
        )

    def answer_question(
        self,
        *,
        patient_id: str,
        question: str,
        prior_context: str,
        prepared_file: PreparedGeminiFile | None = None,
        intent: str = "OK",
    ) -> FinalQaAnswer:
        del intent, patient_id, prior_context
        self.calls.append("answer")
        if prepared_file is None:
            if "나" in question:
                return FinalQaAnswer(status="cannot_verify")
            return FinalQaAnswer(
                status="ok",
                kakaotalk_render="고혈압은 혈압이 지속적으로 높은 상태를 말합니다.",
                used_source_ids=[],
            )
        return FinalQaAnswer(
            status="ok",
            kakaotalk_render="문서에서 관련 내용이 확인됩니다. (1쪽)",
            used_source_ids=[prepared_file.source_id],
        )

    def render_answer(
        self,
        *,
        patient_id: str,
        answer: FinalQaAnswer,
        intent: str = "OK",
        selected_source_id: str = "",
    ) -> str:
        del intent, selected_source_id
        del patient_id
        self.calls.append("render")
        fixed_messages = {
            "cannot_verify": "🔍 해당 내용은 제공된 의료 기록에서 확인하기 어렵습니다.",
            "out_of_scope": "💬 의료 기록과 관련된 질문에만 답변을 드릴 수 있습니다.",
            "emergency": "🚨 즉시 의료기관을 방문하시길 바랍니다.",
            "blocked": "🔒 개인정보 보호 정책에 따라 성함 이외의 세부 개인정보는 안내해 드리지 않습니다.",
            "cost_block": "💳 비용 관련 정보는 해당 의료기관에 직접 문의하셔야 합니다.",
        }
        if answer.status in fixed_messages:
            return fixed_messages[answer.status]
        return answer.kakaotalk_render


class FakeKakaoCallbackService:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, str]] = []

    def send_text_response(self, *, callback_url: str, text: str) -> None:
        if self.fail:
            raise RuntimeError("callback boom")
        self.calls.append({"callback_url": callback_url, "text": text})


class CallbackJobProcessorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            _env_file=None,
            google_service_account_path="credentials/google-service-account.json",
            gemini_api_key="test-key",
            medical_wiki_extraction_mode="metadata",
            callback_job_max_attempts=3,
        )
        self.repository = InMemoryMedicalRepository()
        self.repository.upsert_patient(
            PatientProfile(
                patient_id="P0001",
                name="손창선",
                birth="19461230",
                drive_folder_id="folder-p1",
            )
        )
        self.source_id = "SRC_P0001_TEST"
        self.repository.upsert_medical_source(
            MedicalSource(
                source_id=self.source_id,
                patient_id="P0001",
                source_ref=SourceRef(
                    drive_file_id="drive-file-1",
                    drive_folder_id="folder-p1",
                    original_filename="검사결과.pdf",
                    mime_type="application/pdf",
                ),
                source_status="ACTIVE",
            )
        )
        self.repository.upsert_source_runtime(
            MedicalSourceRuntime(
                source_id=self.source_id,
                patient_id="P0001",
                sync=RuntimeSyncState(status="READY"),
                wiki_sync=RuntimeSyncState(status="READY"),
            )
        )
        self.repository.upsert_wiki_index(
            MedicalWikiIndex(
                patient_id="P0001",
                pages=[
                    MedicalWikiIndexPage(
                        page_id=f"PAGE_{self.source_id}",
                        source_id=self.source_id,
                        category="lab_result",
                        description="검사 결과와 관련된 질문에서 확인할 수 있는 문서입니다.",
                        tags=["검사 결과"],
                        anchors=["검사", "결과"],
                        open_when=["검사 결과를 확인하는 질문"],
                        confidence=0.9,
                    )
                ],
            )
        )
        self.repository.upsert_chat_session(
            ChatSession(patient_id="P0001", kakao_user_id_hash="hash-user", recent_messages=[])
        )
        self.qa_service = FakeGeminiFilesQaService()
        self.callback_service = FakeKakaoCallbackService()
        self.processor = CallbackJobProcessor(
            settings=self.settings,
            repository=self.repository,
            router_service=MedicalRouterService(
                settings=self.settings,
                gateway=FakeRouterGateway(self.source_id),
            ),
            gemini_files_qa_service=self.qa_service,  # type: ignore[arg-type]
            kakao_callback_service=self.callback_service,  # type: ignore[arg-type]
        )

    def _create_job(
        self,
        *,
        status: str = "PENDING",
        expires_delta_minutes: int = 10,
        message: str = "검사 결과 알려줘",
    ) -> KakaoCallbackJob:
        log = ChatLog(
            log_id="LOG_TEST",
            patient_id="P0001",
            kakao_user_id_hash="hash-user",
            message=message,
            created_at=now_kst_iso(),
        )
        self.repository.create_chat_log(log)
        job = KakaoCallbackJob(
            job_id="JOB_TEST",
            patient_id="P0001",
            kakao_user_id_hash="hash-user",
            chat_log_id=log.log_id,
            status=status,  # type: ignore[arg-type]
            callback_url="https://callback.example.test",
            max_attempts=3,
            idempotency_key="P0001:LOG_TEST",
            created_at=now_kst_iso(),
            updated_at=now_kst_iso(),
            expires_at=(datetime.now(KST) + timedelta(minutes=expires_delta_minutes)).isoformat(),
        )
        self.repository.create_callback_job(job)
        return job

    def test_process_pending_job_sends_callback_and_marks_sent(self) -> None:
        self._create_job()

        result = self.processor.process_job("JOB_TEST")

        self.assertEqual(result.status, "CALLBACK_SENT")
        self.assertTrue(result.sent)
        self.assertEqual(len(self.callback_service.calls), 1)
        stored = self.repository.get_callback_job("JOB_TEST")
        assert stored is not None
        self.assertEqual(stored.status, "CALLBACK_SENT")
        self.assertEqual(stored.callback_sent_count, 1)
        self.assertEqual(stored.sent_text_type, "answer")

    def test_general_medical_question_does_not_require_source(self) -> None:
        self._create_job(message="고혈압이 뭐야?")

        result = self.processor.process_job("JOB_TEST")

        self.assertEqual(result.status, "CALLBACK_SENT")
        self.assertEqual([call for call in self.qa_service.calls], ["answer", "render"])
        self.assertEqual(
            self.callback_service.calls[0]["text"],
            "고혈압은 혈압이 지속적으로 높은 상태를 말합니다.",
        )

    def test_patient_record_question_without_exact_router_match_uses_fallback_source(self) -> None:
        self._create_job(message="나 고혈압이야?")

        result = self.processor.process_job("JOB_TEST")

        self.assertEqual(result.status, "CALLBACK_SENT")
        self.assertEqual(self.qa_service.calls, ["prepare", "answer", "render"])
        self.assertIn("문서에서 관련 내용이 확인됩니다", self.callback_service.calls[0]["text"])


    def test_fixed_intent_emergency_bypasses_final_qa(self) -> None:
        self._create_job(message="가슴이 아프고 숨이 차요")

        result = self.processor.process_job("JOB_TEST")

        self.assertEqual(result.status, "CALLBACK_SENT")
        self.assertEqual(self.qa_service.calls, ["render"])
        self.assertEqual(self.callback_service.calls[0]["text"], "🚨 즉시 의료기관을 방문하시길 바랍니다.")

    def test_fixed_intent_cost_block_bypasses_final_qa(self) -> None:
        self._create_job(message="진료비 얼마야?")

        result = self.processor.process_job("JOB_TEST")

        self.assertEqual(result.status, "CALLBACK_SENT")
        self.assertEqual(self.qa_service.calls, ["render"])
        self.assertEqual(self.callback_service.calls[0]["text"], "💳 비용 관련 정보는 해당 의료기관에 직접 문의하셔야 합니다.")

    def test_fixed_intent_privacy_block_bypasses_final_qa(self) -> None:
        self._create_job(message="주민번호 알려줘")

        result = self.processor.process_job("JOB_TEST")

        self.assertEqual(result.status, "CALLBACK_SENT")
        self.assertEqual(self.qa_service.calls, ["render"])
        self.assertEqual(
            self.callback_service.calls[0]["text"],
            "🔒 개인정보 보호 정책에 따라 성함 이외의 세부 개인정보는 안내해 드리지 않습니다.",
        )

    def test_fixed_intent_out_of_scope_bypasses_final_qa(self) -> None:
        self._create_job(message="오늘 날씨 어때?")

        result = self.processor.process_job("JOB_TEST")

        self.assertEqual(result.status, "CALLBACK_SENT")
        self.assertEqual(self.qa_service.calls, ["render"])
        self.assertEqual(self.callback_service.calls[0]["text"], "💬 의료 기록과 관련된 질문에만 답변을 드릴 수 있습니다.")

    def test_process_pending_job_writes_timing_logs(self) -> None:
        self._create_job()

        with self.assertLogs("app.services.callback_jobs", level="INFO") as logs:
            result = self.processor.process_job("JOB_TEST")

        self.assertEqual(result.status, "CALLBACK_SENT")
        output = "\n".join(logs.output)
        for event in [
            "callback_job.start",
            "router.start",
            "router.done",
            "prepare_file.start",
            "prepare_file.done",
            "final_qa.start",
            "final_qa.done",
            "render.start",
            "render.done",
            "callback_send.start",
            "callback_send.done",
            "callback_job.done",
        ]:
            self.assertIn(f"event={event}", output)
        self.assertIn("duration_ms=", output)

    def test_callback_send_failure_marks_job_failed_and_retryable(self) -> None:
        self._create_job()
        self.processor.kakao_callback_service = FakeKakaoCallbackService(fail=True)  # type: ignore[assignment]

        result = self.processor.process_job("JOB_TEST")

        self.assertEqual(result.status, "FAILED")
        stored = self.repository.get_callback_job("JOB_TEST")
        assert stored is not None
        self.assertEqual(stored.status, "FAILED")
        self.assertEqual(stored.retry_count, 1)
        self.assertIsNotNone(stored.last_failure)
        self.assertEqual(stored.last_failure.code, "CALLBACK_SEND_FAILED")

    def test_expire_jobs_marks_expired_jobs(self) -> None:
        self._create_job(expires_delta_minutes=-1)

        payload = self.processor.expire_jobs(limit=10)

        self.assertEqual(payload["expired_count"], 1)
        stored = self.repository.get_callback_job("JOB_TEST")
        assert stored is not None
        self.assertEqual(stored.status, "EXPIRED")

    def test_locked_job_is_not_acquired_twice(self) -> None:
        job = self._create_job()
        self.repository.upsert_callback_job(
            job.model_copy(
                update={
                    "lock_owner": "other-worker",
                    "lease_expires_at": "2999-01-01T00:00:00+09:00",
                }
            )
        )

        result = self.processor.process_job("JOB_TEST")

        self.assertEqual(result.error_code, "NOT_RUNNABLE")
        self.assertEqual(len(self.callback_service.calls), 0)
        stored = self.repository.get_callback_job("JOB_TEST")
        assert stored is not None
        self.assertEqual(stored.lock_owner, "other-worker")

    def test_process_pending_jobs_uses_runnable_query(self) -> None:
        self._create_job()

        payload = self.processor.process_pending_jobs(limit=5)

        self.assertEqual(payload["processed_count"], 1)
        stored = self.repository.get_callback_job("JOB_TEST")
        assert stored is not None
        self.assertEqual(stored.status, "CALLBACK_SENT")

    def test_file_not_ready_marks_job_retryable_without_sending_callback(self) -> None:
        self._create_job()
        self.qa_service.file_not_ready = True

        result = self.processor.process_job("JOB_TEST")

        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.error_code, "FILE_NOT_READY")
        self.assertEqual(len(self.callback_service.calls), 0)
        stored = self.repository.get_callback_job("JOB_TEST")
        assert stored is not None
        self.assertTrue(stored.runnable)
        self.assertEqual(stored.status, "FAILED")
        self.assertEqual(stored.retry_count, 1)
        self.assertIsNotNone(stored.next_run_at)


if __name__ == "__main__":
    unittest.main()
