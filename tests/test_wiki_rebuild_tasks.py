from __future__ import annotations

import unittest

from app.config import Settings
from app.services.cloud_tasks import CloudTaskEnqueueResult
from app.services.wiki_rebuild_tasks import WikiRebuildTaskEnqueueService


class FakeCloudTasksClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def enqueue_http_post(self, **kwargs):
        self.calls.append(kwargs)
        return CloudTaskEnqueueResult(task_name="queues/wiki/tasks/task-1", duplicate=False)


class WikiRebuildTaskEnqueueServiceTest(unittest.TestCase):
    def test_enqueue_wiki_rebuild_builds_worker_endpoint_task(self) -> None:
        settings = Settings(
            _env_file=None,
            cloud_tasks_base_url="https://service.example",
            cloud_tasks_audience="https://service.example",
            cloud_tasks_service_account_email="tasks@example.iam.gserviceaccount.com",
            wiki_rebuild_tasks_dispatch_deadline_seconds=600,
        )
        fake_client = FakeCloudTasksClient()
        service = WikiRebuildTaskEnqueueService(
            settings=settings,
            cloud_tasks_client=fake_client,  # type: ignore[arg-type]
        )

        result = service.enqueue_wiki_rebuild(
            patient_id="P_001",
            source_id="SRC_001",
            reason="drive_sync",
        )

        self.assertEqual(result.task_name, "queues/wiki/tasks/task-1")
        self.assertEqual(len(fake_client.calls), 1)
        call = fake_client.calls[0]
        self.assertEqual(call["task_id"], "wiki-rebuild-p_001-src_001")
        self.assertEqual(
            call["url"],
            "https://service.example/admin/rebuild-wiki-page/P_001/SRC_001",
        )
        self.assertEqual(call["payload"]["patient_id"], "P_001")
        self.assertEqual(call["payload"]["source_id"], "SRC_001")
        self.assertEqual(call["service_account_email"], "tasks@example.iam.gserviceaccount.com")
        self.assertEqual(call["audience"], "https://service.example")


if __name__ == "__main__":
    unittest.main()
