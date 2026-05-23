from __future__ import annotations

import unittest

from app.config import Settings
from app.repositories import InMemoryMedicalRepository
from app.schemas import PatientProfile
from app.services.cloud_tasks import CloudTaskEnqueueResult
from app.services.drive import DriveGateway
from app.services.drive_sync import DriveChangesSyncService
from app.services.medical_wiki import MedicalWikiService


class FakeDriveGateway(DriveGateway):
    def __init__(self) -> None:
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
        return "token-start"

    def list_changes(self, page_token: str, *, page_size: int) -> dict:
        return {"changes": [], "newStartPageToken": "token-new"}


class FakeWikiRebuildTaskEnqueueService:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def enqueue_wiki_rebuild(self, *, patient_id: str, source_id: str, reason: str = "drive_sync"):
        self.calls.append({"patient_id": patient_id, "source_id": source_id, "reason": reason})
        return CloudTaskEnqueueResult(task_name=f"tasks/{source_id}", duplicate=False)


class DriveSyncWikiTasksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            _env_file=None,
            google_service_account_path="credentials/google-service-account.json",
            gemini_api_key="test-key",
            medical_wiki_extraction_mode="metadata",
            wiki_rebuild_worker_mode="cloud_tasks",
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
        self.wiki_service = MedicalWikiService(repository=self.repository, settings=self.settings)
        self.enqueue_service = FakeWikiRebuildTaskEnqueueService()
        self.sync_service = DriveChangesSyncService(
            settings=self.settings,
            repository=self.repository,
            drive_gateway=self.drive_gateway,
            wiki_service=self.wiki_service,
            wiki_rebuild_task_enqueue_service=self.enqueue_service,  # type: ignore[arg-type]
        )

    def test_sync_patient_enqueues_wiki_rebuild_in_cloud_tasks_mode(self) -> None:
        result = self.sync_service.sync_patient("P0001")

        self.assertTrue(result.ok)
        self.assertEqual(result.deferred_count, 1)
        self.assertEqual(len(self.enqueue_service.calls), 1)
        source = self.repository.list_medical_sources("P0001")[0]
        runtime = self.repository.get_source_runtime("P0001", source.source_id)
        assert runtime is not None
        self.assertEqual(runtime.sync.status, "WIKI_PROCESSING")
        self.assertEqual(runtime.wiki_sync.status, "WIKI_PROCESSING")
        self.assertIsNone(self.repository.get_wiki_page_by_source("P0001", source.source_id))


if __name__ == "__main__":
    unittest.main()
