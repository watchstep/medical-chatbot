from __future__ import annotations

import json
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient

try:
    import google.genai  # noqa: F401
except ImportError:
    google_mod = types.ModuleType("google")
    google_mod.__path__ = []
    genai_mod = types.ModuleType("google.genai")
    genai_types_mod = types.ModuleType("google.genai.types")

    class _DummyGenerateContentConfig:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    class _DummyThinkingConfig:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    class _DummyClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    genai_mod.Client = _DummyClient
    genai_types_mod.GenerateContentConfig = _DummyGenerateContentConfig
    genai_types_mod.ThinkingConfig = _DummyThinkingConfig
    genai_mod.types = genai_types_mod
    google_mod.genai = genai_mod
    sys.modules["google"] = google_mod
    sys.modules["google.genai"] = genai_mod
    sys.modules["google.genai.types"] = genai_types_mod

from app.config import Settings
from app.main import create_app
from app.repositories import InMemoryMedicalRepository
from app.schemas import ActiveAttachment, FinalQaAnswer, GeminiFileRuntime, KakaoUserMapping, PatientProfile
from app.services.gemini_files_qa import GeminiFilesGateway, GeminiFilesQaService, PreparedGeminiFile
from app.services.security import hash_kakao_user_id
from app.services.temporary_attachments import TemporaryAttachmentService
from app.services.temporary_attachments import build_default_temporary_attachment_service


