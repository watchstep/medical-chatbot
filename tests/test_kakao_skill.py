from __future__ import annotations

import json
import unittest

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.cache import PatientDataCacheService
from app.services.drive import DriveLookupService
from app.services.gemini_qa import GeminiQaError
from app.sessions import InMemorySessionStore
from tests.test_drive_service import FakeDriveGateway


class TrackingDriveLookupService(DriveLookupService):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.record_context_calls = 0
        self.patient_index_load_calls = 0
        self.document_registry_load_calls = 0

    def get_patient_record_context(self, patient):  # type: ignore[override]
        self.record_context_calls += 1
        return super().get_patient_record_context(patient)

    def load_patient_index_with_file_id(self):  # type: ignore[override]
        self.patient_index_load_calls += 1
        return super().load_patient_index_with_file_id()

    def load_document_registry_with_file_id(self):  # type: ignore[override]
        self.document_registry_load_calls += 1
        return super().load_document_registry_with_file_id()


class FakeGeminiQaService:
    def __init__(self) -> None:
        self.answer = (
            "핵심 답변: 최근 기록에서 혈압 관련 수치는 확인되지 않습니다.\n"
            "근거 문서: result_20260421.pdf, chart_20260421.pdf\n"
            "확인할 점: 담당 의료진에게 혈압 수치와 추적 계획을 확인해 주세요."
        )
        self.raise_error: Exception | None = None
        self.calls: list[dict[str, str]] = []

    def answer_question(self, *, question, context) -> str:
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
        self.assertEqual(self.client.app.state.drive_service.record_context_calls, 0)

    def test_latest_record_uses_document_registry_without_record_context_lookup(self) -> None:
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
        self.assertEqual(self.client.app.state.drive_service.record_context_calls, 0)

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
                "text": self.gemini_qa_service.answer,
            },
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
