from __future__ import annotations

import hashlib
import unittest

from app.config import Settings
from app.services.document_sync import DocumentRegistrySyncService
from app.services.drive import DriveLookupService
from app.services.gemini_qa import FileSearchStoreDocument
from tests.test_drive_service import FakeDriveGateway


class FakeGeminiStoreSyncService:
    def __init__(self) -> None:
        self.created_stores: list[dict[str, str]] = []
        self.upsert_calls: list[dict[str, object]] = []
        self.deleted_documents: list[str] = []
        self.documents_by_store: dict[str, list[FileSearchStoreDocument]] = {}
        self.invalid_document_name = False

    def ensure_file_search_store(
        self,
        *,
        patient_id: str,
        existing_store_name: str = "",
    ) -> str:
        if existing_store_name:
            self.documents_by_store.setdefault(existing_store_name, [])
            return existing_store_name
        store_name = f"fileSearchStores/patient-{patient_id}"
        self.created_stores.append({"patient_id": patient_id, "store_name": store_name})
        self.documents_by_store.setdefault(store_name, [])
        return store_name

    def list_documents(self, *, file_search_store_name: str) -> list[FileSearchStoreDocument]:
        return list(self.documents_by_store.get(file_search_store_name, []))

    def upsert_document(
        self,
        *,
        file_search_store_name: str,
        document,
        document_entry,
        existing_document_name: str = "",
    ) -> str:
        self.upsert_calls.append(
            {
                "file_search_store_name": file_search_store_name,
                "document_name": document.name,
                "document_entry": document_entry,
                "existing_document_name": existing_document_name,
            }
        )
        if existing_document_name:
            self.delete_document(document_name=existing_document_name)
        if self.invalid_document_name:
            return ""
        document_name = f"{file_search_store_name}/documents/{document_entry.drive_file_id}"
        self.documents_by_store.setdefault(file_search_store_name, [])
        self.documents_by_store[file_search_store_name] = [
            item
            for item in self.documents_by_store[file_search_store_name]
            if item.display_name != document.name
        ]
        self.documents_by_store[file_search_store_name].append(
            FileSearchStoreDocument(
                name=document_name,
                display_name=document.name,
                custom_metadata={"drive_file_id": document_entry.drive_file_id},
            )
        )
        return document_name

    def delete_document(self, *, document_name: str) -> None:
        self.deleted_documents.append(document_name)
        for store_name, documents in self.documents_by_store.items():
            self.documents_by_store[store_name] = [
                item for item in documents if item.name != document_name
            ]


class DocumentRegistrySyncServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        settings = Settings(
            google_service_account_path="credentials/google-service-account.json",
            gemini_api_key="test-key",
        )
        self.gateway = FakeDriveGateway()
        self.drive_service = DriveLookupService(settings=settings, gateway=self.gateway)
        self.gemini_store_sync_service = FakeGeminiStoreSyncService()
        self.service = DocumentRegistrySyncService(
            settings=settings,
            drive_service=self.drive_service,
            gemini_store_sync_service=self.gemini_store_sync_service,
        )

    def test_sync_document_registry_creates_ready_entries_and_skips_invalid_pdf_names(self) -> None:
        result = self.service.sync_document_registry()

        self.assertEqual(result.ready_count, 6)
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.imported_count, 6)
        self.assertEqual(result.deleted_count, 0)
        patient_entries = [
            item
            for item in result.document_registry.documents
            if item.patient_id == "P0001"
        ]
        self.assertEqual(
            [item.filename for item in patient_entries if item.sync_status == "READY"],
            [
                "chart_20260419.pdf",
                "result_20260420.pdf",
                "chart_20260421.pdf",
                "image_20260421.pdf",
                "result_20260421.pdf",
            ],
        )
        self.assertEqual(
            [item.filename for item in patient_entries if item.sync_status == "SKIPPED"],
            ["result_latest.pdf"],
        )
        self.assertEqual(
            self.gemini_store_sync_service.created_stores,
            [{"patient_id": "P0002", "store_name": "fileSearchStores/patient-P0002"}],
        )

    def test_sync_document_registry_reuses_existing_ready_registry_without_reimport(self) -> None:
        result_hash = hashlib.sha256(self.gateway.file_map["result-new"]).hexdigest()
        chart_hash = hashlib.sha256(self.gateway.file_map["chart-new"]).hexdigest()
        self.gateway.file_map["document-registry"] = f"""
        {{
          "generated_at": "2026-04-28T12:10:00+09:00",
          "documents": [
            {{
              "patient_id": "P0001",
              "filename": "result_20260421.pdf",
              "document_type": "result",
              "document_date": "20260421",
              "drive_file_id": "result-new",
              "drive_modified_time": "2026-04-21T10:00:00Z",
              "file_hash": "{result_hash}",
              "file_search_store_name": "fileSearchStores/patient-P0001",
              "file_search_document_name": "fileSearchStores/patient-P0001/documents/result-new",
              "sync_status": "READY",
              "synced_at": "2026-04-28T12:10:00+09:00"
            }},
            {{
              "patient_id": "P0001",
              "filename": "chart_20260421.pdf",
              "document_type": "chart",
              "document_date": "20260421",
              "drive_file_id": "chart-new",
              "drive_modified_time": "2026-04-21T11:00:00Z",
              "file_hash": "{chart_hash}",
              "file_search_store_name": "fileSearchStores/patient-P0001",
              "file_search_document_name": "fileSearchStores/patient-P0001/documents/chart-new",
              "sync_status": "READY",
              "synced_at": "2026-04-28T12:10:00+09:00"
            }}
          ]
        }}
        """.encode("utf-8")
        self.gemini_store_sync_service.documents_by_store["fileSearchStores/patient-P0001"] = [
            FileSearchStoreDocument(
                name="fileSearchStores/patient-P0001/documents/result-new",
                display_name="result_20260421.pdf",
                custom_metadata={"drive_file_id": "result-new"},
            ),
            FileSearchStoreDocument(
                name="fileSearchStores/patient-P0001/documents/chart-new",
                display_name="chart_20260421.pdf",
                custom_metadata={"drive_file_id": "chart-new"},
            ),
        ]

        result = self.service.sync_document_registry()

        self.assertEqual(result.imported_count, 4)
        p0001_entries = [
            item
            for item in result.document_registry.documents
            if item.patient_id == "P0001" and item.sync_status == "READY"
        ]
        result_entry = next(item for item in p0001_entries if item.filename == "result_20260421.pdf")
        chart_entry = next(item for item in p0001_entries if item.filename == "chart_20260421.pdf")
        self.assertEqual(
            result_entry.file_search_document_name,
            "fileSearchStores/patient-P0001/documents/result-new",
        )
        self.assertEqual(
            chart_entry.file_search_document_name,
            "fileSearchStores/patient-P0001/documents/chart-new",
        )

    def test_sync_document_registry_deletes_removed_store_documents(self) -> None:
        self.gateway.file_map["document-registry"] = b"""
        {
          "generated_at": "2026-04-28T12:10:00+09:00",
          "documents": [
            {
              "patient_id": "P0001",
              "filename": "removed_20260401.pdf",
              "document_type": "result",
              "document_date": "20260401",
              "drive_file_id": "removed-file",
              "drive_modified_time": "2026-04-01T10:00:00Z",
              "file_hash": "removed-hash",
              "file_search_store_name": "fileSearchStores/patient-P0001",
              "file_search_document_name": "fileSearchStores/patient-P0001/documents/removed-file",
              "sync_status": "READY",
              "synced_at": "2026-04-28T12:10:00+09:00"
            }
          ]
        }
        """

        result = self.service.sync_document_registry()

        self.assertEqual(result.deleted_count, 1)
        self.assertEqual(
            self.gemini_store_sync_service.deleted_documents,
            ["fileSearchStores/patient-P0001/documents/removed-file"],
        )

    def test_sync_document_registry_marks_failed_when_document_name_is_invalid(self) -> None:
        self.gemini_store_sync_service.invalid_document_name = True

        result = self.service.sync_document_registry()

        self.assertGreater(result.failed_count, 0)
        failed_entries = [
            item for item in result.document_registry.documents if item.sync_status == "FAILED"
        ]
        self.assertTrue(failed_entries)


if __name__ == "__main__":
    unittest.main()
