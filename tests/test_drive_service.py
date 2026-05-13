from __future__ import annotations

import unittest
from unittest.mock import sentinel, patch

from app.config import Settings
from app.schemas import DriveFile
from app.services.drive import (
    GoogleDriveGateway,
    DriveLookupError,
    DriveLookupService,
    build_default_drive_service,
)


class FakeDriveGateway:
    def __init__(self):
        self.list_map = {
            "name = 'medical-chatbot' and mimeType = 'application/vnd.google-apps.folder' and trashed = false": [
                {"id": "root", "name": "medical-chatbot", "mimeType": "application/vnd.google-apps.folder"}
            ],
            "'root' in parents and name = '_system' and mimeType = 'application/vnd.google-apps.folder' and trashed = false": [
                {"id": "system", "name": "_system", "mimeType": "application/vnd.google-apps.folder"}
            ],
            "'system' in parents and name = 'patient_index.json' and trashed = false": [
                {"id": "patient-index", "name": "patient_index.json", "mimeType": "application/json"}
            ],
            "'system' in parents and name = 'document_registry.json' and trashed = false": [
                {"id": "document-registry", "name": "document_registry.json", "mimeType": "application/json"}
            ],
            "'root' in parents and name = 'patients' and mimeType = 'application/vnd.google-apps.folder' and trashed = false": [
                {"id": "patients-folder", "name": "patients", "mimeType": "application/vnd.google-apps.folder"}
            ],
            "'patients-folder' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false": [
                {"id": "patient-folder", "name": "P0001_손창선_19461230", "mimeType": "application/vnd.google-apps.folder"},
                {"id": "patient-folder-2", "name": "P0002_홍길동_19800515", "mimeType": "application/vnd.google-apps.folder"},
                {"id": "patient-folder-3", "name": "P0003_김영희_19770101", "mimeType": "application/vnd.google-apps.folder"},
                {"id": "bad-folder", "name": "잘못된폴더명", "mimeType": "application/vnd.google-apps.folder"},
            ],
            "'patients-folder' in parents and name = 'P0001_손창선_19461230' and mimeType = 'application/vnd.google-apps.folder' and trashed = false": [
                {"id": "patient-folder", "name": "P0001_손창선_19461230", "mimeType": "application/vnd.google-apps.folder"}
            ],
            "'patients-folder' in parents and name = 'P0002_홍길동_19800515' and mimeType = 'application/vnd.google-apps.folder' and trashed = false": [
                {"id": "patient-folder-2", "name": "P0002_홍길동_19800515", "mimeType": "application/vnd.google-apps.folder"}
            ],
            "'patient-folder' in parents and trashed = false": [
                {"id": "result-old", "name": "result_20260420.pdf", "mimeType": "application/pdf", "modifiedTime": "2026-04-20T10:00:00Z"},
                {"id": "result-new", "name": "result_20260421.pdf", "mimeType": "application/pdf", "modifiedTime": "2026-04-21T10:00:00Z"},
                {"id": "chart-old", "name": "chart_20260419.pdf", "mimeType": "application/pdf", "modifiedTime": "2026-04-19T10:00:00Z"},
                {"id": "chart-new", "name": "chart_20260421.pdf", "mimeType": "application/pdf", "modifiedTime": "2026-04-21T11:00:00Z"},
                {"id": "image-file", "name": "image_20260421.pdf", "mimeType": "application/pdf", "modifiedTime": "2026-04-21T12:00:00Z"},
                {"id": "ignored-file", "name": "result_latest.pdf", "mimeType": "application/pdf", "modifiedTime": "2026-04-21T13:00:00Z"},
            ],
            "'patient-folder-2' in parents and trashed = false": [
                {"id": "result-2", "name": "result_20260410.pdf", "mimeType": "application/pdf", "modifiedTime": "2026-04-10T09:00:00Z"},
            ],
        }
        self.file_map = {
            "patient-index": b"""
            {
              "patients": [
                {
                  "patient_id": "P0001",
                  "name": "\354\206\220\354\260\275\354\204\240",
                  "birth": "19461230",
                  "folder_name": "P0001_\354\206\220\354\260\275\354\204\240_19461230",
                  "kakao_user_ids": ["kakao-user-id-1"]
                },
                {
                  "patient_id": "P0002",
                  "name": "\355\231\215\352\270\270\353\217\231",
                  "birth": "19800515",
                  "folder_name": "P0002_\355\231\215\352\270\270\353\217\231_19800515",
                  "kakao_user_ids": ["mapped-user-2"]
                }
              ]
            }
            """,
            "document-registry": b"""
            {
              "generated_at": "2026-04-28T12:10:00+09:00",
              "documents": [
                {
                  "patient_id": "P0001",
                  "filename": "result_20260421.pdf",
                  "document_type": "result",
                  "document_date": "20260421",
                  "drive_file_id": "result-new",
                  "drive_modified_time": "2026-04-21T10:00:00Z",
                  "file_hash": "result-hash",
                  "file_search_store_name": "fileSearchStores/patient-P0001",
                  "file_search_document_name": "fileSearchStores/patient-P0001/documents/result-new",
                  "sync_status": "READY",
                  "synced_at": "2026-04-28T12:10:00+09:00"
                },
                {
                  "patient_id": "P0001",
                  "filename": "chart_20260421.pdf",
                  "document_type": "chart",
                  "document_date": "20260421",
                  "drive_file_id": "chart-new",
                  "drive_modified_time": "2026-04-21T11:00:00Z",
                  "file_hash": "chart-hash",
                  "file_search_store_name": "fileSearchStores/patient-P0001",
                  "file_search_document_name": "fileSearchStores/patient-P0001/documents/chart-new",
                  "sync_status": "READY",
                  "synced_at": "2026-04-28T12:10:00+09:00"
                }
              ]
            }
            """,
            "patient-folder": {
                "id": "patient-folder",
                "name": "P0001_\354\206\220\354\260\275\354\204\240_19461230",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": ["patients-folder"]
            },
            "result-old": b"result old pdf bytes",
            "result-new": b"result pdf bytes",
            "chart-old": b"chart old pdf bytes",
            "chart-new": b"chart pdf bytes",
            "image-file": b"image pdf bytes",
            "result-2": b"result 2 pdf bytes",
            "patient-folder-2": {
                "id": "patient-folder-2",
                "name": "P0002_\355\231\215\352\270\270\353\217\231_19800515",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": ["patients-folder"]
            },
            "patient-folder-3": {
                "id": "patient-folder-3",
                "name": "P0003_\352\271\200\354\230\201\355\235\254_19770101",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": ["patients-folder"]
            },
        }
        self.updated_files: dict[str, bytes] = {}

    def list_files(self, query: str) -> list[dict]:
        return self.list_map.get(query, [])

    def download_file_bytes(self, file_id: str) -> bytes:
        return self.file_map[file_id]

    def get_file(self, file_id: str) -> dict:
        return self.file_map[file_id]

    def update_file_bytes(self, file_id: str, content: bytes, mime_type: str) -> None:
        self.updated_files[file_id] = content
        self.file_map[file_id] = content

    def create_file_bytes(self, parent_id: str, file_name: str, content: bytes, mime_type: str) -> str:
        created_id = f"created-{file_name}"
        self.updated_files[created_id] = content
        self.file_map[created_id] = content
        self.list_map[f"'{parent_id}' in parents and name = '{file_name}' and trashed = false"] = [
            {"id": created_id, "name": file_name, "mimeType": mime_type}
        ]
        return created_id


