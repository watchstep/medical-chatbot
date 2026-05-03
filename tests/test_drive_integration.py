from __future__ import annotations

import os
import unittest

from app.config import Settings
from app.services.drive import build_default_drive_service


@unittest.skipUnless(
    os.getenv("RUN_DRIVE_INTEGRATION_TEST") == "1",
    "Set RUN_DRIVE_INTEGRATION_TEST=1 to run real Google Drive integration checks.",
)
class GoogleDriveIntegrationTest(unittest.TestCase):
    def test_can_load_patient_index_from_real_google_drive(self) -> None:
        settings = Settings()
        service = build_default_drive_service(settings)

        patient_index = service.load_patient_index()

        self.assertGreater(len(patient_index.patients), 0)

    def test_can_load_document_registry_for_real_google_drive(self) -> None:
        target_kakao_user_id = os.getenv("TEST_KAKAO_USER_ID")
        target_name = os.getenv("TEST_PATIENT_NAME")
        target_birth = os.getenv("TEST_PATIENT_BIRTH")

        if not all([target_name, target_birth]):
            self.skipTest(
                "Set TEST_PATIENT_NAME and TEST_PATIENT_BIRTH "
                "to verify a real patient registry lookup."
            )

        settings = Settings()
        service = build_default_drive_service(settings)
        patient_index = service.load_patient_index()
        patient = next(
            (
                item
                for item in patient_index.patients
                if item.name == target_name and item.birth == target_birth
            ),
            None,
        )
        self.assertIsNotNone(patient)

        if target_kakao_user_id:
            mapped_patient = service.get_patient_by_kakao_user_id(target_kakao_user_id)
            self.assertIsNotNone(mapped_patient)
            self.assertEqual(mapped_patient.patient_id, patient.patient_id)

        registry = service.load_document_registry()
        ready_documents = [
            item
            for item in registry.documents
            if item.patient_id == patient.patient_id and item.sync_status == "READY"
        ]

        self.assertGreater(len(ready_documents), 0)
