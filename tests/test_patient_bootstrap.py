from __future__ import annotations

import sys
import types
import unittest

# These tests use a fake Drive gateway and do not call Google SDKs.
if "google.auth" not in sys.modules:
    google_mod = types.ModuleType("google")
    google_mod.__path__ = []

    auth_mod = types.ModuleType("google.auth")
    auth_mod.default = lambda *args, **kwargs: (object(), None)

    oauth2_mod = types.ModuleType("google.oauth2")
    service_account_mod = types.ModuleType("google.oauth2.service_account")

    class _DummyCredentials:
        @classmethod
        def from_service_account_file(cls, *args, **kwargs):
            return cls()

    service_account_mod.Credentials = _DummyCredentials
    oauth2_mod.service_account = service_account_mod

    googleapiclient_mod = types.ModuleType("googleapiclient")
    discovery_mod = types.ModuleType("googleapiclient.discovery")
    errors_mod = types.ModuleType("googleapiclient.errors")
    http_mod = types.ModuleType("googleapiclient.http")

    discovery_mod.build = lambda *args, **kwargs: object()

    class _DummyHttpError(Exception):
        pass

    class _DummyMediaIoBaseDownload:
        def __init__(self, *args, **kwargs):
            pass

    errors_mod.HttpError = _DummyHttpError
    http_mod.MediaIoBaseDownload = _DummyMediaIoBaseDownload

    google_mod.auth = auth_mod
    google_mod.oauth2 = oauth2_mod
    sys.modules["google"] = google_mod
    sys.modules["google.auth"] = auth_mod
    sys.modules["google.oauth2"] = oauth2_mod
    sys.modules["google.oauth2.service_account"] = service_account_mod
    sys.modules["googleapiclient"] = googleapiclient_mod
    sys.modules["googleapiclient.discovery"] = discovery_mod
    sys.modules["googleapiclient.errors"] = errors_mod
    sys.modules["googleapiclient.http"] = http_mod

from app.config import Settings
from app.repositories import InMemoryMedicalRepository
from app.services.drive_bootstrap import PatientDriveBootstrapService


FOLDER_MIME = "application/vnd.google-apps.folder"


class FakeDriveGateway:
    def __init__(self) -> None:
        self.folders = {
            "root-1": {
                "id": "root-1",
                "name": "medical-chatbot-local",
                "mimeType": FOLDER_MIME,
                "parents": [],
                "trashed": False,
            },
            "patients-1": {
                "id": "patients-1",
                "name": "patients",
                "mimeType": FOLDER_MIME,
                "parents": ["root-1"],
                "trashed": False,
            },
            "folder-test": {
                "id": "folder-test",
                "name": "테스트_19900101",
                "mimeType": FOLDER_MIME,
                "parents": ["patients-1"],
                "trashed": False,
            },
            "folder-hong": {
                "id": "folder-hong",
                "name": "홍길동_19890515",
                "mimeType": FOLDER_MIME,
                "parents": ["patients-1"],
                "trashed": False,
            },
            "folder-invalid": {
                "id": "folder-invalid",
                "name": "not-a-patient-folder",
                "mimeType": FOLDER_MIME,
                "parents": ["patients-1"],
                "trashed": False,
            },
        }

    def get_file(self, file_id: str) -> dict:
        return dict(self.folders[file_id])

    def find_folders_by_name(self, folder_name: str, *, parent_id: str | None = None) -> list[dict]:
        matches = []
        for folder in self.folders.values():
            if folder["name"] != folder_name:
                continue
            if parent_id and parent_id not in folder.get("parents", []):
                continue
            matches.append(dict(folder))
        return matches

    def list_child_folders(self, folder_id: str) -> list[dict]:
        return [
            dict(folder)
            for folder in self.folders.values()
            if folder_id in folder.get("parents", []) and folder.get("mimeType") == FOLDER_MIME
        ]


class PatientDriveBootstrapServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            google_drive_root_folder_name="medical-chatbot-local",
            patients_folder_name="patients",
            medical_wiki_extraction_mode="metadata",
            gemini_api_key="test-key",
        )
        self.repository = InMemoryMedicalRepository()
        self.drive_gateway = FakeDriveGateway()
        self.service = PatientDriveBootstrapService(
            settings=self.settings,
            repository=self.repository,
            drive_gateway=self.drive_gateway,  # type: ignore[arg-type]
        )

    def test_bootstrap_creates_patients_and_folder_index_from_drive_folders(self) -> None:
        result = self.service.bootstrap()

        self.assertTrue(result.ok)
        self.assertEqual(result.root_folder_id, "root-1")
        self.assertEqual(result.patients_folder_id, "patients-1")
        self.assertEqual(result.scanned_count, 3)
        self.assertEqual(result.created_count, 2)
        self.assertEqual(result.invalid_count, 1)

        patients = self.repository.list_active_patients()
        self.assertEqual(len(patients), 2)
        names = {patient.name for patient in patients}
        self.assertEqual(names, {"테스트", "홍길동"})

        test_patient = next(patient for patient in patients if patient.name == "테스트")
        self.assertEqual(test_patient.birth, "19900101")
        self.assertEqual(test_patient.drive_folder_id, "folder-test")
        self.assertEqual(test_patient.drive_folder_name, "테스트_19900101")
        folder_index = self.repository.get_drive_folder_index("folder-test")
        self.assertIsNotNone(folder_index)
        assert folder_index is not None
        self.assertEqual(folder_index.patient_id, test_patient.patient_id)

    def test_bootstrap_is_idempotent_for_unchanged_folders(self) -> None:
        first = self.service.bootstrap()
        second = self.service.bootstrap()

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(second.created_count, 0)
        self.assertEqual(second.updated_count, 0)
        self.assertGreaterEqual(second.skipped_count, 2)
        self.assertEqual(len(self.repository.list_active_patients()), 2)

    def test_bootstrap_can_resolve_root_by_id(self) -> None:
        settings = Settings(
            google_drive_root_folder_id="root-1",
            google_drive_root_folder_name="duplicate-name-is-ignored",
            patients_folder_name="patients",
            medical_wiki_extraction_mode="metadata",
            gemini_api_key="test-key",
        )
        service = PatientDriveBootstrapService(
            settings=settings,
            repository=InMemoryMedicalRepository(),
            drive_gateway=self.drive_gateway,  # type: ignore[arg-type]
        )

        result = service.bootstrap()

        self.assertTrue(result.ok)
        self.assertEqual(result.root_folder_id, "root-1")


if __name__ == "__main__":
    unittest.main()
