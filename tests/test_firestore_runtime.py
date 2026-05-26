from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.repositories import InMemoryMedicalRepository
from app.schemas import (
    DriveFolderIndexEntry,
    DriveSyncState,
    FinalQaAnswer,
    GeminiFileRuntime,
    PatientProfile,
)
from app.services.drive import DriveGateway, DriveLookupService
from app.services.gemini_files_qa import PreparedGeminiFile
from app.services.security import hash_kakao_user_id


class FakeDriveGateway(DriveGateway):
    def __init__(self) -> None:
        self.changes_response: dict | Exception = {"changes": [], "newStartPageToken": "token-2"}
        self.files_by_folder = {
            "folder-p1": [
                {
                    "id": "drive-file-1",
                    "name": "검사결과_20260421.pdf",
                    "mimeType": "application/pdf",
                    "modifiedTime": "2026-04-21T10:00:00Z",
                    "parents": ["folder-p1"],
                    "size": "1234",
                    "md5Checksum": "hash-1",
                }
            ]
        }

    def list_files_in_folder(self, folder_id: str) -> list[dict]:
        return [dict(item) for item in self.files_by_folder.get(folder_id, [])]

    def download_file_bytes(self, file_id: str) -> bytes:
        return b"%PDF fake"

    def get_file(self, file_id: str) -> dict:
        for files in self.files_by_folder.values():
            for file_info in files:
                if file_info["id"] == file_id:
                    return dict(file_info)
        raise KeyError(file_id)

    def get_start_page_token(self) -> str:
        return "token-1"

    def list_changes(self, page_token: str, *, page_size: int) -> dict:
        if isinstance(self.changes_response, Exception):
            raise self.changes_response
        return dict(self.changes_response)


class FakeRouterGateway:
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
        payload = json.loads(contents[0])
        catalog_pages = payload["catalog"]["pages"]
        source_id = catalog_pages[0]["source_id"] if catalog_pages else ""
        return json.dumps(
            {
                "selection_status": "selected" if source_id else "insufficient",
                "intent": "OK",
                "primary_source_id": source_id,
                "confidence": 0.9 if source_id else 0.0,
                "reason": "test router selection",
            },
            ensure_ascii=False,
        )


class FakeGeminiFilesQaService:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.gateway = FakeRouterGateway()

    def prepare_file(
        self,
        *,
        patient_id: str,
        source_id: str,
        timing_context: dict[str, str] | None = None,
    ) -> PreparedGeminiFile:
        del timing_context
        self.calls.append({"step": "prepare", "patient_id": patient_id, "source_id": source_id})
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
        del prior_context, intent
        self.calls.append({"step": "answer", "patient_id": patient_id, "question": question})
        if prepared_file is None:
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
        del patient_id, intent, selected_source_id
        return answer.kakaotalk_render


class FakeKakaoCallbackService:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def send_text_response(self, *, callback_url: str, text: str) -> None:
        self.calls.append({"callback_url": callback_url, "text": text})


class FirestoreRuntimeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            _env_file=None,
            google_service_account_path="credentials/google-service-account.json",
            admin_auth_mode="token",
            admin_sync_token="admin-token",
            gemini_api_key="test-key",
            medical_wiki_extraction_mode="metadata",
            drive_changes_scope_id="main",
        )
        self.repository = InMemoryMedicalRepository()
        self.repository.upsert_patient(
            PatientProfile(
                patient_id="P0001",
                name="손창선",
                birth="19461230",
                drive_folder_id="folder-p1",
                drive_folder_name="손창선_19461230",
            )
        )
        self.drive_gateway = FakeDriveGateway()
        self.drive_service = DriveLookupService(settings=self.settings, gateway=self.drive_gateway)
        self.gemini_files_qa_service = FakeGeminiFilesQaService()
        self.callback_service = FakeKakaoCallbackService()
        self.client = TestClient(
            create_app(
                settings=self.settings,
                drive_service=self.drive_service,
                medical_repository=self.repository,
                gemini_files_qa_service=self.gemini_files_qa_service,  # type: ignore[arg-type]
                kakao_callback_service=self.callback_service,  # type: ignore[arg-type]
            )
        )

    def test_auth_stores_hashed_kakao_mapping(self) -> None:
        response = self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "kakao-user-1"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        mapping = self.repository.get_kakao_mapping(hash_kakao_user_id("kakao-user-1"))
        self.assertIsNotNone(mapping)
        self.assertIsNone(self.repository.get_kakao_mapping("kakao-user-1"))

    def test_admin_sync_patient_builds_source_runtime_and_wiki_index(self) -> None:
        response = self.client.post(
            "/admin/sync-drive/patient/P0001",
            headers={"x-admin-token": "admin-token"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        sources = self.repository.list_medical_sources("P0001")
        self.assertEqual(len(sources), 1)
        runtime = self.repository.get_source_runtime("P0001", sources[0].source_id)
        self.assertIsNotNone(runtime)
        index = self.repository.get_wiki_index("P0001")
        self.assertIsNotNone(index)
        self.assertEqual(len(index.pages), 1)

    def test_latest_record_message_uses_korean_category_label(self) -> None:
        self.client.post("/admin/sync-drive/patient/P0001", headers={"x-admin-token": "admin-token"})
        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "kakao-user-latest-record"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )

        response = self.client.post(
            "/kakao/chat",
            json={
                "userRequest": {
                    "user": {"id": "kakao-user-latest-record"},
                    "utterance": "의료 기록 조회",
                }
            },
        )

        text = response.json()["template"]["outputs"][0]["simpleText"]["text"]
        self.assertIn("문서 유형: 검사 결과", text)
        self.assertNotIn("lab_result", text)
        assistant_logs = [log for log in self.repository.chat_logs.values() if log.role == "assistant"]
        self.assertEqual(len(assistant_logs), 1)
        self.assertEqual(assistant_logs[0].message, text)

    def test_admin_sync_patient_skips_unchanged_source_without_rebuilding_wiki(self) -> None:
        self.client.post("/admin/sync-drive/patient/P0001", headers={"x-admin-token": "admin-token"})
        log_count = len(self.repository.wiki_logs)

        response = self.client.post(
            "/admin/sync-drive/patient/P0001",
            headers={"x-admin-token": "admin-token"},
        )

        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["updated_count"], 0)
        self.assertEqual(payload["skipped_count"], 1)
        self.assertEqual(len(self.repository.wiki_logs), log_count)

    def test_modified_source_expires_existing_gemini_file_and_returns_ready(self) -> None:
        self.client.post("/admin/sync-drive/patient/P0001", headers={"x-admin-token": "admin-token"})
        source = self.repository.list_medical_sources("P0001")[0]
        runtime = self.repository.get_source_runtime("P0001", source.source_id)
        assert runtime is not None
        runtime.gemini_file = GeminiFileRuntime(
            file_name="files/old",
            uri="https://example.test/old",
            mime_type="application/pdf",
            state="ACTIVE",
        )
        self.repository.upsert_source_runtime(runtime)
        self.drive_gateway.files_by_folder["folder-p1"][0]["modifiedTime"] = "2026-04-22T10:00:00Z"
        self.drive_gateway.files_by_folder["folder-p1"][0]["md5Checksum"] = "hash-2"

        response = self.client.post(
            "/admin/sync-drive/patient/P0001",
            headers={"x-admin-token": "admin-token"},
        )

        self.assertEqual(response.json()["updated_count"], 1)
        updated_source = self.repository.get_medical_source("P0001", source.source_id)
        updated_runtime = self.repository.get_source_runtime("P0001", source.source_id)
        assert updated_source is not None
        assert updated_runtime is not None
        self.assertEqual(updated_source.source_status, "ACTIVE")
        self.assertEqual(updated_runtime.gemini_file.state, "EXPIRED")
        self.assertEqual(updated_runtime.gemini_file.source_file_hash, "")
        self.assertEqual(updated_runtime.gemini_file.source_drive_modified_at, "")
        self.assertIsNone(updated_runtime.gemini_file.source_file_size_bytes)
        self.assertEqual(updated_runtime.sync.status, "READY")
        self.assertEqual(updated_runtime.wiki_sync.status, "READY")

    def test_sync_changes_advances_next_page_token_after_successful_page(self) -> None:
        self.repository.upsert_drive_folder_index(
            DriveFolderIndexEntry(drive_folder_id="folder-p1", patient_id="P0001")
        )
        self.repository.upsert_drive_sync_state(
            DriveSyncState(scope_id="main", saved_page_token="token-old")
        )
        self.drive_gateway.changes_response = {
            "changes": [
                {
                    "fileId": "drive-file-1",
                    "file": self.drive_gateway.files_by_folder["folder-p1"][0],
                }
            ],
            "nextPageToken": "token-next",
        }

        response = self.client.post(
            "/admin/sync-drive-changes",
            headers={"x-admin-token": "admin-token"},
        )

        self.assertTrue(response.json()["ok"])
        state = self.repository.get_drive_sync_state("main")
        assert state is not None
        self.assertEqual(state.saved_page_token, "token-next")
        self.assertEqual(state.sync_status, "READY")

    def test_sync_changes_failure_keeps_checkpoint_and_records_failure(self) -> None:
        self.repository.upsert_drive_sync_state(
            DriveSyncState(scope_id="main", saved_page_token="token-old")
        )
        self.drive_gateway.changes_response = RuntimeError("changes boom")

        response = self.client.post(
            "/admin/sync-drive-changes",
            headers={"x-admin-token": "admin-token"},
        )

        self.assertFalse(response.json()["ok"])
        state = self.repository.get_drive_sync_state("main")
        assert state is not None
        self.assertEqual(state.saved_page_token, "token-old")
        self.assertEqual(state.sync_status, "FAILED")
        self.assertIsNotNone(state.last_failure)

    def test_sync_changes_keeps_checkpoint_when_wiki_generation_is_deferred(self) -> None:
        self.settings.max_wiki_page_generations_per_run = 0
        self.repository.upsert_drive_folder_index(
            DriveFolderIndexEntry(drive_folder_id="folder-p1", patient_id="P0001")
        )
        self.repository.upsert_drive_sync_state(
            DriveSyncState(scope_id="main", saved_page_token="token-old")
        )
        self.drive_gateway.changes_response = {
            "changes": [
                {
                    "fileId": "drive-file-1",
                    "file": self.drive_gateway.files_by_folder["folder-p1"][0],
                }
            ],
            "newStartPageToken": "token-new",
        }

        response = self.client.post(
            "/admin/sync-drive-changes",
            headers={"x-admin-token": "admin-token"},
        )

        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["deferred_count"], 1)
        state = self.repository.get_drive_sync_state("main")
        assert state is not None
        self.assertEqual(state.saved_page_token, "token-old")
        sources = self.repository.list_medical_sources("P0001")
        self.assertEqual(len(sources), 1)
        runtime = self.repository.get_source_runtime("P0001", sources[0].source_id)
        assert runtime is not None
        self.assertEqual(runtime.sync.status, "WIKI_PENDING")

        self.settings.max_wiki_page_generations_per_run = 1
        response = self.client.post(
            "/admin/sync-drive-changes",
            headers={"x-admin-token": "admin-token"},
        )

        self.assertTrue(response.json()["ok"])
        state = self.repository.get_drive_sync_state("main")
        assert state is not None
        self.assertEqual(state.saved_page_token, "token-new")
        runtime = self.repository.get_source_runtime("P0001", sources[0].source_id)
        assert runtime is not None
        self.assertEqual(runtime.sync.status, "READY")

    def test_sync_changes_fails_when_active_lock_exists(self) -> None:
        self.repository.upsert_drive_sync_state(
            DriveSyncState(
                scope_id="main",
                saved_page_token="token-old",
                lock_owner="other-worker",
                lease_expires_at="2999-01-01T00:00:00+09:00",
            )
        )

        response = self.client.post(
            "/admin/sync-drive-changes",
            headers={"x-admin-token": "admin-token"},
        )

        payload = response.json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["message"], "sync lock is held by another worker")

    def test_removed_change_marks_source_deleted_and_excludes_index(self) -> None:
        self.client.post("/admin/sync-drive/patient/P0001", headers={"x-admin-token": "admin-token"})
        source = self.repository.list_medical_sources("P0001")[0]
        self.repository.upsert_drive_sync_state(
            DriveSyncState(scope_id="main", saved_page_token="token-old")
        )
        self.drive_gateway.changes_response = {
            "changes": [{"fileId": "drive-file-1", "removed": True}],
            "newStartPageToken": "token-new",
        }

        response = self.client.post(
            "/admin/sync-drive-changes",
            headers={"x-admin-token": "admin-token"},
        )

        self.assertEqual(response.json()["deleted_count"], 1)
        deleted_source = self.repository.get_medical_source("P0001", source.source_id)
        runtime = self.repository.get_source_runtime("P0001", source.source_id)
        index = self.repository.get_wiki_index("P0001")
        assert deleted_source is not None
        assert runtime is not None
        assert index is not None
        self.assertEqual(deleted_source.source_status, "DELETED")
        self.assertEqual(runtime.sync.status, "EXPIRED")
        self.assertEqual(runtime.wiki_sync.status, "EXPIRED")
        self.assertEqual(index.pages, [])

    def test_sync_changes_reassigns_file_moved_to_another_patient_folder(self) -> None:
        self.client.post("/admin/sync-drive/patient/P0001", headers={"x-admin-token": "admin-token"})
        old_source = self.repository.list_medical_sources("P0001")[0]
        self.repository.upsert_patient(
            PatientProfile(
                patient_id="P0002",
                name="홍길동",
                birth="19800515",
                drive_folder_id="folder-p2",
                drive_folder_name="홍길동_19800515",
            )
        )
        self.repository.upsert_drive_folder_index(
            DriveFolderIndexEntry(drive_folder_id="folder-p2", patient_id="P0002")
        )
        moved_file = dict(self.drive_gateway.files_by_folder["folder-p1"][0])
        moved_file["parents"] = ["folder-p2"]
        self.drive_gateway.files_by_folder["folder-p1"] = []
        self.drive_gateway.files_by_folder["folder-p2"] = [moved_file]
        self.repository.upsert_drive_sync_state(
            DriveSyncState(scope_id="main", saved_page_token="token-old")
        )
        self.drive_gateway.changes_response = {
            "changes": [{"fileId": "drive-file-1", "file": moved_file}],
            "newStartPageToken": "token-new",
        }

        response = self.client.post(
            "/admin/sync-drive-changes",
            headers={"x-admin-token": "admin-token"},
        )

        self.assertTrue(response.json()["ok"])
        old_source_after_move = self.repository.get_medical_source("P0001", old_source.source_id)
        new_sources = self.repository.list_medical_sources("P0002")
        drive_index = self.repository.get_drive_file_index("drive-file-1")
        old_index = self.repository.get_wiki_index("P0001")
        new_index = self.repository.get_wiki_index("P0002")
        assert old_source_after_move is not None
        assert drive_index is not None
        assert old_index is not None
        assert new_index is not None
        self.assertEqual(old_source_after_move.source_status, "INACTIVE")
        self.assertEqual(len(new_sources), 1)
        self.assertEqual(new_sources[0].patient_id, "P0002")
        self.assertEqual(drive_index.patient_id, "P0002")
        self.assertEqual(old_index.pages, [])
        self.assertEqual(len(new_index.pages), 1)

    def test_chat_uses_callback_job_after_sync(self) -> None:
        self.client.post("/admin/sync-drive/patient/P0001", headers={"x-admin-token": "admin-token"})
        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "kakao-user-2"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )

        response = self.client.post(
            "/kakao/chat",
            json={
                "userRequest": {
                    "user": {"id": "kakao-user-2"},
                    "utterance": "검사 결과가 어떤가요?",
                    "callbackUrl": "https://callback.example.com/job",
                }
            },
        )

        self.assertEqual(response.json(), {"version": "2.0", "useCallback": True})
        self.assertEqual(len(self.callback_service.calls), 1)
        self.assertEqual(self.callback_service.calls[0]["text"], "문서에서 관련 내용이 확인됩니다. (1쪽)")
        self.assertEqual([call["step"] for call in self.gemini_files_qa_service.calls], ["prepare", "answer"])

    def test_pending_callback_job_dedupes_but_chat_log_is_always_recorded(self) -> None:
        self.settings.callback_worker_mode = "polling"
        self.client.post("/admin/sync-drive/patient/P0001", headers={"x-admin-token": "admin-token"})
        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "kakao-user-3"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )

        for _ in range(2):
            response = self.client.post(
                "/kakao/chat",
                json={
                    "userRequest": {
                        "user": {"id": "kakao-user-3"},
                        "utterance": "검사 결과가 어떤가요?",
                        "callbackUrl": "https://callback.example.com/dedupe",
                    }
                },
            )
            self.assertEqual(response.json(), {"version": "2.0", "useCallback": True})

        self.assertEqual(len(self.repository.chat_logs), 2)
        self.assertEqual(len(self.repository.callback_jobs), 1)
        job = next(iter(self.repository.callback_jobs.values()))
        self.assertEqual(job.status, "PENDING")
        self.assertTrue(all(log.job_id == job.job_id for log in self.repository.chat_logs.values()))

    def test_callback_sent_job_is_not_reused_for_same_question(self) -> None:
        self.client.post("/admin/sync-drive/patient/P0001", headers={"x-admin-token": "admin-token"})
        self.client.post(
            "/kakao/auth",
            json={
                "userRequest": {
                    "user": {"id": "kakao-user-4"},
                    "utterance": "인증 손창선 19461230",
                }
            },
        )

        for _ in range(2):
            response = self.client.post(
                "/kakao/chat",
                json={
                    "userRequest": {
                        "user": {"id": "kakao-user-4"},
                        "utterance": "검사 결과가 어떤가요?",
                        "callbackUrl": "https://callback.example.com/sent",
                    }
                },
            )
            self.assertEqual(response.json(), {"version": "2.0", "useCallback": True})

        self.assertEqual(len(self.repository.chat_logs), 4)
        self.assertEqual(
            len([log for log in self.repository.chat_logs.values() if log.role == "assistant"]),
            2,
        )
        self.assertEqual(len(self.repository.callback_jobs), 2)
        self.assertEqual(len(self.callback_service.calls), 2)

    def test_admin_endpoints_fail_closed_without_token(self) -> None:
        response = self.client.post("/admin/sync-drive/patient/P0001")

        self.assertEqual(response.json(), {"ok": False, "error": "unauthorized"})


if __name__ == "__main__":
    unittest.main()
