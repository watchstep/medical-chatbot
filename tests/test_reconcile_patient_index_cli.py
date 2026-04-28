from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from app.config import Settings
from app.services.drive import DriveLookupService
from app.tools import reconcile_patient_index
from tests.test_drive_service import FakeDriveGateway


class ReconcilePatientIndexCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            google_service_account_path="credentials/google-service-account.json",
        )
        self.gateway = FakeDriveGateway()
        self.drive_service = DriveLookupService(
            settings=self.settings,
            gateway=self.gateway,
        )

    def test_cli_dry_run_does_not_persist(self) -> None:
        stdout = io.StringIO()
        with patch.object(
            reconcile_patient_index,
            "get_settings",
            return_value=self.settings,
        ), patch.object(
            reconcile_patient_index,
            "build_default_drive_service",
            return_value=self.drive_service,
        ), redirect_stdout(stdout):
            exit_code = reconcile_patient_index.main(["--json"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["applied"])
        self.assertTrue(payload["changed"])
        self.assertEqual(len(payload["added"]), 1)
        self.assertNotIn("patient-index", self.gateway.updated_files)

    def test_cli_apply_persists_reconciled_index(self) -> None:
        stdout = io.StringIO()
        with patch.object(
            reconcile_patient_index,
            "get_settings",
            return_value=self.settings,
        ), patch.object(
            reconcile_patient_index,
            "build_default_drive_service",
            return_value=self.drive_service,
        ), redirect_stdout(stdout):
            exit_code = reconcile_patient_index.main(["--apply", "--json"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["applied"])
        self.assertIn("patient-index", self.gateway.updated_files)
        persisted = json.loads(self.gateway.updated_files["patient-index"].decode("utf-8"))
        self.assertEqual(len(persisted["patients"]), 3)
        self.assertEqual(persisted["patients"][-1]["patient_id"], "P0003")
        self.assertEqual(persisted["patients"][-1]["phone_last4"], "")


if __name__ == "__main__":
    unittest.main()
