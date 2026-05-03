from __future__ import annotations

import json
import unittest

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.schemas import Block, ModelAnswer
from app.services.cache import PatientDataCacheService
from app.services.drive import DriveLookupService
from app.services.gemini_qa import GeminiQaAnswer, GeminiQaError
from app.sessions import InMemorySessionStore
from tests.test_drive_service import FakeDriveGateway


class TrackingDriveLookupService(DriveLookupService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.patient_index_load_calls = 0
        self.document_registry_load_calls = 0

    def load_patient_index_with_file_id(self):  # type: ignore[override]
        self.patient_index_load_calls += 1
        return super().load_patient_index_with_file_id()

    def load_document_registry_with_file_id(self):  # type: ignore[override]
        self.document_registry_load_calls += 1
        return super().load_document_registry_with_file_id()


class FakeGeminiQaService:
    def __init__(self) -> None:
        self.answer = GeminiQaAnswer(
            model_answer=ModelAnswer(
                status="ok",
                blocks=[
                    Block(
                        type="paragraph",
                        text="최근 기록에서 혈압 관련 수치는 확인되지 않습니다.",
                    )
                ],
                used_source_ids=["chart_20260421.pdf"],
                show_sources=True,
            ),
            grounding_source_ids=["chart_20260421.pdf"],
        )
        self.raise_error: Exception | None = None
        self.calls: list[dict[str, str]] = []

    def answer_question(self, *, question, context) -> GeminiQaAnswer:
        self.calls.append(
            {
                "question": question,
                "patient_id": context.patient.patient_id,
                "store_name": context.file_search_store_name or "",
            }
        )
        if self.raise_error is not None:
            raise self.raise_error
        return self.answer


class FakeKakaoCallbackService:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def send_text_response(self, *, callback_url: str, text: str) -> None:
        self.calls.append({"callback_url": callback_url, "text": text})


class KakaoSkillEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        settings = Settings(
            google_service_account_path="credentials/google-service-account.json",
        )
        self.gateway = FakeDriveGateway()
        drive_service = TrackingDriveLookupService(settings=settings, gateway=self.gateway)
        self.patient_data_cache_service = PatientDataCacheService(
            settings=settings,
            drive_service=drive_service,
        )
        self.gemini_qa_service = FakeGeminiQaService()
        self.kakao_callback_service = FakeKakaoCallbackService()
        session_store = InMemorySessionStore(ttl_minutes=settings.session_ttl_minutes)
        app = create_app(
            settings=settings,
            drive_service=drive_service,
            gemini_qa_service=self.gemini_qa_service,
            patient_data_cache_service=self.patient_data_cache_service,
            kakao_callback_service=self.kakao_callback_service,
            session_store=session_store,
        )
        self.client = TestClient(app)

    def test_greeting_before_authentication(self) -> None:
        response = self.client.post(
            "/kakao/auth",
            json={"userRequest": {"user": {"id": "new-user"}, "utterance": "안녕하세요"}},
        )

        self.assertEqual(response.status_code, 200)
        text = response.json()["template"]["outputs"][0]["simpleText"]["text"]
        self.assertIn("먼저 환자 인증이 필요합니다.", text)

    def test_authentication_persists_mapping_without_patient_drive_lookup(self) -> None:
        response = self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "new-user"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        text = response.json()["template"]["outputs"][0]["simpleText"]["text"]
        self.assertIn("인증이 완료되었습니다.", text)
        updated_index = self.client.app.state.drive_service.load_patient_index()
        patient = next(item for item in updated_index.patients if item.patient_id == "P0001")
        self.assertIn("new-user", patient.kakao_user_ids)
        self.assertIn("patient-index", self.gateway.updated_files)

    def test_latest_record_uses_document_registry(self) -> None:
        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "new-user"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )

        response = self.client.post(
            "/kakao/chat",
            json={"userRequest": {"user": {"id": "new-user"}, "utterance": "💾 최신 기록"}},
        )

        self.assertEqual(response.status_code, 200)
        text = response.json()["template"]["outputs"][0]["simpleText"]["text"]
        self.assertIn("최근 검사결과지와 진료기록부가 등록되어 있습니다.", text)
        self.assertIn("result_20260421.pdf", text)
        self.assertEqual(self.client.app.state.drive_service.document_registry_load_calls, 1)

    def test_latest_record_returns_preparing_message_when_registry_not_ready(self) -> None:
        self.gateway.file_map["document-registry"] = json.dumps(
            {
                "generated_at": "2026-04-28T12:10:00+09:00",
                "documents": [
                    {
                        "patient_id": "P0001",
                        "filename": "result_20260421.pdf",
                        "document_type": "result",
                        "document_date": "20260421",
                        "drive_file_id": "result-new",
                        "drive_modified_time": "2026-04-21T10:00:00Z",
                        "file_hash": "result-hash",
                        "file_search_store_name": "",
                        "file_search_document_name": "",
                        "sync_status": "PENDING",
                        "synced_at": "2026-04-28T12:10:00+09:00",
                    }
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")

        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "pending-user"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )
        self.patient_data_cache_service._document_registry_state = None

        response = self.client.post(
            "/kakao/chat",
            json={"userRequest": {"user": {"id": "pending-user"}, "utterance": "💾 최신 기록"}},
        )

        self.assertEqual(response.status_code, 200)
        text = response.json()["template"]["outputs"][0]["simpleText"]["text"]
        self.assertEqual(
            text,
            "최신 진단 기록을 조회 중입니다.\n잠시 후 다시 [💾 최신 기록 조회]을 눌러 주세요.",
        )

    def test_auth_reset_removes_mapping_and_session(self) -> None:
        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "reset-user"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )
        response = self.client.post(
            "/kakao/auth",
            json={"userRequest": {"user": {"id": "reset-user"}, "utterance": "인증 초기화"}},
        )

        self.assertEqual(response.status_code, 200)
        text = response.json()["template"]["outputs"][0]["simpleText"]["text"]
        self.assertIn("기존 인증 정보를 초기화했습니다.", text)
        updated_index = self.client.app.state.drive_service.load_patient_index()
        patient = next(item for item in updated_index.patients if item.patient_id == "P0001")
        self.assertNotIn("reset-user", patient.kakao_user_ids)
        self.assertIsNone(self.client.app.state.session_store.get("reset-user"))

    def test_authenticated_free_question_returns_callback_ack_and_sends_final_answer(self) -> None:
        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "callback-user"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )

        response = self.client.post(
            "/kakao/chat",
            json={
                "userRequest": {
                    "user": {"id": "callback-user"},
                    "utterance": "혈액검사 결과가 어떤가요?",
                    "callbackUrl": "https://callback.example.com/task-1",
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"version": "2.0", "useCallback": True})
        self.assertEqual(len(self.gemini_qa_service.calls), 1)
        self.assertEqual(
            self.gemini_qa_service.calls[0]["store_name"],
            "fileSearchStores/patient-P0001",
        )
        self.assertEqual(len(self.kakao_callback_service.calls), 1)
        self.assertEqual(
            self.kakao_callback_service.calls[0],
            {
                "callback_url": "https://callback.example.com/task-1",
                "text": (
                    "최근 기록에서 혈압 관련 수치는 확인되지 않습니다.\n\n"
                    "📄 출처 (1건)\n"
                    "1. 진료기록부, 2026년 4월 21일 (chart_20260421.pdf)"
                ),
            },
        )

    def test_authenticated_free_question_renders_bullet_list_without_emoji(self) -> None:
        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "bullet-user"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )
        self.gemini_qa_service.answer = GeminiQaAnswer(
            model_answer=ModelAnswer(
                status="ok",
                blocks=[
                    Block(
                        type="bullet_list",
                        items=[
                            "Hemoglobin은 참고치보다 낮게 확인됩니다.",
                            "BUN은 참고치보다 높게 확인됩니다.",
                        ],
                    )
                ],
                used_source_ids=["result_20260421.pdf"],
                show_sources=True,
            ),
            grounding_source_ids=["result_20260421.pdf"],
        )

        self.client.post(
            "/kakao/chat",
            json={
                "userRequest": {
                    "user": {"id": "bullet-user"},
                    "utterance": "혈액검사 결과가 어떤가요?",
                    "callbackUrl": "https://callback.example.com/task-bullet",
                }
            },
        )

        self.assertEqual(
            self.kakao_callback_service.calls[0]["text"],
            (
                "- Hemoglobin은 참고치보다 낮게 확인됩니다.\n"
                "- BUN은 참고치보다 높게 확인됩니다.\n\n"
                "📄 출처 (1건)\n"
                "1. 검사결과지, 2026년 4월 21일 (result_20260421.pdf)"
            ),
        )
        self.assertNotIn("📊", self.kakao_callback_service.calls[0]["text"])
        self.assertNotIn("💊", self.kakao_callback_service.calls[0]["text"])

    def test_authenticated_free_question_dedupes_sources_and_uses_intersection(self) -> None:
        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "source-dedupe-user"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )
        self.gemini_qa_service.answer = GeminiQaAnswer(
            model_answer=ModelAnswer(
                status="ok",
                blocks=[Block(type="paragraph", text="진료기록에서 관련 내용이 확인됩니다.")],
                used_source_ids=["chart_20260421.pdf", "chart_20260421.pdf"],
                show_sources=True,
            ),
            grounding_source_ids=["result_20260421.pdf", "chart_20260421.pdf"],
        )

        self.client.post(
            "/kakao/chat",
            json={
                "userRequest": {
                    "user": {"id": "source-dedupe-user"},
                    "utterance": "진료 기록에 관련 내용이 있나요?",
                    "callbackUrl": "https://callback.example.com/task-source",
                }
            },
        )

        self.assertEqual(
            self.kakao_callback_service.calls[0]["text"],
            (
                "진료기록에서 관련 내용이 확인됩니다.\n\n"
                "📄 출처 (1건)\n"
                "1. 진료기록부, 2026년 4월 21일 (chart_20260421.pdf)"
            ),
        )

    def test_authenticated_free_question_blocks_wrong_model_source(self) -> None:
        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "wrong-source-user"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )
        self.gemini_qa_service.answer = GeminiQaAnswer(
            model_answer=ModelAnswer(
                status="ok",
                blocks=[Block(type="paragraph", text="진료기록에서 관련 내용이 확인됩니다.")],
                used_source_ids=["chart_20260421.pdf", "unknown_20260421.pdf"],
                show_sources=True,
            ),
            grounding_source_ids=["chart_20260421.pdf"],
        )

        self.client.post(
            "/kakao/chat",
            json={
                "userRequest": {
                    "user": {"id": "wrong-source-user"},
                    "utterance": "진료 기록에 관련 내용이 있나요?",
                    "callbackUrl": "https://callback.example.com/task-wrong-source",
                }
            },
        )

        self.assertEqual(
            self.kakao_callback_service.calls[0]["text"],
            "해당 내용은 제공된 진단 기록에서 확인하기 어렵습니다.",
        )

    def test_authenticated_free_question_allows_missing_grounding_source_when_model_source_is_ready(self) -> None:
        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "missing-grounding-user"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )
        self.gemini_qa_service.answer = GeminiQaAnswer(
            model_answer=ModelAnswer(
                status="ok",
                blocks=[Block(type="paragraph", text="진료기록에서 관련 내용이 확인됩니다.")],
                used_source_ids=["chart_20260421.pdf"],
                show_sources=True,
            ),
            grounding_source_ids=[],
        )

        self.client.post(
            "/kakao/chat",
            json={
                "userRequest": {
                    "user": {"id": "missing-grounding-user"},
                    "utterance": "진료 기록에 관련 내용이 있나요?",
                    "callbackUrl": "https://callback.example.com/task-missing-grounding",
                }
            },
        )

        self.assertEqual(
            self.kakao_callback_service.calls[0]["text"],
            (
                "진료기록에서 관련 내용이 확인됩니다.\n\n"
                "📄 출처 (1건)\n"
                "1. 진료기록부, 2026년 4월 21일 (chart_20260421.pdf)"
            ),
        )

    def test_authenticated_free_question_blocks_private_info_output(self) -> None:
        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "private-output-user"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )
        self.gemini_qa_service.answer = GeminiQaAnswer(
            model_answer=ModelAnswer(
                status="ok",
                blocks=[Block(type="paragraph", text="환자번호 12345가 기록되어 있습니다.")],
                used_source_ids=["chart_20260421.pdf"],
                show_sources=True,
            ),
            grounding_source_ids=["chart_20260421.pdf"],
        )

        self.client.post(
            "/kakao/chat",
            json={
                "userRequest": {
                    "user": {"id": "private-output-user"},
                    "utterance": "환자 번호가 있나요?",
                    "callbackUrl": "https://callback.example.com/task-private",
                }
            },
        )

        self.assertEqual(
            self.kakao_callback_service.calls[0]["text"],
            "⚠️ 개인정보 보호 정책에 따라 성함 이외의 세부 개인정보는 안내해 드리지 않습니다.",
        )

    def test_authenticated_free_question_rejects_forbidden_source_token_in_blocks(self) -> None:
        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "forbidden-token-user"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )
        self.gemini_qa_service.answer = GeminiQaAnswer(
            model_answer=ModelAnswer(
                status="ok",
                blocks=[Block(type="paragraph", text="chart_20260421.pdf에서 확인됩니다.")],
                used_source_ids=["chart_20260421.pdf"],
                show_sources=True,
            ),
            grounding_source_ids=["chart_20260421.pdf"],
        )

        self.client.post(
            "/kakao/chat",
            json={
                "userRequest": {
                    "user": {"id": "forbidden-token-user"},
                    "utterance": "진료 기록에 관련 내용이 있나요?",
                    "callbackUrl": "https://callback.example.com/task-forbidden-token",
                }
            },
        )

        self.assertEqual(
            self.kakao_callback_service.calls[0]["text"],
            "해당 내용은 제공된 진단 기록에서 확인하기 어렵습니다.",
        )

    def test_authenticated_free_question_uses_fixed_message_for_non_ok_status(self) -> None:
        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "non-ok-status-user"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )
        self.gemini_qa_service.answer = GeminiQaAnswer(
            model_answer=ModelAnswer(
                status="cost_block",
                blocks=[Block(type="paragraph", text="이 blocks는 출력되면 안 됩니다.")],
                used_source_ids=["chart_20260421.pdf"],
                show_sources=True,
            ),
            grounding_source_ids=["chart_20260421.pdf"],
        )

        self.client.post(
            "/kakao/chat",
            json={
                "userRequest": {
                    "user": {"id": "non-ok-status-user"},
                    "utterance": "보험금 청구 금액 알려줘",
                    "callbackUrl": "https://callback.example.com/task-status",
                }
            },
        )

        self.assertEqual(
            self.kakao_callback_service.calls[0]["text"],
            "⚠️ 비용 관련 정보는 해당 의료기관에 직접 문의하셔야 합니다.",
        )

    def test_authenticated_free_question_sends_safe_message_on_gemini_error(self) -> None:
        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "gemini-error-user"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )
        self.gemini_qa_service.raise_error = GeminiQaError("boom")

        response = self.client.post(
            "/kakao/chat",
            json={
                "userRequest": {
                    "user": {"id": "gemini-error-user"},
                    "utterance": "혈액검사 결과가 어떤가요?",
                    "callbackUrl": "https://callback.example.com/task-2",
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"version": "2.0", "useCallback": True})
        self.assertEqual(
            self.kakao_callback_service.calls[0]["text"],
            "현재 진단 기록 기반 답변을 생성하지 못했습니다. 잠시 후 다시 시도해 주세요.",
        )


if __name__ == "__main__":
    unittest.main()
