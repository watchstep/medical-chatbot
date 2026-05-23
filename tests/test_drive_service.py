from __future__ import annotations

import unittest
from unittest.mock import sentinel, patch

from app.config import Settings
from app.services.drive import (
    GoogleDriveGateway,
    DriveLookupService,
    build_default_drive_service,
)


class FakeDriveGateway:
    def list_files_in_folder(self, folder_id: str) -> list[dict]:
        return []

    def download_file_bytes(self, file_id: str) -> bytes:
        return b""

    def get_file(self, file_id: str) -> dict:
        return {}

    def get_start_page_token(self) -> str:
        return "token"

    def list_changes(self, page_token: str, *, page_size: int) -> dict:
        return {"changes": [], "newStartPageToken": "token-2"}


class FakeExecuteRequest:
    def __init__(self, response: dict):
        self.response = response

    def execute(self) -> dict:
        return self.response


class FakeChangesResource:
    def __init__(self) -> None:
        self.list_kwargs: dict | None = None

    def list(self, **kwargs: object) -> FakeExecuteRequest:
        self.list_kwargs = kwargs
        return FakeExecuteRequest({"changes": [], "newStartPageToken": "token-2"})


class FakeGoogleDriveService:
    def __init__(self) -> None:
        self.changes_resource = FakeChangesResource()

    def changes(self) -> FakeChangesResource:
        return self.changes_resource


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

    def test_list_changes_includes_removed_items(self) -> None:
        service = FakeGoogleDriveService()
        gateway = GoogleDriveGateway.__new__(GoogleDriveGateway)
        gateway.service = service

        response = gateway.list_changes("token-old", page_size=20)

        self.assertEqual(response["newStartPageToken"], "token-2")
        assert service.changes_resource.list_kwargs is not None
        self.assertTrue(service.changes_resource.list_kwargs["includeRemoved"])
        self.assertTrue(service.changes_resource.list_kwargs["includeItemsFromAllDrives"])
        self.assertTrue(service.changes_resource.list_kwargs["supportsAllDrives"])

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


if __name__ == "__main__":
    unittest.main()
