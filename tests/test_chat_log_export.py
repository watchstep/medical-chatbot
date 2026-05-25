from __future__ import annotations

import csv
import io
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.repositories import InMemoryMedicalRepository
from app.schemas import ChatLog, ChatLogExportState, PatientProfile
from app.services.chat_log_export import ChatLogExportService, build_chat_log_csv
from app.services.drive import DriveGateway


KST = timezone(timedelta(hours=9))
FOLDER_MIME = "application/vnd.google-apps.folder"
CSV_MIME = "text/csv"


class FakeDriveGateway(DriveGateway):
    def __init__(self) -> None:
        self.folders: dict[str, dict] = {
            "root-1": {
                "id": "root-1",
                "name": "medical-chatbot-local",
                "mimeType": FOLDER_MIME,
                "parents": [],
                "trashed": False,
            }
        }
        self.files: dict[str, dict] = {}
        self.uploads: list[dict] = []
        self.updates: list[dict] = []
        self._next_id = 1

    def list_files_in_folder(self, folder_id: str) -> list[dict]:
        return [
            dict(item)
            for item in self.files.values()
            if folder_id in item.get("parents", []) and not item.get("trashed")
        ]

    def list_child_folders(self, folder_id: str) -> list[dict]:
        return [
            dict(item)
            for item in self.folders.values()
            if folder_id in item.get("parents", []) and not item.get("trashed")
        ]

    def find_folders_by_name(self, folder_name: str, *, parent_id: str | None = None) -> list[dict]:
        return [
            dict(item)
            for item in self.folders.values()
            if item["name"] == folder_name
            and not item.get("trashed")
            and (parent_id is None or parent_id in item.get("parents", []))
        ]

    def find_files_by_name(
        self,
        file_name: str,
        *,
        parent_id: str | None = None,
        mime_type: str | None = None,
    ) -> list[dict]:
        return [
            dict(item)
            for item in self.files.values()
            if item["name"] == file_name
            and not item.get("trashed")
            and (parent_id is None or parent_id in item.get("parents", []))
            and (mime_type is None or item.get("mimeType") == mime_type)
        ]

    def create_folder(self, folder_name: str, *, parent_id: str | None = None) -> dict:
        folder_id = f"folder-{self._next_id}"
        self._next_id += 1
        folder = {
            "id": folder_id,
            "name": folder_name,
            "mimeType": FOLDER_MIME,
            "parents": [parent_id] if parent_id else [],
            "trashed": False,
        }
        self.folders[folder_id] = folder
        return dict(folder)

    def upload_file_bytes(
        self,
        *,
        parent_id: str,
        file_name: str,
        content: bytes,
        mime_type: str,
    ) -> dict:
        file_id = f"file-{self._next_id}"
        self._next_id += 1
        file_info = {
            "id": file_id,
            "name": file_name,
            "mimeType": mime_type,
            "parents": [parent_id],
            "content": content,
            "trashed": False,
        }
        self.files[file_id] = file_info
        self.uploads.append(dict(file_info))
        return dict(file_info)

    def update_file_bytes(
        self,
        *,
        file_id: str,
        content: bytes,
        mime_type: str,
    ) -> dict:
        self.files[file_id]["content"] = content
        self.files[file_id]["mimeType"] = mime_type
        self.updates.append(dict(self.files[file_id]))
        return dict(self.files[file_id])

    def download_file_bytes(self, file_id: str) -> bytes:
        return bytes(self.files[file_id]["content"])

    def get_file(self, file_id: str) -> dict:
        if file_id in self.folders:
            return dict(self.folders[file_id])
        return dict(self.files[file_id])

    def get_start_page_token(self) -> str:
        return "token"

    def list_changes(self, page_token: str, *, page_size: int) -> dict:
        return {"changes": [], "newStartPageToken": page_token}


class ChatLogExportServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            _env_file=None,
            google_drive_root_folder_name="medical-chatbot-local",
            chat_log_export_enabled=True,
            chat_log_export_interval_hours=1,
            chat_log_export_anchor_hour=1,
        )
        self.repository = InMemoryMedicalRepository()
        self.repository.upsert_patient(
            PatientProfile(
                patient_id="P0001",
                name="홍길동",
                birth="19890515",
                drive_folder_id="patient-folder",
            )
        )
        self.drive_gateway = FakeDriveGateway()
        self.service = ChatLogExportService(
            settings=self.settings,
            repository=self.repository,
            drive_gateway=self.drive_gateway,
        )

    def test_one_hour_slot_exports_current_previous_hour_only(self) -> None:
        self.repository.create_chat_log(
            ChatLog(
                log_id="LOG_OLD",
                patient_id="P0001",
                kakao_user_id_hash="sha256:old",
                message="이전 질문",
                created_at="2026-05-23T11:30:00+09:00",
            )
        )
        self.repository.create_chat_log(
            ChatLog(
                log_id="LOG_NOW",
                patient_id="P0001",
                kakao_user_id_hash="sha256:current",
                message="현재 slot 질문",
                created_at="2026-05-23T12:30:00+09:00",
            )
        )

        result = self.service.export_latest_closed_slot(
            now=datetime(2026, 5, 23, 13, 0, tzinfo=KST)
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "exported")
        self.assertEqual(result.slot_start_at, "2026-05-23T12:00:00+09:00")
        self.assertEqual(result.slot_end_at, "2026-05-23T13:00:00+09:00")
        self.assertEqual(result.exported_count, 1)
        self.assertEqual(self.drive_gateway.uploads[0]["name"], "chat_logs_20260523_1200_1300.csv")
        csv_text = self.drive_gateway.uploads[0]["content"].decode("utf-8-sig")
        self.assertIn("현재 slot 질문", csv_text)
        self.assertNotIn("이전 질문", csv_text)

    def test_three_hour_anchor_uses_0100_0400_slot(self) -> None:
        settings = self.settings.model_copy(update={"chat_log_export_interval_hours": 3})
        service = ChatLogExportService(
            settings=settings,
            repository=self.repository,
            drive_gateway=self.drive_gateway,
        )

        slot = service._latest_closed_slot(datetime(2026, 5, 23, 4, 5, tzinfo=KST))

        self.assertEqual(slot.start_iso, "2026-05-23T01:00:00+09:00")
        self.assertEqual(slot.end_iso, "2026-05-23T04:00:00+09:00")

    def test_resume_after_long_pause_exports_only_latest_closed_slot(self) -> None:
        self.repository.upsert_chat_log_export_state(
            ChatLogExportState(
                state_id="main",
                last_exported_slot_end_at="2026-05-20T01:00:00+09:00",
            )
        )
        self.repository.create_chat_log(
            ChatLog(
                log_id="LOG_BACKLOG",
                patient_id="P0001",
                kakao_user_id_hash="sha256:backlog",
                message="밀린 질문",
                created_at="2026-05-20T01:30:00+09:00",
            )
        )
        self.repository.create_chat_log(
            ChatLog(
                log_id="LOG_CURRENT",
                patient_id="P0001",
                kakao_user_id_hash="sha256:current",
                message="재개 후 직전 slot 질문",
                created_at="2026-05-23T12:30:00+09:00",
            )
        )

        result = self.service.export_latest_closed_slot(
            now=datetime(2026, 5, 23, 13, 0, tzinfo=KST)
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.exported_count, 1)
        self.assertEqual(result.slot_start_at, "2026-05-23T12:00:00+09:00")
        csv_text = self.drive_gateway.uploads[0]["content"].decode("utf-8-sig")
        self.assertIn("재개 후 직전 slot 질문", csv_text)
        self.assertNotIn("밀린 질문", csv_text)

    def test_duplicate_run_does_not_create_second_file(self) -> None:
        self.repository.create_chat_log(
            ChatLog(
                log_id="LOG_NOW",
                patient_id="P0001",
                kakao_user_id_hash="sha256:current",
                message="현재 slot 질문",
                created_at="2026-05-23T12:30:00+09:00",
            )
        )

        first = self.service.export_latest_closed_slot(now=datetime(2026, 5, 23, 13, 0, tzinfo=KST))
        second = self.service.export_latest_closed_slot(now=datetime(2026, 5, 23, 13, 5, tzinfo=KST))

        self.assertEqual(first.status, "exported")
        self.assertEqual(second.status, "already_exported")
        self.assertEqual(len(self.drive_gateway.uploads), 1)

    def test_csv_has_utf8_bom_and_escapes_formula_like_message(self) -> None:
        rows = [
            type(
                "Row",
                (),
                {
                    "created_at_kst": "2026-05-23T12:00:00+09:00",
                    "patient_name": "홍길동",
                    "patient_id": "P0001",
                    "kakao_user_hash_prefix": "sha256:abcd",
                    "message_type": "question",
                    "message": "=IMPORTXML(\"http://example\")",
                },
            )()
        ]

        content = build_chat_log_csv(rows).decode("utf-8-sig")
        parsed = list(csv.reader(io.StringIO(content)))

        self.assertTrue(build_chat_log_csv(rows).startswith(b"\xef\xbb\xbf"))
        self.assertEqual(parsed[1][5], "'=IMPORTXML(\"http://example\")")


class ChatLogExportEndpointTest(unittest.TestCase):
    def test_disabled_endpoint_does_not_require_repository_or_drive(self) -> None:
        client = TestClient(
            create_app(
                settings=Settings(
                    _env_file=None,
                    admin_auth_mode="token",
                    admin_sync_token="admin-token",
                    chat_log_export_enabled=False,
                ),
                drive_service=None,
                medical_repository=None,
            )
        )

        response = client.post("/admin/export-chat-logs", headers={"x-admin-token": "admin-token"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "disabled")


if __name__ == "__main__":
    unittest.main()
