from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.callback_jobs import CallbackJobProcessResult


class FakeCallbackJobProcessor:
    def __init__(self, result: CallbackJobProcessResult) -> None:
        self.result = result

    def process_job(self, job_id: str) -> CallbackJobProcessResult:
        return self.result


class CallbackTaskEndpointTest(unittest.TestCase):
    def _client(self, result: CallbackJobProcessResult) -> TestClient:
        app = create_app(
            settings=Settings(
                _env_file=None,
                admin_auth_mode="token",
                admin_sync_token="secret",
                kakao_callback_timeout_seconds=5,
            ),
        )
        app.state.callback_job_processor = FakeCallbackJobProcessor(result)
        return TestClient(app)

    def test_callback_task_endpoint_returns_503_for_retryable_failure(self) -> None:
        client = self._client(
            CallbackJobProcessResult(job_id="JOB_TEST", status="FAILED", error_code="FILE_NOT_READY")
        )

        response = client.post("/admin/process-callback-job/JOB_TEST", headers={"x-admin-token": "secret"})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["error_code"], "FILE_NOT_READY")

    def test_callback_task_endpoint_returns_200_for_non_retryable_result(self) -> None:
        client = self._client(
            CallbackJobProcessResult(job_id="JOB_TEST", status="CALLBACK_SENT", sent=True, text_type="answer")
        )

        response = client.post("/admin/process-callback-job/JOB_TEST", headers={"x-admin-token": "secret"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["status"], "CALLBACK_SENT")


if __name__ == "__main__":
    unittest.main()