class FakeGeminiFile:
    def __init__(self, *, name: str, state: str = "ACTIVE", mime_type: str = "application/pdf") -> None:
        self.name = name
        self.state = state
        self.uri = "gemini://temporary"
        self.mime_type = mime_type
        self.expiration_time = (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat()


class FakeGateway(GeminiFilesGateway):
    def __init__(self) -> None:
        self.uploaded: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.generate_result = ""

    def upload_file(self, *, display_name: str, content: bytes, mime_type: str) -> Any:
        self.uploaded.append({"display_name": display_name, "content": content, "mime_type": mime_type})
        return FakeGeminiFile(name=f"files/temp-{len(self.uploaded)}", mime_type=mime_type)

    def get_file(self, *, file_name: str) -> Any:
        return FakeGeminiFile(name=file_name)

    def delete_file(self, *, file_name: str) -> None:
        self.deleted.append(file_name)

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
        del model, system_instruction, response_schema, temperature, max_output_tokens, thinking_level
        if self.generate_result:
            return self.generate_result
        return json.dumps(
            {
                "status": "ok",
                "kakaotalk_render": "업로드한 파일에서 확인됩니다. (1쪽)",
                "used_source_ids": ["ATT_TEST"],
            },
            ensure_ascii=False,
        )


class FakeDriveGateway:
    def download_file_bytes(self, file_id: str) -> bytes:
        del file_id
        return b""


class TemporaryAttachmentServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            _env_file=None,
            gemini_api_key="test-key",
            upload_token_secret="secret",
            upload_base_url="https://chatbot.example",
            file_ready_wait_seconds=1,
            file_ready_poll_interval_seconds=0.5,
            file_expiration_margin_seconds=0,
        )
        self.repository = InMemoryMedicalRepository()
        self.gateway = FakeGateway()
        self.service = TemporaryAttachmentService(
            settings=self.settings,
            repository=self.repository,
            gateway=self.gateway,
        )

    def test_upload_link_token_is_stateful_and_one_use(self) -> None:
        link = self.service.create_upload_link(patient_id="P1", kakao_user_id_hash="hash-user")

        self.assertIn("/upload/", link.url)
        stored = self.service.validate_upload_token(link.token)
        self.assertEqual(stored.patient_id, "P1")

        attachment = self.service.upload_file(
            token=link.token,
            content=b"%PDF-1.7 fake",
            content_type="application/pdf",
        )

        self.assertEqual(attachment.patient_id, "P1")
        self.assertEqual(attachment.upload_token_id, link.token_id)
        self.assertEqual(attachment.mime_type, "application/pdf")
        self.assertEqual(self.repository.get_upload_token(link.token_id).status, "USED")
        with self.assertRaises(Exception):
            self.service.validate_upload_token(link.token)

    def test_default_service_can_create_upload_link_without_gemini_api_key(self) -> None:
        settings = Settings(
            _env_file=None,
            gemini_api_key=None,
            upload_token_secret="secret-secret-secret",
            upload_base_url="https://chatbot.example",
        )
        service = build_default_temporary_attachment_service(
            settings=settings,
            repository=self.repository,
        )

        link = service.create_upload_link(patient_id="P1", kakao_user_id_hash="hash-user")

        self.assertIn("https://chatbot.example/upload/", link.url)
        self.assertIsNone(service.gateway)

    def test_upload_command_returns_link_even_when_gemini_gateway_is_not_initialized(self) -> None:
        settings = Settings(
            _env_file=None,
            gemini_api_key=None,
            upload_token_secret="secret-secret-secret",
            upload_base_url="https://chatbot.example",
            medical_wiki_extraction_mode="metadata",
        )
        repository = InMemoryMedicalRepository()
        repository.upsert_patient(
            PatientProfile(
                patient_id="P1",
                name="테스트",
                birth="19900101",
                drive_folder_id="folder-p1",
            )
        )
        repository.upsert_kakao_mapping(
            KakaoUserMapping(
                kakao_user_id_hash=hash_kakao_user_id("kakao-user-1"),
                patient_id="P1",
                authenticated_at=datetime.now(timezone.utc).isoformat(),
                expires_at=(datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
                status="active",
            )
        )
        app = create_app(settings=settings, medical_repository=repository)
        client = TestClient(app)

        response = client.post(
            "/kakao/chat",
            json={
                "userRequest": {
                    "user": {"id": "kakao-user-1"},
                    "utterance": "파일 업로드",
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        text = response.json()["template"]["outputs"][0]["simpleText"]["text"]
        self.assertIn("https://chatbot.example/upload/", text)

    def test_used_token_can_report_completed_upload_without_reuploading(self) -> None:
        link = self.service.create_upload_link(patient_id="P1", kakao_user_id_hash="hash-user")
        attachment = self.service.upload_file(
            token=link.token,
            content=b"%PDF-1.7 fake",
            content_type="application/pdf",
        )

        completed = self.service.get_completed_upload_attachment(token=link.token)

        assert completed is not None
        self.assertEqual(completed.attachment_id, attachment.attachment_id)
        self.assertEqual(len(self.gateway.uploaded), 1)

    def test_used_token_replay_ignores_attachment_from_different_token(self) -> None:
        first_link = self.service.create_upload_link(patient_id="P1", kakao_user_id_hash="hash-user")
        self.service.upload_file(
            token=first_link.token,
            content=b"%PDF-1.7 fake",
            content_type="application/pdf",
        )
        second_link = self.service.create_upload_link(patient_id="P1", kakao_user_id_hash="hash-user")
        self.service.upload_file(
            token=second_link.token,
            content=b"\x89PNG\r\n\x1a\nfake",
            content_type="image/png",
        )

        completed = self.service.get_completed_upload_attachment(token=first_link.token)

        self.assertIsNone(completed)

    def test_upload_endpoint_treats_used_token_replay_as_completed(self) -> None:
        link = self.service.create_upload_link(patient_id="P1", kakao_user_id_hash="hash-user")
        self.service.upload_file(
            token=link.token,
            content=b"%PDF-1.7 fake",
            content_type="application/pdf",
        )
        app = create_app(settings=self.settings, medical_repository=self.repository)
        app.state.temporary_attachment_service = self.service
        client = TestClient(app)

        response = client.post(
            f"/upload/{link.token}",
            files={"file": ("record.pdf", b"%PDF-1.7 replay", "application/pdf")},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("이미 업로드가 완료되었습니다", response.text)
        self.assertIn("“방금 파일” 또는 “업로드한 파일”", response.text)
        self.assertIn("기존 등록 진료기록", response.text)
        self.assertEqual(len(self.gateway.uploaded), 1)

    def test_upload_endpoint_success_shows_uploaded_file_guidance(self) -> None:
        link = self.service.create_upload_link(patient_id="P1", kakao_user_id_hash="hash-user")
        app = create_app(settings=self.settings, medical_repository=self.repository)
        app.state.temporary_attachment_service = self.service
        client = TestClient(app)

        response = client.post(
            f"/upload/{link.token}",
            files={"file": ("record.pdf", b"%PDF-1.7 fake", "application/pdf")},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("파일 업로드가 완료되었습니다", response.text)
        self.assertIn("“방금 파일” 또는 “업로드한 파일”", response.text)
        self.assertIn("기존 등록 진료기록", response.text)
        self.assertIn("white-space: pre-line", response.text)

    def test_upload_form_disables_submit_button(self) -> None:
        link = self.service.create_upload_link(patient_id="P1", kakao_user_id_hash="hash-user")
        app = create_app(settings=self.settings, medical_repository=self.repository)
        app.state.temporary_attachment_service = self.service
        client = TestClient(app)

        response = client.get(f"/upload/{link.token}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("button.disabled = true", response.text)
        self.assertIn("업로드 중...", response.text)

    def test_new_upload_replaces_previous_active_attachment_and_deletes_old_file(self) -> None:
        first_link = self.service.create_upload_link(patient_id="P1", kakao_user_id_hash="hash-user")
        first = self.service.upload_file(
            token=first_link.token,
            content=b"%PDF-1.7 fake",
            content_type="application/pdf",
        )
        second_link = self.service.create_upload_link(patient_id="P1", kakao_user_id_hash="hash-user")

        second = self.service.upload_file(
            token=second_link.token,
            content=b"\x89PNG\r\n\x1a\nfake",
            content_type="image/png",
        )

        self.assertNotEqual(first.attachment_id, second.attachment_id)
        self.assertIn(first.gemini_file.file_name, self.gateway.deleted)
        active = self.repository.get_active_attachment("P1", "hash-user")
        assert active is not None
        self.assertEqual(active.attachment_id, second.attachment_id)

    def test_temporary_prepared_file_allows_generic_source_label(self) -> None:
        attachment = ActiveAttachment(
            attachment_id="ATT_TEST",
            patient_id="P1",
            kakao_user_id_hash="hash-user",
            gemini_file=GeminiFileRuntime(file_name="files/temp", state="ACTIVE", mime_type="application/pdf"),
            mime_type="application/pdf",
            expires_at=(datetime.now(timezone(timedelta(hours=9))) + timedelta(minutes=10)).isoformat(),
        )
        prepared = self.service.prepare_attachment_file(attachment=attachment)
        self.gateway.generate_result = json.dumps(
            {
                "status": "ok",
                "kakaotalk_render": "업로드한 파일에서 확인됩니다. (1쪽)",
                "used_source_ids": [attachment.attachment_id],
            },
            ensure_ascii=False,
        )
        qa_service = GeminiFilesQaService(
            settings=self.settings,
            repository=self.repository,
            drive_gateway=FakeDriveGateway(),
            gateway=self.gateway,
        )

        answer = qa_service.answer_question(
            patient_id="P1",
            question="이 파일 설명해줘",
            prior_context="",
            prepared_file=prepared,
        )
        rendered = qa_service.render_answer(
            patient_id="P1",
            answer=answer,
            selected_source_id=attachment.attachment_id,
            temporary_source_label="업로드한 파일",
        )

        self.assertEqual(prepared.source_kind, "temporary_attachment")
        self.assertIn("📄 출처", rendered)
        self.assertIn("1. 업로드한 파일", rendered)


if __name__ == "__main__":
    unittest.main()
