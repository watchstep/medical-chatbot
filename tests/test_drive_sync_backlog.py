from __future__ import annotations

import unittest

from app.config import Settings
from app.repositories import InMemoryMedicalRepository
from app.schemas import (
    DriveSyncState,
    MedicalSource,
    MedicalSourceRuntime,
    PatientProfile,
    RuntimeSyncState,
    SourceRef,
)
from app.services.drive import DriveGateway, DriveLookupError
from app.services.drive_sync import GOOGLE_DRIVE_FOLDER_MIME_TYPE
from app.services.drive_sync import DriveChangesSyncService
from app.services.medical_wiki import MedicalWikiService
from app.services.medical_wiki_extractor import MedicalWikiExtractionError


class FakeDriveGateway(DriveGateway):
    def __init__(self) -> None:
        self.folders_by_id: dict[str, dict] = {
            "root-1": self.folder_info("root-1", "medical-chatbot", []),
            "patients-1": self.folder_info("patients-1", "patients", ["root-1"]),
            "folder-p1": self.folder_info("folder-p1", "손창선_19461230", ["patients-1"]),
        }
        self.files_by_folder: dict[str, list[dict]] = {
            "folder-p1": [
                self.file_info("drive-file-1", "검사결과_20260421.pdf", "hash-1"),
                self.file_info("drive-file-2", "처방전_20260422.pdf", "hash-2"),
            ]
        }
        self.changes_by_token: dict[str, dict] = {}
        self.start_page_token = "token-start"

    def folder_info(self, folder_id: str, name: str, parents: list[str]) -> dict:
        return {
            "id": folder_id,
            "name": name,
            "mimeType": GOOGLE_DRIVE_FOLDER_MIME_TYPE,
            "modifiedTime": "2026-04-21T10:00:00Z",
            "parents": parents,
            "trashed": False,
        }

    def file_info(self, file_id: str, name: str, md5: str, parents: list[str] | None = None) -> dict:
        return {
            "id": file_id,
            "name": name,
            "mimeType": "application/pdf",
            "modifiedTime": "2026-04-21T10:00:00Z",
            "parents": parents or ["folder-p1"],
            "size": "1234",
            "md5Checksum": md5,
        }

    def list_files_in_folder(self, folder_id: str) -> list[dict]:
        return [dict(item) for item in self.files_by_folder.get(folder_id, [])]

    def list_child_folders(self, folder_id: str) -> list[dict]:
        return [
            dict(folder)
            for folder in self.folders_by_id.values()
            if folder_id in (folder.get("parents") or []) and not folder.get("trashed")
        ]

    def find_folders_by_name(self, folder_name: str, *, parent_id: str | None = None) -> list[dict]:
        folders: list[dict] = []
        for folder in self.folders_by_id.values():
            if folder.get("name") != folder_name or folder.get("trashed"):
                continue
            if parent_id and parent_id not in (folder.get("parents") or []):
                continue
            folders.append(dict(folder))
        return folders

    def download_file_bytes(self, file_id: str) -> bytes:
        return b"%PDF fake"

    def get_file(self, file_id: str) -> dict:
        if file_id in self.folders_by_id:
            return dict(self.folders_by_id[file_id])
        for files in self.files_by_folder.values():
            for file_info in files:
                if file_info["id"] == file_id:
                    return dict(file_info)
        raise DriveLookupError(f"not found: {file_id}")

    def get_start_page_token(self) -> str:
        return self.start_page_token

    def list_changes(self, page_token: str, *, page_size: int) -> dict:
        response = self.changes_by_token.get(page_token)
        if isinstance(response, Exception):
            raise response
        return dict(response or {"changes": [], "newStartPageToken": "token-new"})


class FailingWikiService(MedicalWikiService):
    def rebuild_wiki_page(self, *, patient_id: str, source_id: str):  # type: ignore[override]
        raise MedicalWikiExtractionError("forced wiki extraction failure")


class DriveSyncBacklogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            _env_file=None,
            google_service_account_path="credentials/google-service-account.json",
            admin_sync_token="admin-token",
            gemini_api_key="test-key",
            medical_wiki_extraction_mode="metadata",
            drive_changes_scope_id="main",
            max_changes_per_run=10,
            max_changes_pages_per_run=5,
            max_wiki_page_generations_per_run=10,
            max_wiki_backlog_per_run=10,
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
        self.sync_service = DriveChangesSyncService(
            settings=self.settings,
            repository=self.repository,
            drive_gateway=self.drive_gateway,
            wiki_service=self.wiki_service,
        )

    def test_changes_pagination_processes_multiple_pages_and_saves_new_token(self) -> None:
        file1 = self.drive_gateway.file_info("drive-file-1", "검사결과_20260421.pdf", "hash-1")
        file2 = self.drive_gateway.file_info("drive-file-2", "처방전_20260422.pdf", "hash-2")
        self.repository.upsert_drive_sync_state(DriveSyncState(scope_id="main", saved_page_token="token-old"))
        self.drive_gateway.changes_by_token = {
            "token-old": {"changes": [{"fileId": "drive-file-1", "file": file1}], "nextPageToken": "token-page-2"},
            "token-page-2": {"changes": [{"fileId": "drive-file-2", "file": file2}], "newStartPageToken": "token-new"},
        }

        result = self.sync_service.sync_changes()

        self.assertTrue(result.ok)
        self.assertEqual(result.created_count, 2)
        state = self.repository.get_drive_sync_state("main")
        assert state is not None
        self.assertEqual(state.saved_page_token, "token-new")
        self.assertEqual(state.last_success_page_token, "token-new")
        self.assertEqual(len(self.repository.list_medical_sources("P0001")), 2)

    def test_changes_failure_keeps_saved_checkpoint(self) -> None:
        self.repository.upsert_drive_sync_state(DriveSyncState(scope_id="main", saved_page_token="token-old"))
        self.drive_gateway.changes_by_token = {"token-old": RuntimeError("boom")}

        result = self.sync_service.sync_changes()

        self.assertFalse(result.ok)
        state = self.repository.get_drive_sync_state("main")
        assert state is not None
        self.assertEqual(state.saved_page_token, "token-old")
        self.assertEqual(state.sync_status, "FAILED")
        self.assertIsNotNone(state.last_failure)

    def test_wiki_pending_backlog_is_processed_without_new_drive_change(self) -> None:
        source = MedicalSource(
            source_id="SRC_P0001_BACKLOG",
            patient_id="P0001",
            source_ref=SourceRef(
                drive_file_id="drive-file-1",
                drive_folder_id="folder-p1",
                original_filename="검사결과_20260421.pdf",
                mime_type="application/pdf",
                file_size_bytes=1234,
                file_hash="hash-1",
                drive_modified_at="2026-04-21T10:00:00Z",
            ),
            source_status="ACTIVE",
        )
        self.repository.upsert_medical_source(source)
        self.repository.upsert_source_runtime(
            MedicalSourceRuntime(
                source_id=source.source_id,
                patient_id="P0001",
                sync=RuntimeSyncState(status="WIKI_PENDING"),
                wiki_sync=RuntimeSyncState(status="WIKI_PENDING"),
            )
        )
        self.repository.upsert_drive_sync_state(DriveSyncState(scope_id="main", saved_page_token="token-old"))
        self.drive_gateway.changes_by_token = {"token-old": {"changes": [], "newStartPageToken": "token-new"}}

        result = self.sync_service.sync_changes()

        self.assertTrue(result.ok)
        runtime = self.repository.get_source_runtime("P0001", source.source_id)
        assert runtime is not None
        self.assertEqual(runtime.sync.status, "READY")
        self.assertEqual(runtime.wiki_sync.status, "READY")
        index = self.repository.get_wiki_index("P0001")
        assert index is not None
        self.assertEqual(len(index.pages), 1)

    def test_full_sync_respects_global_lock(self) -> None:
        self.repository.upsert_drive_sync_state(
            DriveSyncState(
                scope_id="main",
                saved_page_token="token-old",
                lock_owner="other-worker",
                lease_expires_at="2999-01-01T00:00:00+09:00",
            )
        )

        result = self.sync_service.sync_all()

        self.assertFalse(result.ok)
        self.assertEqual(result.message, "sync lock is held by another worker")

    def test_patient_sync_marks_missing_stale_source_deleted(self) -> None:
        self.sync_service.sync_patient("P0001")
        source = self.repository.list_medical_sources("P0001")[0]
        self.repository.upsert_medical_source(source.model_copy(update={"source_status": "STALE"}))
        self.drive_gateway.files_by_folder["folder-p1"] = [
            file_info
            for file_info in self.drive_gateway.files_by_folder["folder-p1"]
            if file_info["id"] != source.source_ref.drive_file_id
        ]

        result = self.sync_service.sync_patient("P0001")

        self.assertTrue(result.ok)
        self.assertEqual(result.deleted_count, 1)
        deleted_source = self.repository.get_medical_source("P0001", source.source_id)
        runtime = self.repository.get_source_runtime("P0001", source.source_id)
        file_index = self.repository.get_drive_file_index(source.source_ref.drive_file_id)
        assert deleted_source is not None
        assert runtime is not None
        assert file_index is not None
        self.assertEqual(deleted_source.source_status, "DELETED")
        self.assertEqual(runtime.sync.status, "EXPIRED")
        self.assertEqual(runtime.wiki_sync.status, "EXPIRED")
        self.assertEqual(file_index.status, "DELETED")

    def test_changes_new_patient_folder_creates_patient_sources_and_counts_files(self) -> None:
        folder = self.drive_gateway.folder_info("folder-p2", "홍길동_19800515", ["patients-1"])
        file_info = self.drive_gateway.file_info(
            "drive-file-p2-1",
            "새검사결과_20260501.pdf",
            "hash-p2-1",
            parents=["folder-p2"],
        )
        self.drive_gateway.folders_by_id["folder-p2"] = folder
        self.drive_gateway.files_by_folder["folder-p2"] = [file_info]
        self.repository.upsert_drive_sync_state(DriveSyncState(scope_id="main", saved_page_token="token-old"))
        self.drive_gateway.changes_by_token = {
            "token-old": {"changes": [{"fileId": "folder-p2", "file": folder}], "newStartPageToken": "token-new"}
        }

        result = self.sync_service.sync_changes()

        self.assertTrue(result.ok)
        self.assertEqual(result.created_count, 2)
        patient = self.repository.find_active_patient_by_identity(name="홍길동", birth="19800515")
        self.assertIsNotNone(patient)
        assert patient is not None
        folder_index = self.repository.get_drive_folder_index("folder-p2")
        self.assertIsNotNone(folder_index)
        self.assertEqual(len(self.repository.list_medical_sources(patient.patient_id)), 1)
        index = self.repository.get_wiki_index(patient.patient_id)
        assert index is not None
        self.assertEqual(len(index.pages), 1)

    def test_changes_removed_patient_folder_deletes_patient_and_sources(self) -> None:
        self.sync_service.sync_patient("P0001")
        source = self.repository.list_medical_sources("P0001")[0]
        self.repository.upsert_drive_sync_state(DriveSyncState(scope_id="main", saved_page_token="token-old"))
        self.drive_gateway.changes_by_token = {
            "token-old": {"changes": [{"fileId": "folder-p1", "removed": True}], "newStartPageToken": "token-new"}
        }

        result = self.sync_service.sync_changes()

        self.assertTrue(result.ok)
        self.assertEqual(result.deleted_count, 1)
        patient = self.repository.get_patient("P0001")
        folder_index = self.repository.get_drive_folder_index("folder-p1")
        deleted_source = self.repository.get_medical_source("P0001", source.source_id)
        assert patient is not None
        assert folder_index is not None
        assert deleted_source is not None
        self.assertEqual(patient.status, "inactive")
        self.assertEqual(folder_index.status, "deleted")
        self.assertEqual(deleted_source.source_status, "DELETED")

    def test_removed_change_records_source_deleted_log(self) -> None:
        self.sync_service.sync_patient("P0001")
        source = self.repository.list_medical_sources("P0001")[0]
        self.repository.upsert_drive_sync_state(DriveSyncState(scope_id="main", saved_page_token="token-old"))
        self.drive_gateway.changes_by_token = {
            "token-old": {"changes": [{"fileId": source.source_ref.drive_file_id, "removed": True}], "newStartPageToken": "token-new"}
        }

        result = self.sync_service.sync_changes()

        self.assertTrue(result.ok)
        logs = [payload for (_, _), payload in self.repository.wiki_logs.items()]
        self.assertTrue(any(log.get("event_type") == "source_deleted" for log in logs))

    def test_page_failed_is_recorded_as_source_level_failure(self) -> None:
        failing_service = DriveChangesSyncService(
            settings=self.settings,
            repository=self.repository,
            drive_gateway=self.drive_gateway,
            wiki_service=FailingWikiService(repository=self.repository, settings=self.settings),
        )

        result = failing_service.sync_patient("P0001")

        self.assertTrue(result.ok)
        self.assertEqual(result.failed_count, 2)
        logs = [payload for (_, _), payload in self.repository.wiki_logs.items()]
        self.assertTrue(any(log.get("event_type") == "page_failed" for log in logs))


if __name__ == "__main__":
    unittest.main()
