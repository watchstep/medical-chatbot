from __future__ import annotations

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

    def get_patient_record_context(self, patient):  # type: ignore[override]
        self.record_context_calls += 1
        return super().get_patient_record_context(patient)

    def load_patient_index_with_file_id(self):  # type: ignore[override]
        self.patient_index_load_calls += 1
        return super().load_patient_index_with_file_id()


class FakeGeminiQaService:
    def __init__(self) -> None:
        self.answer = (
            "핵심 답변: 최근 기록에서 혈압 관련 수치는 확인되지 않습니다.\n"
            "근거 문서: result_20260421.pdf, chart_20260421.pdf\n"
            "확인할 점: 담당 의료진에게 혈압 수치와 추적 계획을 확인해 주세요."
        )
        self.raise_error: Exception | None = None
        self.calls: list[dict[str, str]] = []

    def answer_question(self, *, question, context, drive_service) -> str:
        self.calls.append(
            {
                "question": question,
                "patient_id": context.patient.patient_id,
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
            json={
                "userRequest": {
                    "user": {"id": "new-user"},
                    "utterance": "안녕하세요",
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        text = response.json()["template"]["outputs"][0]["simpleText"]["text"]
        self.assertEqual(
            text,
            "개인 의료 기록을 확인하려면 먼저 환자 인증이 필요합니다.\n"
            "아래 형식으로 입력해 주세요.\n"
            "인증 이름 생년월일 (YYYYMMDD)↩️\n\n"
            "예시:\n"
            "인증 홍길동 19890515",
        )

    def test_authentication_persists_mapping_and_prewarms_record_cache(self) -> None:
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
        self.assertEqual(
            text,
            "손창선님 안녕하세요.🙂\n"
            "인증이 완료되었습니다.\n"
            "\n"
            "아래 메뉴에서 [💾 최신 기록]을 누르거나,\n"
            '채팅창에 "최신 기록 보여줘"라고 입력해 주세요.',
        )
        updated_index = self.client.app.state.drive_service.load_patient_index()
        patient = next(item for item in updated_index.patients if item.patient_id == "P0001")
        self.assertIn("new-user", patient.kakao_user_ids)
        self.assertIn("patient-index", self.gateway.updated_files)
        cached_context = self.patient_data_cache_service.get_cached_patient_record_context(
            patient_id="P0001"
        )
        self.assertIsNotNone(cached_context)

    def test_auth_uses_patient_index_cache(self) -> None:
        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "first-user"},
                    "utterance": "안녕하세요",
                }
            },
        )
        first_load_count = self.client.app.state.drive_service.patient_index_load_calls

        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "second-user"},
                    "utterance": "안녕하세요",
                }
            },
        )

        self.assertEqual(
            self.client.app.state.drive_service.patient_index_load_calls,
            first_load_count,
        )

    def test_latest_record_returns_cached_summary_without_extra_drive_lookup(self) -> None:
        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "new-user"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )
        prewarmed_calls = self.client.app.state.drive_service.record_context_calls

        response = self.client.post(
            "/kakao/chat",
            json={
                "userRequest": {
                    "user": {"id": "new-user"},
                    "utterance": "💾 최신 기록",
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        text = response.json()["template"]["outputs"][0]["simpleText"]["text"]
        self.assertIn("최근 검사결과지와 진료기록부가 등록되어 있습니다.", text)
        self.assertEqual(
            self.client.app.state.drive_service.record_context_calls,
            prewarmed_calls,
        )

    def test_latest_record_returns_preparing_message_when_cache_missing(self) -> None:
        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "cache-miss-user"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )
        self.patient_data_cache_service._patient_record_states.clear()

        response = self.client.post(
            "/kakao/chat",
            json={
                "userRequest": {
                    "user": {"id": "cache-miss-user"},
                    "utterance": "💾 최신 기록",
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        text = response.json()["template"]["outputs"][0]["simpleText"]["text"]
        self.assertEqual(
            text,
            "최신 진단 기록을 준비하고 있습니다.\n"
            "잠시 후 다시 [💾 최신 기록]을 눌러 주세요.",
        )
        self.assertIsNotNone(
            self.patient_data_cache_service.get_cached_patient_record_context(
                patient_id="P0001"
            )
        )

    def test_mapped_user_latest_record_menu_requires_reauth(self) -> None:
        response = self.client.post(
            "/kakao/chat",
            json={
                "userRequest": {
                    "user": {"id": "kakao-user-id-1"},
                    "utterance": "💾 최신 기록",
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        text = response.json()["template"]["outputs"][0]["simpleText"]["text"]
        self.assertEqual(
            text,
            "손창선님 안녕하세요.🙂\n"
            "진단 기록 확인을 위해 다시 인증이 필요합니다.\n\n"
            "아래 형식으로 입력해 주세요.\n"
            "인증 이름 생년월일 (YYYYMMDD)↩️\n\n"
            "예시:\n"
            "인증 홍길동 19890515",
        )

    def test_free_question_requires_callback_url(self) -> None:
        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "free-question-user"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )

        response = self.client.post(
            "/kakao/chat",
            json={
                "userRequest": {
                    "user": {"id": "free-question-user"},
                    "utterance": "혈액검사 결과가 어떤가요?",
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        text = response.json()["template"]["outputs"][0]["simpleText"]["text"]
        self.assertEqual(
            text,
            "현재 진단 기록 기반 답변을 생성하지 못했습니다. 잠시 후 다시 시도해 주세요.",
        )
        self.assertEqual(len(self.kakao_callback_service.calls), 0)

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

    def test_authenticated_free_question_sends_no_record_message(self) -> None:
        del self.gateway.list_map["'patient-folder-2' in parents and name = 'meta.json' and trashed = false"]
        self.gateway.list_map["'patient-folder-2' in parents and trashed = false"] = []
        self.gateway.file_map.pop("meta-file-2", None)
        response = self.client.post(
            "/kakao/chat",
            json={
                "userRequest": {
                    "user": {"id": "mapped-user-2"},
                    "utterance": "이번 진료에서 혈액검사 해석해줘",
                    "callbackUrl": "https://callback.example.com/task-3",
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        text = response.json()["template"]["outputs"][0]["simpleText"]["text"]
        self.assertEqual(
            text,
            "홍길동님 안녕하세요.🙂\n"
            "진단 기록 확인을 위해 다시 인증이 필요합니다.\n\n"
            "아래 형식으로 입력해 주세요.\n"
            "인증 이름 생년월일 (YYYYMMDD)↩️\n\n"
            "예시:\n"
            "인증 홍길동 19890515",
        )

        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "mapped-user-2"},
                    "utterance": "인증 홍길동 19800515",
                }
            },
        )
        self.kakao_callback_service.calls.clear()

        response = self.client.post(
            "/kakao/chat",
            json={
                "userRequest": {
                    "user": {"id": "mapped-user-2"},
                    "utterance": "이번 진료에서 혈액검사 해석해줘",
                    "callbackUrl": "https://callback.example.com/task-4",
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"version": "2.0", "useCallback": True})
        self.assertEqual(
            self.kakao_callback_service.calls[0]["text"],
            "확인 가능한 최신 진단 기록이 없어 답변드리기 어렵습니다.\n"
            "병원에 기록 등록 여부를 확인해 주세요.",
        )


if __name__ == "__main__":
    unittest.main()
