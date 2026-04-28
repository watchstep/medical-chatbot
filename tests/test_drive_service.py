from __future__ import annotations

import unittest

from app.config import Settings
from app.services.drive import DriveLookupError, DriveLookupService


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
            "'root' in parents and name = 'patients' and mimeType = 'application/vnd.google-apps.folder' and trashed = false": [
                {"id": "patients-folder", "name": "patients", "mimeType": "application/vnd.google-apps.folder"}
            ],
            "'patients-folder' in parents and name = 'P0001_손창선_19461230' and mimeType = 'application/vnd.google-apps.folder' and trashed = false": [
                {"id": "patient-folder", "name": "P0001_손창선_19461230", "mimeType": "application/vnd.google-apps.folder"}
            ],
            "'patients-folder' in parents and name = 'P0002_홍길동_19800515' and mimeType = 'application/vnd.google-apps.folder' and trashed = false": [
                {"id": "patient-folder-2", "name": "P0002_홍길동_19800515", "mimeType": "application/vnd.google-apps.folder"}
            ],
            "'patient-folder' in parents and name = 'meta.json' and trashed = false": [
                {"id": "meta-file", "name": "meta.json", "mimeType": "application/json"}
            ],
            "'patient-folder-2' in parents and name = 'meta.json' and trashed = false": [
                {"id": "meta-file-2", "name": "meta.json", "mimeType": "application/json"}
            ],
            "'patient-folder' in parents and trashed = false": [
                {"id": "meta-file", "name": "meta.json", "mimeType": "application/json"},
                {"id": "result-old", "name": "result_20260420.pdf", "mimeType": "application/pdf"},
                {"id": "result-new", "name": "result_20260421.pdf", "mimeType": "application/pdf"},
                {"id": "chart-old", "name": "chart_20260419.pdf", "mimeType": "application/pdf"},
                {"id": "chart-new", "name": "chart_20260421.pdf", "mimeType": "application/pdf"},
                {"id": "image-file", "name": "image_20260421.pdf", "mimeType": "application/pdf"},
                {"id": "ignored-file", "name": "result_latest.pdf", "mimeType": "application/pdf"},
            ],
            "'patient-folder-2' in parents and trashed = false": [
                {"id": "meta-file-2", "name": "meta.json", "mimeType": "application/json"},
                {"id": "result-2", "name": "result_20260410.pdf", "mimeType": "application/pdf"},
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
            "meta-file": b"""
            {
              "patient_id": "P0001",
              "name": "\354\206\220\354\260\275\354\204\240",
              "birth": "19461230",
              "latest_visit_date": "20260421",
              "files": [
                {
                  "type": "result",
                  "date": "20260421",
                  "filename": "result_20260421.pdf",
                  "description": "\352\262\200\354\202\254\352\262\260\352\263\274\354\247\200"
                },
                {
                  "type": "chart",
                  "date": "20260421",
                  "filename": "chart_20260421.pdf",
                  "description": "\354\247\204\353\243\214\352\270\260\353\241\235\353\266\200"
                }
              ],
              "latest_summary": {
                "date": "20260421",
                "title": "2026\353\205\204 4\354\233\224 21\354\235\274 \354\247\204\353\243\214 \353\260\217 \352\262\200\354\202\254 \352\270\260\353\241\235",
                "description": "\354\265\234\352\267\274 \352\262\200\354\202\254\352\262\260\352\263\274\354\247\200\354\231\200 \354\247\204\353\243\214\352\270\260\353\241\235\353\266\200\352\260\200 \353\223\261\353\241\235\353\220\230 \354\236\210\354\212\265\353\213\210\353\213\244."
              }
            }
            """,
            "meta-file-2": b"""
            {
              "patient_id": "P0002",
              "name": "\355\231\215\352\270\270\353\217\231",
              "birth": "19800515",
              "latest_visit_date": "20260410",
              "files": [
                {
                  "type": "result",
                  "date": "20260410",
                  "filename": "result_20260410.pdf",
                  "description": "\352\262\200\354\202\254\352\262\260\352\263\274\354\247\200"
                }
              ],
              "latest_summary": {
                "date": "20260410",
                "title": "2026\353\205\204 4\354\233\224 10\354\235\274 \354\247\204\353\243\214 \353\260\217 \352\262\200\354\202\254 \352\270\260\353\241\235",
                "description": "\354\265\234\352\267\274 \352\262\200\354\202\254\352\262\260\352\263\274\354\247\200\354\231\200 \353\223\261\353\241\235\353\220\230 \354\236\210\354\212\265\353\213\210\353\213\244."
              }
            }
            """,
            "patient-folder": {
                "id": "patient-folder",
                "name": "P0001_\354\206\220\354\260\275\354\204\240_19461230",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": ["patients-folder"]
            },
            "result-new": b"result pdf bytes",
            "chart-new": b"chart pdf bytes",
            "result-2": b"result 2 pdf bytes",
            "patient-folder-2": {
                "id": "patient-folder-2",
                "name": "P0002_\355\231\215\352\270\270\353\217\231_19800515",
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
        self.file_map["meta-file"] = b"""
        {
          "patient_id": "P0001",
          "name": "\354\206\220\354\260\275\354\204\240",
          "birth": "19461230",
          "drive_folder_id": "patient-folder",
          "files": [
            {
              "type": "result",
              "date": "2026-04-21",
              "file_id": "result-new",
              "filename": "result_20260421.pdf"
            },
            {
              "type": "chart",
              "date": "2026-04-22",
              "file_id": "chart-new",
              "filename": "chart_20260421.pdf"
            }
          ],
          "allowed_kakao_user_ids": ["kakao-user-id-1"],
          "created_at": "2026-04-24T10:00:00+09:00"
        }
        """


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

    def test_get_patient_record_context_selects_latest_files_by_filename_date(self) -> None:
        patient = self.service.load_patient_index().patients[0]

        context = self.service.get_patient_record_context(
            patient=patient,
        )

        self.assertEqual(context.meta.latest_visit_date, "20260421")
        self.assertEqual(context.latest_result.name, "result_20260421.pdf")
        self.assertEqual(context.latest_chart.name, "chart_20260421.pdf")

    def test_download_drive_file_bytes_returns_selected_pdf_content(self) -> None:
        patient = self.service.load_patient_index().patients[0]
        context = self.service.get_patient_record_context(patient=patient)

        content = self.service.download_drive_file_bytes(context.latest_result)

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

    def test_legacy_schema_supports_patient_record_lookup(self) -> None:
        settings = Settings(
            google_service_account_path="credentials/google-service-account.json",
        )
        service = DriveLookupService(settings=settings, gateway=LegacyFakeDriveGateway())
        patient = service.load_patient_index().patients[0]

        context = service.get_patient_record_context(
            patient=patient,
        )

        self.assertEqual(context.patient.patient_id, "P0001")
        self.assertEqual(context.meta.latest_visit_date, "20260421")
        self.assertEqual(context.latest_result.name, "result_20260421.pdf")
        self.assertEqual(context.latest_chart.name, "chart_20260421.pdf")

    def test_get_patient_by_kakao_user_id(self) -> None:
        patient = self.service.get_patient_by_kakao_user_id("kakao-user-id-1")

        self.assertIsNotNone(patient)
        self.assertEqual(patient.patient_id, "P0001")

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

    def test_get_patient_record_context_without_meta_uses_pdf_files(self) -> None:
        del self.service.gateway.list_map["'patient-folder-2' in parents and name = 'meta.json' and trashed = false"]
        listing = self.service.gateway.list_map["'patient-folder-2' in parents and trashed = false"]
        self.service.gateway.list_map["'patient-folder-2' in parents and trashed = false"] = [
            item for item in listing if item["name"] != "meta.json"
        ]
        self.service.gateway.file_map.pop("meta-file-2", None)
        patient = next(
            item for item in self.service.load_patient_index().patients if item.patient_id == "P0002"
        )

        context = self.service.get_patient_record_context(
            patient=patient,
        )

        self.assertEqual(context.meta.latest_visit_date, "20260410")
        self.assertEqual(context.meta.latest_summary.description, "최근 검사결과지가 등록되어 있습니다.")
        self.assertEqual(context.latest_result.name, "result_20260410.pdf")


if __name__ == "__main__":
    unittest.main()
