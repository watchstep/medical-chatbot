from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

if "google" not in sys.modules:
    google_mod = types.ModuleType("google")
    google_mod.__path__ = []
    genai_mod = types.ModuleType("google.genai")
    genai_types_mod = types.ModuleType("google.genai.types")
    genai_mod.Client = object
    genai_types_mod.GenerateContentConfig = object
    genai_types_mod.ThinkingConfig = object
    genai_mod.types = genai_types_mod
    cloud_mod = types.ModuleType("google.cloud")
    tasks_mod = types.ModuleType("google.cloud.tasks_v2")
    api_core_mod = types.ModuleType("google.api_core")
    exceptions_mod = types.ModuleType("google.api_core.exceptions")

    class AlreadyExists(Exception):
        pass

    exceptions_mod.AlreadyExists = AlreadyExists
    google_mod.genai = genai_mod
    google_mod.cloud = cloud_mod
    sys.modules["google"] = google_mod
    sys.modules["google.genai"] = genai_mod
    sys.modules["google.genai.types"] = genai_types_mod
    sys.modules["google.cloud"] = cloud_mod
    sys.modules["google.cloud.tasks_v2"] = tasks_mod
    sys.modules["google.api_core"] = api_core_mod
    sys.modules["google.api_core.exceptions"] = exceptions_mod

from app.config import Settings
from app.repositories import InMemoryMedicalRepository
from app.schemas import (
    GeminiFileRuntime,
    MedicalSource,
    MedicalSourceRuntime,
    MedicalWikiIndex,
    MedicalWikiIndexPage,
    PatientProfile,
    RuntimeSyncState,
    SourceRef,
)
from app.services.gemini_file_prewarm import (
    GeminiFilePrewarmEnqueueService,
    GeminiFilePrewarmRetryableError,
    GeminiFilePrewarmService,
)
from app.services.gemini_files_qa import PreparedGeminiFile


KST = timezone(timedelta(hours=9))


class FakeCloudTasksClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def enqueue_http_post(self, **kwargs):
        from app.services.cloud_tasks import CloudTaskEnqueueResult

        self.calls.append(kwargs)
        return CloudTaskEnqueueResult(task_name="tasks/prewarm", duplicate=False)


class FakeQaService:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail = False

    def prepare_file(self, *, patient_id: str, source_id: str, timing_context=None):
        self.calls.append({"patient_id": patient_id, "source_id": source_id, "timing_context": timing_context or {}})
        if self.fail:
            raise RuntimeError("upload failed")
        return PreparedGeminiFile(
            source_id=source_id,
            file_name="files/prewarm",
            uri="gemini://prewarm",
            mime_type="application/pdf",
            file_object=SimpleNamespace(name="files/prewarm"),
        )


def future_expiration(minutes: int = 60) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


class GeminiFilePrewarmTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            _env_file=None,
            gemini_api_key="test-key",
            medical_wiki_extraction_mode="metadata",
            cloud_tasks_project_id="test-project",
            prewarm_tasks_queue_name="prewarm",
            cloud_tasks_base_url="https://service",
            cloud_tasks_audience="https://service",
            cloud_tasks_service_account_email="scheduler@test-project.iam.gserviceaccount.com",
        )
        self.repository = InMemoryMedicalRepository()
        self.repository.upsert_patient(PatientProfile(patient_id="P1", name="손창선", birth="19461230", drive_folder_id="folder"))
        self.source_id = "SRC_P1_A"
        self.repository.upsert_medical_source(
            MedicalSource(
                source_id=self.source_id,
                patient_id="P1",
                source_ref=SourceRef(
                    drive_file_id="drive-file",
                    mime_type="application/pdf",
                    file_size_bytes=1234,
                    file_hash="hash-1",
                    drive_modified_at="2026-05-01T00:00:00Z",
                ),
                source_status="ACTIVE",
            )
        )
        self.repository.upsert_source_runtime(
            MedicalSourceRuntime(
                source_id=self.source_id,
                patient_id="P1",
                sync=RuntimeSyncState(status="READY"),
                wiki_sync=RuntimeSyncState(status="READY"),
            )
        )
        self.repository.upsert_wiki_index(
            MedicalWikiIndex(
                patient_id="P1",
                pages=[
                    MedicalWikiIndexPage(
                        page_id=f"PAGE_{self.source_id}",
                        source_id=self.source_id,
                        category="lab_result",
                        date="2026-05-01",
                        confidence=0.9,
                    )
                ],
            )
        )
        self.cloud_tasks = FakeCloudTasksClient()
        self.qa_service = FakeQaService()

    def test_enqueue_latest_source_creates_job_and_cloud_task(self) -> None:
        service = GeminiFilePrewarmEnqueueService(
            settings=self.settings,
            repository=self.repository,
            cloud_tasks_client=self.cloud_tasks,  # type: ignore[arg-type]
        )

        result = service.enqueue_latest_source(patient_id="P1", reason="auth_success")

        self.assertTrue(result.ok)
        self.assertTrue(result.enqueued)
        self.assertEqual(result.source_id, self.source_id)
        self.assertEqual(len(self.cloud_tasks.calls), 1)
        job = self.repository.get_prewarm_job(result.job_id)
        assert job is not None
        self.assertEqual(job.status, "PENDING")

    def test_process_job_calls_prepare_file_and_marks_done(self) -> None:
        enqueue = GeminiFilePrewarmEnqueueService(
            settings=self.settings,
            repository=self.repository,
            cloud_tasks_client=self.cloud_tasks,  # type: ignore[arg-type]
        )
        job_id = enqueue.enqueue_latest_source(patient_id="P1", reason="medical_record_lookup").job_id
        worker = GeminiFilePrewarmService(
            settings=self.settings,
            repository=self.repository,
            qa_service=self.qa_service,  # type: ignore[arg-type]
            cleanup_service=None,
        )

        result = worker.process_job(job_id)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "DONE")
        self.assertEqual(len(self.qa_service.calls), 1)
        stored = self.repository.get_prewarm_job(job_id)
        assert stored is not None
        self.assertEqual(stored.status, "DONE")

    def test_process_job_skips_when_file_already_active(self) -> None:
        runtime = self.repository.get_source_runtime("P1", self.source_id)
        assert runtime is not None
        self.repository.upsert_source_runtime(
            runtime.model_copy(
                update={
                    "gemini_file": GeminiFileRuntime(
                        file_name="files/already-active",
                        state="ACTIVE",
                        expiration_time=future_expiration(),
                        source_drive_modified_at="2026-05-01T00:00:00Z",
                        source_file_hash="hash-1",
                        source_file_size_bytes=1234,
                    )
                }
            )
        )
        enqueue = GeminiFilePrewarmEnqueueService(
            settings=self.settings,
            repository=self.repository,
            cloud_tasks_client=self.cloud_tasks,  # type: ignore[arg-type]
        )
        job_id = enqueue.enqueue_latest_source(patient_id="P1", reason="auth_success").job_id
        worker = GeminiFilePrewarmService(
            settings=self.settings,
            repository=self.repository,
            qa_service=self.qa_service,  # type: ignore[arg-type]
            cleanup_service=None,
        )

        result = worker.process_job(job_id)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "SKIPPED")
        self.assertEqual(result.reason, "already_active")
        self.assertEqual(len(self.qa_service.calls), 0)

    def test_prepare_failure_marks_failed_and_raises_retryable(self) -> None:
        enqueue = GeminiFilePrewarmEnqueueService(
            settings=self.settings,
            repository=self.repository,
            cloud_tasks_client=self.cloud_tasks,  # type: ignore[arg-type]
        )
        job_id = enqueue.enqueue_latest_source(patient_id="P1", reason="auth_success").job_id
        self.qa_service.fail = True
        worker = GeminiFilePrewarmService(
            settings=self.settings,
            repository=self.repository,
            qa_service=self.qa_service,  # type: ignore[arg-type]
            cleanup_service=None,
        )

        with self.assertRaises(GeminiFilePrewarmRetryableError):
            worker.process_job(job_id)
        stored = self.repository.get_prewarm_job(job_id)
        assert stored is not None
        self.assertEqual(stored.status, "FAILED")
        self.assertTrue(stored.runnable)
        self.assertEqual(stored.retry_count, 1)


if __name__ == "__main__":
    unittest.main()