class LegacyFakeDriveGateway(FakeDriveGateway):
    def __init__(self):
        super().__init__()
        self.file_map["patient-index"] = b"""
        {
          "patients": [
            {
              "patient_id": "P0001",
              "name": "\354\206\220\354\260\275\354\204\240",
              "birth": "19461230",
              "phone_last4": "9706",
              "folder_id": "patient-folder"
            }
          ]
        }
        """


class GoogleDriveGatewayAuthTest(unittest.TestCase):
    def test_uses_adc_when_credentials_path_is_not_configured(self) -> None:
        with patch(
            "app.services.drive.google.auth.default",
            return_value=(sentinel.credentials, sentinel.project_id),
        ) as default_auth, patch(
            "app.services.drive.build",
            return_value=sentinel.service,
        ) as build:
            gateway = GoogleDriveGateway()

        default_auth.assert_called_once_with(
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        build.assert_called_once_with(
            "drive",
            "v3",
            credentials=sentinel.credentials,
            cache_discovery=False,
        )
        self.assertIs(gateway.service, sentinel.service)

    def test_uses_service_account_file_when_credentials_path_is_configured(self) -> None:
        with patch(
            "app.services.drive.ServiceAccountCredentials.from_service_account_file",
            return_value=sentinel.credentials,
        ) as from_service_account_file, patch(
            "app.services.drive.build",
            return_value=sentinel.service,
        ) as build:
            gateway = GoogleDriveGateway("credentials/google-service-account.json")

        from_service_account_file.assert_called_once_with(
            "credentials/google-service-account.json",
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        build.assert_called_once_with(
            "drive",
            "v3",
            credentials=sentinel.credentials,
            cache_discovery=False,
        )
        self.assertIs(gateway.service, sentinel.service)


class BuildDefaultDriveServiceTest(unittest.TestCase):
    def test_empty_credentials_path_uses_adc(self) -> None:
        settings = Settings(google_service_account_path="")

        with patch(
            "app.services.drive.GoogleDriveGateway",
            return_value=FakeDriveGateway(),
        ) as gateway_class:
            service = build_default_drive_service(settings)

        gateway_class.assert_called_once_with(None)
        self.assertIsInstance(service, DriveLookupService)

    def test_uses_adc_when_credentials_path_is_not_configured(self) -> None:
        settings = Settings()

        with patch(
            "app.services.drive.GoogleDriveGateway",
            return_value=FakeDriveGateway(),
        ) as gateway_class:
            service = build_default_drive_service(settings)

        gateway_class.assert_called_once_with(None)
        self.assertIsInstance(service, DriveLookupService)

    def test_uses_service_account_file_when_credentials_path_is_configured(self) -> None:
        settings = Settings(
            google_service_account_path="credentials/google-service-account.json",
        )

        with patch(
            "app.services.drive.GoogleDriveGateway",
            return_value=FakeDriveGateway(),
        ) as gateway_class:
            service = build_default_drive_service(settings)

        gateway_class.assert_called_once_with("credentials/google-service-account.json")
        self.assertIsInstance(service, DriveLookupService)


class DriveLookupServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        settings = Settings(
            google_service_account_path="credentials/google-service-account.json",
        )
        self.service = DriveLookupService(settings=settings, gateway=FakeDriveGateway())

    def test_load_patient_index(self) -> None:
        patient_index = self.service.load_patient_index()

        self.assertEqual(len(patient_index.patients), 2)
        first_patient = next(
            item for item in patient_index.patients if item.patient_id == "P0001"
        )
        self.assertEqual(first_patient.folder_name, "P0001_손창선_19461230")
        self.assertIn("kakao-user-id-1", first_patient.kakao_user_ids)
        self.assertEqual(first_patient.phone_last4, "")

    def test_download_drive_file_bytes_returns_selected_pdf_content(self) -> None:
        drive_file = DriveFile(
            file_id="result-new",
            name="result_20260421.pdf",
            mime_type="application/pdf",
            date="20260421",
            type="result",
        )

        content = self.service.download_drive_file_bytes(drive_file)

        self.assertEqual(content, self.service.gateway.file_map["result-new"])

    def test_load_legacy_patient_index(self) -> None:
        settings = Settings(
            google_service_account_path="credentials/google-service-account.json",
        )
        service = DriveLookupService(settings=settings, gateway=LegacyFakeDriveGateway())

        patient_index = service.load_patient_index()

        self.assertEqual(len(patient_index.patients), 1)
        self.assertEqual(patient_index.patients[0].folder_id, "patient-folder")
        self.assertEqual(patient_index.patients[0].folder_name, "P0001_손창선_19461230")
        self.assertEqual(patient_index.patients[0].kakao_user_ids, [])

    def test_get_patient_by_kakao_user_id(self) -> None:
        patient = self.service.get_patient_by_kakao_user_id("kakao-user-id-1")

        self.assertIsNotNone(patient)
        self.assertEqual(patient.patient_id, "P0001")

    def test_sync_patient_index_adds_missing_drive_patient_and_skips_invalid_folder(self) -> None:
        result = self.service.sync_patient_index()

        self.assertEqual(
            [patient.patient_id for patient in result.added],
            ["P0003"],
        )
        added_patient = result.added[0]
        self.assertEqual(added_patient.folder_id, "patient-folder-3")
        self.assertEqual(added_patient.kakao_user_ids, [])
        self.assertEqual(added_patient.phone_last4, "")
        self.assertEqual(
            [item.folder_name for item in result.skipped],
            ["잘못된폴더명"],
        )
        reconciled_patient = next(
            item for item in result.patient_index.patients if item.patient_id == "P0001"
        )
        self.assertIn("kakao-user-id-1", reconciled_patient.kakao_user_ids)

    def test_sync_patient_index_reports_existing_index_missing_in_drive_without_deleting(self) -> None:
        self.service.gateway.file_map["patient-index"] = b"""
        {
          "patients": [
            {
              "patient_id": "P9999",
              "name": "\353\210\204\353\235\275\355\231\230",
              "birth": "19990101",
              "folder_name": "P9999_\353\210\204\353\235\275\355\231\230_19990101",
              "kakao_user_ids": ["legacy-user"]
            }
          ]
        }
        """

        result = self.service.sync_patient_index()

        self.assertEqual(
            [patient.patient_id for patient in result.missing_in_drive],
            ["P9999"],
        )
        self.assertEqual(
            [patient.patient_id for patient in result.patient_index.patients],
            ["P9999", "P0001", "P0002", "P0003"],
        )

    def test_sync_patient_index_skips_duplicate_patient_ids(self) -> None:
        self.service.gateway.list_map[
            "'patients-folder' in parents and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        ].append(
            {
                "id": "patient-folder-duplicate",
                "name": "P0003_김영희_19770101",
                "mimeType": "application/vnd.google-apps.folder",
            }
        )

        result = self.service.sync_patient_index()

        self.assertEqual(result.added, [])
        self.assertEqual(
            sorted(item.reason for item in result.skipped if item.folder_name == "P0003_김영희_19770101"),
            ["duplicate_patient_id", "duplicate_patient_id"],
        )

    def test_authenticate_and_map_patient_saves_kakao_user_id(self) -> None:
        patient = self.service.authenticate_and_map_patient(
            kakao_user_id="new-kakao-user",
            name="손창선",
            birth="19461230",
        )

        self.assertEqual(patient.patient_id, "P0001")
        updated_index = self.service.load_patient_index()
        reloaded_patient = next(
            item for item in updated_index.patients if item.patient_id == "P0001"
        )
        self.assertIn("new-kakao-user", reloaded_patient.kakao_user_ids)
        self.assertIn("patient-index", self.service.gateway.updated_files)

    def test_authenticate_and_map_patient_rejects_existing_other_patient_mapping(self) -> None:
        with self.assertRaises(DriveLookupError):
            self.service.authenticate_and_map_patient(
                kakao_user_id="mapped-user-2",
                name="손창선",
                birth="19461230",
            )

    def test_debug_probe_supports_identity_lookup_without_kakao_user_id(self) -> None:
        result = self.service.debug_probe(name="손창선", birth="19461230")

        self.assertTrue(result["ok"])
        self.assertTrue(result["identity_found"])
        self.assertEqual(result["authenticated_patient_id"], "P0001")
        self.assertEqual(result["latest_visit_date"], "20260421")
        self.assertEqual(result["latest_result"], "result_20260421.pdf")
        self.assertEqual(result["latest_chart"], "chart_20260421.pdf")


if __name__ == "__main__":
    unittest.main()
