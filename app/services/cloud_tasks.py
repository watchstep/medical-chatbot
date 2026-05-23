from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

logger = logging.getLogger(__name__)


class CloudTasksEnqueueError(RuntimeError):
    pass


@dataclass(frozen=True)
class CloudTaskEnqueueResult:
    task_name: str
    duplicate: bool = False


def normalize_task_id(value: str, *, max_length: int = 500) -> str:
    """Return a Cloud Tasks compatible deterministic task id."""
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-")
    if not normalized:
        raise ValueError("task id must not be empty")
    return normalized[:max_length]


@dataclass
class CloudTasksClient:
    project_id: str
    location: str
    queue_name: str
    client: Any | None = None

    def __post_init__(self) -> None:
        if not self.project_id:
            raise ValueError("project_id is required for Cloud Tasks")
        if not self.location:
            raise ValueError("location is required for Cloud Tasks")
        if not self.queue_name:
            raise ValueError("queue_name is required for Cloud Tasks")
        if self.client is None:
            from google.cloud import tasks_v2

            self.client = tasks_v2.CloudTasksClient()

    def enqueue_http_post(
        self,
        *,
        task_id: str,
        url: str,
        payload: dict[str, Any] | None = None,
        service_account_email: str,
        audience: str,
        dispatch_deadline_seconds: int = 600,
    ) -> CloudTaskEnqueueResult:
        if not url:
            raise ValueError("url is required for Cloud Tasks HTTP target")
        if not service_account_email:
            raise ValueError("service_account_email is required for Cloud Tasks OIDC")
        if not audience:
            raise ValueError("audience is required for Cloud Tasks OIDC")
        if dispatch_deadline_seconds <= 0:
            raise ValueError("dispatch_deadline_seconds must be positive")

        from google.api_core.exceptions import AlreadyExists
        from google.cloud import tasks_v2
        from google.protobuf.duration_pb2 import Duration

        normalized_task_id = normalize_task_id(task_id)
        parent = self.client.queue_path(self.project_id, self.location, self.queue_name)
        task_name = self.client.task_path(
            self.project_id,
            self.location,
            self.queue_name,
            normalized_task_id,
        )
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        dispatch_deadline = Duration()
        dispatch_deadline.FromTimedelta(timedelta(seconds=dispatch_deadline_seconds))

        task = tasks_v2.Task(
            name=task_name,
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=url,
                headers={"Content-Type": "application/json; charset=utf-8"},
                body=body,
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=service_account_email,
                    audience=audience,
                ),
            ),
            dispatch_deadline=dispatch_deadline,
        )

        try:
            created = self.client.create_task(parent=parent, task=task)
        except AlreadyExists:
            logger.info(
                "cloud_task duplicate queue=%s task_id=%s task_name=%s",
                self.queue_name,
                normalized_task_id,
                task_name,
            )
            return CloudTaskEnqueueResult(task_name=task_name, duplicate=True)
        except Exception as exc:  # pragma: no cover - network/runtime failure
            raise CloudTasksEnqueueError(str(exc)) from exc

        return CloudTaskEnqueueResult(task_name=created.name, duplicate=False)
