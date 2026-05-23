from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import quote

from app.config import Settings
from app.services.cloud_tasks import CloudTaskEnqueueResult, CloudTasksClient, normalize_task_id

logger = logging.getLogger(__name__)


@dataclass
class WikiRebuildTaskEnqueueService:
    """Enqueue source-level Medical Wiki rebuild work to Cloud Tasks.

    The worker endpoint remains /admin/rebuild-wiki-page/{patient_id}/{source_id},
    which performs the actual inline rebuild and index recompile. This service is
    only used from Drive sync paths to keep sync requests short.
    """

    settings: Settings
    cloud_tasks_client: CloudTasksClient

    def enqueue_wiki_rebuild(
        self,
        *,
        patient_id: str,
        source_id: str,
        reason: str = "drive_sync",
    ) -> CloudTaskEnqueueResult:
        if not patient_id:
            raise ValueError("patient_id is required")
        if not source_id:
            raise ValueError("source_id is required")

        base_url = self.settings.cloud_tasks_base_url.rstrip("/")
        if not base_url:
            raise ValueError("cloud_tasks_base_url is required")
        audience = self.settings.cloud_tasks_audience or self.settings.cloud_tasks_base_url
        if not audience:
            raise ValueError("cloud_tasks_audience is required")

        task_id = normalize_task_id(f"wiki-rebuild-{patient_id.lower()}-{source_id.lower()}")
        url = (
            f"{base_url}/admin/rebuild-wiki-page/"
            f"{quote(patient_id, safe='')}/{quote(source_id, safe='')}"
        )
        result = self.cloud_tasks_client.enqueue_http_post(
            task_id=task_id,
            url=url,
            payload={
                "patient_id": patient_id,
                "source_id": source_id,
                "reason": reason,
            },
            service_account_email=self.settings.cloud_tasks_service_account_email,
            audience=audience,
            dispatch_deadline_seconds=self.settings.wiki_rebuild_tasks_dispatch_deadline_seconds,
        )
        logger.info(
            "wiki_rebuild_task enqueued patient_id=%s source_id=%s task_name=%s duplicate=%s reason=%s",
            patient_id,
            source_id,
            result.task_name,
            result.duplicate,
            reason,
        )
        return result
