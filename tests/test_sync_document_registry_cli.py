from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from app.config import Settings
from app.services.document_sync import DocumentRegistrySyncService
from app.services.drive import DriveLookupService
from app.tools import sync_document_registry
from tests.test_document_sync import FakeGeminiStoreSyncService
from tests.test_drive_service import FakeDriveGateway


class SyncDocumentRegistryCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            google_service_account_path="credentials/google-service-account.json",
            gemini_api_key="test-key",
        )
        self.gateway = FakeDriveGateway()
        self.drive_service = DriveLookupService(
            settings=self.settings,
            gateway=self.gateway,
        )
        self.gemini_store_sync_service = FakeGeminiStoreSyncService()
        self.sync_service = DocumentRegistrySyncService(
            settings=self.settings,
            drive_service=self.drive_service,
            gemini_store_sync_service=self.gemini_store_sync_service,
        )

    def test_cli_dry_run_does_not_persist(self) -> None:
        stdout = io.StringIO()
        with patch.object(
            sync_document_registry,
            "get_settings",
            return_value=self.settings,
        ), patch.object(
            sync_document_registry,
            "build_default_drive_service",
            return_value=self.drive_service,
        ), patch.object(
            sync_document_registry,
            "build_default_gemini_patient_store_sync_service",
            return_value=self.gemini_store_sync_service,
        ), patch.object(
            sync_document_registry,
            "DocumentRegistrySyncService",
            return_value=self.sync_service,
        ), redirect_stdout(stdout):
            exit_code = sync_document_registry.main(["--json"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["applied"])
        self.assertNotIn("created-document_registry.json", self.gateway.updated_files)

    def test_cli_apply_persists_document_registry(self) -> None:
        stdout = io.StringIO()
        with patch.object(
            sync_document_registry,
            "get_settings",
            return_value=self.settings,
        ), patch.object(
            sync_document_registry,
            "build_default_drive_service",
            return_value=self.drive_service,
        ), patch.object(
            sync_document_registry,
            "build_default_gemini_patient_store_sync_service",
            return_value=self.gemini_store_sync_service,
        ), patch.object(
            sync_document_registry,
            "DocumentRegistrySyncService",
            return_value=self.sync_service,
        ), redirect_stdout(stdout):
            exit_code = sync_document_registry.main(["--apply", "--json"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["applied"])
        self.assertIn("document-registry", self.gateway.updated_files)


if __name__ == "__main__":
    unittest.main()
