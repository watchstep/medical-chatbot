from __future__ import annotations

import base64
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.repositories import InMemoryMedicalRepository
from app.schemas import ChatLog, KakaoCallbackJob, PatientProfile, RuntimeFailure
from app.services.admin_dashboard import dashboard_html
from app.services.drive import DriveLookupService


KST = timezone(timedelta(hours=9))
FOLDER_MIME = "application/vnd.google-apps.folder"


def basic_auth(username: str = "admin", password: str = "secret") -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def dashboard_script() -> str:
    return dashboard_html().split("<script>", 1)[1].split("</script>", 1)[0]


class FakeDriveGateway:
    def __init__(self) -> None:
        self.root = {
            "id": "root-1",
            "name": "medical-chatbot-local",
            "mimeType": FOLDER_MIME,
            "trashed": False,
        }

    def find_folders_by_name(self, folder_name: str, *, parent_id: str | None = None) -> list[dict]:
        if folder_name == self.root["name"]:
            return [dict(self.root)]
        return []

    def get_file(self, file_id: str) -> dict:
        if file_id == self.root["id"]:
            return dict(self.root)
        raise KeyError(file_id)


class AdminDashboardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            _env_file=None,
            admin_dashboard_enabled=True,
            admin_dashboard_username="admin",
            admin_dashboard_password="secret",
            google_drive_root_folder_name="medical-chatbot-local",
            firestore_project_id="project-1",
            firestore_database_id="db-1",
        )
        self.repository = InMemoryMedicalRepository()
        self.repository.upsert_patient(
            PatientProfile(
                patient_id="P0001",
                name="홍길동",
                birth="19890515",
                drive_folder_id="folder-p1",
            )
        )
        self.drive_gateway = FakeDriveGateway()
        self.client = TestClient(
            create_app(
                settings=self.settings,
                medical_repository=self.repository,
                drive_service=DriveLookupService(settings=self.settings, gateway=self.drive_gateway),  # type: ignore[arg-type]
            )
        )

    def test_dashboard_disabled_returns_404(self) -> None:
        client = TestClient(
            create_app(
                settings=self.settings.model_copy(update={"admin_dashboard_enabled": False}),
                medical_repository=self.repository,
            )
        )

        response = client.get("/admin/dashboard", headers=basic_auth())

        self.assertEqual(response.status_code, 404)

    def test_dashboard_requires_basic_auth(self) -> None:
        response = self.client.get("/admin/dashboard")

        self.assertEqual(response.status_code, 401)
        self.assertIn("Basic", response.headers.get("www-authenticate", ""))

    def test_dashboard_rejects_wrong_password(self) -> None:
        response = self.client.get("/admin/dashboard", headers=basic_auth(password="wrong"))

        self.assertEqual(response.status_code, 401)

    def test_dashboard_accepts_basic_auth(self) -> None:
        response = self.client.get("/admin/dashboard", headers=basic_auth())

        self.assertEqual(response.status_code, 200)
        self.assertIn("Medical Chatbot Admin", response.text)
        self.assertIn('id="limit" type="number" value="20"', response.text)
        self.assertIn("CSV 다운로드", response.text)
        self.assertNotIn("CSV export", response.text)
        self.assertNotIn("export-chat-logs", response.text)

    def test_dashboard_script_keeps_csv_newline_escaped(self) -> None:
        script = dashboard_script()

        self.assertIn(".join('\\n')", script)
        self.assertNotIn(".join('\n')", script)

    def test_dashboard_status_does_not_return_password(self) -> None:
        response = self.client.get("/admin/dashboard/status", headers=basic_auth())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["firestore"], "connected")
        self.assertEqual(payload["drive"], "connected")
        self.assertNotIn("admin_dashboard_password", payload)
        self.assertNotIn("secret", str(payload))

    def test_chat_logs_recent_mode_is_paginated_and_privacy_safe(self) -> None:
        self.repository.create_chat_log(
            ChatLog(
                log_id="LOG_1",
                patient_id="P0001",
                kakao_user_id_hash="sha256:abcdef1234567890",
                message="이전 질문",
                created_at="2026-05-23T12:20:00+09:00",
                job_id="JOB_1",
            )
        )
        self.repository.create_chat_log(
            ChatLog(
                log_id="ANSWER_JOB_1",
                patient_id="P0001",
                kakao_user_id_hash="sha256:abcdef1234567890",
                role="assistant",
                message="이전 답변",
                message_type="answer",
                created_at="2026-05-23T12:21:00+09:00",
                job_id="JOB_1",
            )
        )
        self.repository.create_chat_log(
            ChatLog(
                log_id="LOG_2",
                patient_id="P0001",
                kakao_user_id_hash="sha256:abcdef1234567890",
                message="최근 질문",
                created_at="2026-05-23T12:30:00+09:00",
                job_id="JOB_2",
            )
        )
        self.repository.create_chat_log(
            ChatLog(
                log_id="ANSWER_JOB_2",
                patient_id="P0001",
                kakao_user_id_hash="sha256:abcdef1234567890",
                role="assistant",
                message="최근 답변",
                message_type="answer",
                created_at="2026-05-23T12:31:00+09:00",
                job_id="JOB_2",
            )
        )

        response = self.client.get(
            "/admin/dashboard/chat-logs",
            params={
                "mode": "recent",
                "limit": 1,
                "page": 1,
            },
            headers=basic_auth(),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        item = payload["items"][0]
        self.assertEqual(item["question"], "최근 질문")
        self.assertEqual(item["answer"], "최근 답변")
        self.assertEqual(item["question_created_at_display"], "2026-05-23 12:30:00 KST")
        self.assertEqual(item["answer_created_at_display"], "2026-05-23 12:31:00 KST")
        self.assertTrue(payload["has_next"])
        self.assertFalse(payload["has_prev"])
        self.assertNotIn("birth", item)
        self.assertNotIn("callback_url", item)
        self.assertNotIn("source_id", item)
        self.assertNotIn("job_id", item)

        page_2 = self.client.get(
            "/admin/dashboard/chat-logs",
            params={"mode": "recent", "limit": 1, "page": 2},
            headers=basic_auth(),
        )

        self.assertEqual(page_2.status_code, 200)
        self.assertEqual(page_2.json()["items"][0]["question"], "이전 질문")
        self.assertEqual(page_2.json()["items"][0]["answer"], "이전 답변")
        self.assertTrue(page_2.json()["has_prev"])

    def test_chat_logs_range_mode_filters_by_created_at(self) -> None:
        self.repository.create_chat_log(
            ChatLog(
                log_id="LOG_OLD",
                patient_id="P0001",
                kakao_user_id_hash="sha256:old",
                message="범위 밖",
                created_at="2026-05-22T23:59:00+09:00",
            )
        )
        self.repository.create_chat_log(
            ChatLog(
                log_id="LOG_IN",
                patient_id="P0001",
                kakao_user_id_hash="sha256:in",
                message="범위 안",
                created_at="2026-05-23T12:30:00+09:00",
            )
        )

        response = self.client.get(
            "/admin/dashboard/chat-logs",
            params={
                "mode": "range",
                "start_at": "2026-05-23T00:00:00+09:00",
                "end_at": "2026-05-24T00:00:00+09:00",
                "limit": 10,
            },
            headers=basic_auth(),
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["question"] for item in payload["items"]], ["범위 안"])
        self.assertEqual(payload["mode"], "range")

    def test_chat_logs_pair_immediate_answer_by_related_log_id(self) -> None:
        self.repository.create_chat_log(
            ChatLog(
                log_id="LOG_DIRECT",
                patient_id="P0001",
                kakao_user_id_hash="sha256:direct",
                message="최근 기록 보여줘",
                created_at="2026-05-23T12:30:00+09:00",
            )
        )
        self.repository.create_chat_log(
            ChatLog(
                log_id="ANSWER_LOG_DIRECT",
                patient_id="P0001",
                kakao_user_id_hash="sha256:direct",
                role="assistant",
                message="최근 기록 안내",
                message_type="system",
                created_at="2026-05-23T12:30:01+09:00",
            )
        )

        response = self.client.get("/admin/dashboard/chat-logs", headers=basic_auth())

        self.assertEqual(response.status_code, 200)
        item = response.json()["items"][0]
        self.assertEqual(item["question"], "최근 기록 보여줘")
        self.assertEqual(item["answer"], "최근 기록 안내")
        self.assertEqual(item["answer_type"], "system")

    def test_chat_log_export_endpoints_are_removed(self) -> None:
        self.assertEqual(
            self.client.post("/admin/export-chat-logs", headers=basic_auth()).status_code,
            404,
        )
        self.assertEqual(
            self.client.get("/admin/dashboard/chat-log-export", headers=basic_auth()).status_code,
            404,
        )
        self.assertEqual(
            self.client.post("/admin/dashboard/actions/export-chat-logs", headers=basic_auth()).status_code,
            404,
        )

    def test_callback_job_summary_returns_counts_and_recent_failures(self) -> None:
        self.repository.create_callback_job(
            KakaoCallbackJob(
                job_id="JOB_FAILED",
                patient_id="P0001",
                kakao_user_id_hash="sha256:user",
                chat_log_id="LOG_1",
                status="FAILED",
                retry_count=1,
                max_attempts=3,
                last_failure=RuntimeFailure(
                    code="CALLBACK_SEND_FAILED",
                    message="callback failed",
                    failed_at=datetime.now(KST).isoformat(),
                ),
                created_at="2026-05-23T12:30:00+09:00",
                updated_at="2026-05-23T12:31:00+09:00",
            )
        )

        response = self.client.get("/admin/dashboard/callback-jobs", headers=basic_auth())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["counts"]["FAILED"], 1)
        self.assertEqual(payload["recent_failed_jobs"][0]["last_failure_code"], "CALLBACK_SEND_FAILED")
        self.assertNotIn("callback_url", payload["recent_failed_jobs"][0])


if __name__ == "__main__":
    unittest.main()
