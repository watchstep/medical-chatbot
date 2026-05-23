from __future__ import annotations

import unittest

from app.config import Settings
from app.services.callback_tasks import CallbackTaskEnqueueService
from app.services.cloud_tasks import CloudTaskEnqueueResult


class FakeCloudTasksClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def enqueue_http_post(self, **kwargs):
        self.calls.append(kwargs)
        return CloudTaskEnqueueResult(task_name="queues/q/tasks/t", duplicate=False)


class CallbackTaskEnqueueServiceTest(unittest.TestCase):
    def test_enqueue_callback_job_builds_worker_request(self) -> None:
        settings = Settings(
            _env_file=None,
            firestore_project_id="project-1",
            cloud_tasks_base_url="https://service.example",
            cloud_tasks_audience="https://service.example",
            cloud_tasks_service_account_email="worker@example.iam.gserviceaccount.com",
            callback_tasks_dispatch_deadline_seconds=300,
        )
        client = FakeCloudTasksClient()
        service = CallbackTaskEnqueueService(settings=settings, cloud_tasks_client=client)  # type: ignore[arg-type]

        result = service.enqueue_callback_job(job_id="JOB_ABC123")

        self.assertEqual(result.task_name, "queues/q/tasks/t")
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(call["task_id"], "callback-job-job_abc123")
        self.assertEqual(call["url"], "https://service.example/admin/process-callback-job/JOB_ABC123")
        self.assertEqual(call["payload"], {"job_id": "JOB_ABC123"})
        self.assertEqual(call["service_account_email"], "worker@example.iam.gserviceaccount.com")
        self.assertEqual(call["audience"], "https://service.example")
        self.assertEqual(call["dispatch_deadline_seconds"], 300)


if __name__ == "__main__":
    unittest.main()
