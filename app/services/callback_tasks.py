from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote

from app.config import Settings
from app.services.cloud_tasks import CloudTaskEnqueueResult, CloudTasksClient, normalize_task_id

logger = logging.getLogger(__name__)


@dataclass
class CallbackTaskEnqueueService:
    settings: Settings
    cloud_tasks_client: CloudTasksClient

    def enqueue_callback_job(self, *, job_id: str) -> CloudTaskEnqueueResult:
        if not job_id:
            raise ValueError("job_id is required")
        base_url = self.settings.cloud_tasks_base_url.rstrip("/")
        if not base_url:
            raise ValueError("cloud_tasks_base_url is required")
        audience = self.settings.cloud_tasks_audience or self.settings.cloud_tasks_base_url
        if not audience:
            raise ValueError("cloud_tasks_audience is required")

        task_id = normalize_task_id(f"callback-job-{job_id.lower()}")
        url = f"{base_url}/admin/process-callback-job/{quote(job_id, safe='')}"
        result = self.cloud_tasks_client.enqueue_http_post(
            task_id=task_id,
            url=url,
            payload={"job_id": job_id},
            service_account_email=self.settings.cloud_tasks_service_account_email,
            audience=audience,
            dispatch_deadline_seconds=self.settings.callback_tasks_dispatch_deadline_seconds,
        )
        logger.info(
            "callback_task enqueued job_id=%s task_name=%s duplicate=%s",
            job_id,
            result.task_name,
            result.duplicate,
        )
        return result
