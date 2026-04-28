from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.config import Settings
from app.schemas import DocumentRegistryEntry, PatientDocumentRegistryContext, PatientIndexEntry
from app.services.gemini_qa import (
    FileSearchStoreDocument,
    GoogleGeminiGateway,
    GeminiDocument,
    GeminiGateway,
    GeminiPatientStoreSyncService,
    GeminiQaError,
    GeminiQaService,
    GeminiRecordNotFoundError,
)


class FakeGeminiGateway(GeminiGateway):
    def __init__(self) -> None:
        self.answer = (
            "핵심 답변: 제공된 진단 기록 문서에서 해당 내용을 확인하지 못했습니다.\n"
            "근거 문서: result_20260421.pdf, chart_20260421.pdf\n"
            "확인할 점: 담당 의료진에게 직접 확인해 주세요."
        )
        self.raise_error: Exception | None = None
        self.created_stores: list[str] = []
        self.import_calls: list[dict[str, object]] = []
        self.generate_calls: list[dict[str, object]] = []
        self.deleted_documents: list[str] = []
        self.documents_by_store: dict[str, list[FileSearchStoreDocument]] = {}

    def create_file_search_store(self, *, display_name: str) -> str:
        store_name = f"fileSearchStores/{display_name}"
        self.created_stores.append(display_name)
        self.documents_by_store.setdefault(store_name, [])
        return store_name

    def upload_document_to_store(
        self,
        *,
        file_search_store_name: str,
        document: GeminiDocument,
        custom_metadata: dict[str, str],
        chunk_max_tokens: int | None = None,
        chunk_overlap_tokens: int | None = None,
    ) -> str:
        self.import_calls.append(
            {
                "file_search_store_name": file_search_store_name,
                "document": document,
                "custom_metadata": custom_metadata,
                "chunk_max_tokens": chunk_max_tokens,
                "chunk_overlap_tokens": chunk_overlap_tokens,
            }
        )
        documents = [
            item
            for item in self.documents_by_store.get(file_search_store_name, [])
            if item.display_name != document.name
            and item.custom_metadata.get("drive_file_id") != custom_metadata["drive_file_id"]
        ]
        documents.append(
            FileSearchStoreDocument(
                name=(
                    f"{file_search_store_name}/documents/"
                    f"{custom_metadata['drive_file_id']}"
                ),
                display_name=document.name,
                custom_metadata=custom_metadata,
            )
        )
        self.documents_by_store[file_search_store_name] = documents
        return f"{file_search_store_name}/documents/{custom_metadata['drive_file_id']}"

    def list_store_documents(self, *, file_search_store_name: str) -> list[FileSearchStoreDocument]:
        return list(self.documents_by_store.get(file_search_store_name, []))

    def delete_store_document(self, *, document_name: str) -> None:
        self.deleted_documents.append(document_name)
        for store_name, documents in self.documents_by_store.items():
            self.documents_by_store[store_name] = [
                item for item in documents if item.name != document_name
            ]

    def generate_answer(
        self,
        *,
        model: str,
        system_instruction: str,
        prompt: str,
        file_search_store_name: str,
        temperature: float,
        max_output_tokens: int,
        thinking_budget: int | None,
        file_search_top_k: int,
        log_retrieval: bool,
    ) -> str:
        self.generate_calls.append(
            {
                "model": model,
                "system_instruction": system_instruction,
                "prompt": prompt,
                "file_search_store_name": file_search_store_name,
                "temperature": temperature,
                "max_output_tokens": max_output_tokens,
                "thinking_budget": thinking_budget,
                "file_search_top_k": file_search_top_k,
                "log_retrieval": log_retrieval,
            }
        )
        if self.raise_error is not None:
            raise self.raise_error
        return self.answer


class GeminiQaServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            google_service_account_path="credentials/google-service-account.json",
            gemini_api_key="test-key",
            gemini_model="gemini-test-model",
            gemini_temperature=0.1,
            gemini_max_output_tokens=700,
            gemini_thinking_budget=0,
            gemini_file_search_top_k=5,
            gemini_file_search_chunk_max_tokens=512,
            gemini_file_search_chunk_overlap_tokens=100,
            gemini_file_search_log_retrieval=True,
        )
        self.gateway = FakeGeminiGateway()
        self.qa_service = GeminiQaService(settings=self.settings, gateway=self.gateway)
        self.sync_service = GeminiPatientStoreSyncService(
            settings=self.settings,
            gateway=self.gateway,
        )

    def test_answer_question_uses_file_search_store_name(self) -> None:
        context = self._build_context(store_name="fileSearchStores/patient-P0001")

        answer = self.qa_service.answer_question(
            question="이번 기록에서 혈액검사 이상이 있나요?",
            context=context,
        )

        self.assertEqual(answer, self.gateway.answer)
        self.assertEqual(
            self.gateway.generate_calls[0]["file_search_store_name"],
            "fileSearchStores/patient-P0001",
        )
        self.assertIn("당신은 의료 문서 기반 질의응답 도우미입니다", self.gateway.generate_calls[0]["system_instruction"])
        self.assertIn("아래 PDF 파일들은 동일 환자의 최신 진단 기록입니다.", self.gateway.generate_calls[0]["prompt"])
        self.assertIn("result_20260421.pdf (검사결과지, 문서 날짜: 20260421)", self.gateway.generate_calls[0]["prompt"])
        self.assertIn("chart_20260421.pdf (진료기록부, 문서 날짜: 20260421)", self.gateway.generate_calls[0]["prompt"])
        self.assertIn("근거 문서 표시 규칙", self.gateway.generate_calls[0]["prompt"])
        self.assertIn("사용자 질문", self.gateway.generate_calls[0]["prompt"])
        self.assertEqual(self.gateway.generate_calls[0]["temperature"], 0.1)
        self.assertEqual(self.gateway.generate_calls[0]["max_output_tokens"], 700)
        self.assertEqual(self.gateway.generate_calls[0]["thinking_budget"], 0)
        self.assertEqual(self.gateway.generate_calls[0]["file_search_top_k"], 5)
        self.assertTrue(self.gateway.generate_calls[0]["log_retrieval"])

    def test_answer_question_uses_configured_generation_options(self) -> None:
        qa_service = GeminiQaService(
            settings=Settings(
                google_service_account_path="credentials/google-service-account.json",
                gemini_api_key="test-key",
                gemini_model="gemini-test-model",
                gemini_temperature=0.2,
                gemini_max_output_tokens=1024,
                gemini_thinking_budget=256,
                gemini_file_search_top_k=7,
                gemini_file_search_log_retrieval=False,
            ),
            gateway=self.gateway,
        )
        context = self._build_context(store_name="fileSearchStores/patient-P0001")

        qa_service.answer_question(
            question="이번 기록에서 혈액검사 이상이 있나요?",
            context=context,
        )

        self.assertEqual(self.gateway.generate_calls[0]["temperature"], 0.2)
        self.assertEqual(self.gateway.generate_calls[0]["max_output_tokens"], 1024)
        self.assertEqual(self.gateway.generate_calls[0]["thinking_budget"], 256)
        self.assertEqual(self.gateway.generate_calls[0]["file_search_top_k"], 7)
        self.assertFalse(self.gateway.generate_calls[0]["log_retrieval"])

    def test_answer_question_raises_when_store_name_missing(self) -> None:
        context = self._build_context(store_name="")

        with self.assertRaises(GeminiRecordNotFoundError):
            self.qa_service.answer_question(
                question="이번 기록에서 혈액검사 이상이 있나요?",
                context=context,
            )

    def test_answer_question_wraps_gateway_error(self) -> None:
        context = self._build_context(store_name="fileSearchStores/patient-P0001")
        self.gateway.raise_error = RuntimeError("gateway failed")

        with self.assertRaises(GeminiQaError):
            self.qa_service.answer_question(
                question="이번 기록에서 혈액검사 이상이 있나요?",
                context=context,
            )

    def test_ensure_file_search_store_reuses_existing_store(self) -> None:
        store_name = self.sync_service.ensure_file_search_store(
            patient_id="P0001",
            existing_store_name="fileSearchStores/patient-P0001",
        )

        self.assertEqual(store_name, "fileSearchStores/patient-P0001")
        self.assertEqual(self.gateway.created_stores, [])

    def test_ensure_file_search_store_creates_store_when_missing(self) -> None:
        store_name = self.sync_service.ensure_file_search_store(patient_id="P0001")

        self.assertEqual(store_name, "fileSearchStores/patient-P0001")
        self.assertEqual(self.gateway.created_stores, ["patient-P0001"])

    def test_upsert_document_imports_into_file_search_store(self) -> None:
        document_entry = DocumentRegistryEntry(
            patient_id="P0001",
            filename="result_20260421.pdf",
            document_type="result",
            document_date="20260421",
            drive_file_id="result-new",
            drive_modified_time="2026-04-21T10:00:00Z",
            file_hash="hash-1",
            sync_status="PENDING",
        )

        document_name = self.sync_service.upsert_document(
            file_search_store_name="fileSearchStores/patient-P0001",
            document=GeminiDocument(name="result_20260421.pdf", content=b"%PDF result pdf bytes"),
            document_entry=document_entry,
        )

        self.assertEqual(
            document_name,
            "fileSearchStores/patient-P0001/documents/result-new",
        )
        self.assertEqual(len(self.gateway.import_calls), 1)
        self.assertEqual(
            self.gateway.import_calls[0]["custom_metadata"]["patient_id"],
            "P0001",
        )
        self.assertEqual(self.gateway.import_calls[0]["chunk_max_tokens"], 512)
        self.assertEqual(self.gateway.import_calls[0]["chunk_overlap_tokens"], 100)

    def test_upsert_document_deletes_previous_document_before_reimport(self) -> None:
        self.gateway.documents_by_store["fileSearchStores/patient-P0001"] = [
            FileSearchStoreDocument(
                name="fileSearchStores/patient-P0001/documents/old-result",
                display_name="result_20260421.pdf",
                custom_metadata={"drive_file_id": "result-old"},
            )
        ]
        document_entry = DocumentRegistryEntry(
            patient_id="P0001",
            filename="result_20260421.pdf",
            document_type="result",
            document_date="20260421",
            drive_file_id="result-new",
            drive_modified_time="2026-04-21T10:00:00Z",
            file_hash="hash-1",
            sync_status="PENDING",
        )

        document_name = self.sync_service.upsert_document(
            file_search_store_name="fileSearchStores/patient-P0001",
            document=GeminiDocument(name="result_20260421.pdf", content=b"%PDF result pdf bytes"),
            document_entry=document_entry,
            existing_document_name="fileSearchStores/patient-P0001/documents/old-result",
        )

        self.assertEqual(
            self.gateway.deleted_documents,
            ["fileSearchStores/patient-P0001/documents/old-result"],
        )
        self.assertEqual(
            document_name,
            "fileSearchStores/patient-P0001/documents/result-new",
        )

    def _build_context(self, *, store_name: str) -> PatientDocumentRegistryContext:
        patient = PatientIndexEntry(
            patient_id="P0001",
            name="손창선",
            birth="19461230",
            folder_name="P0001_손창선_19461230",
        )
        documents = [
            DocumentRegistryEntry(
                patient_id="P0001",
                filename="result_20260421.pdf",
                document_type="result",
                document_date="20260421",
                drive_file_id="result-new",
                drive_modified_time="2026-04-21T10:00:00Z",
                file_hash="hash-1",
                file_search_store_name=store_name,
                file_search_document_name=f"{store_name}/documents/result-new" if store_name else "",
                sync_status="READY",
                synced_at="2026-04-28T12:10:00+09:00",
            ),
            DocumentRegistryEntry(
                patient_id="P0001",
                filename="chart_20260421.pdf",
                document_type="chart",
                document_date="20260421",
                drive_file_id="chart-new",
                drive_modified_time="2026-04-21T11:00:00Z",
                file_hash="hash-2",
                file_search_store_name=store_name,
                file_search_document_name=f"{store_name}/documents/chart-new" if store_name else "",
                sync_status="READY",
                synced_at="2026-04-28T12:10:00+09:00",
            ),
        ]
        return PatientDocumentRegistryContext(
            patient=patient,
            documents=documents,
            latest_result=documents[0],
            latest_chart=documents[1],
            file_search_store_name=store_name or None,
        )


class FakeFileSearchDocumentsApi:
    def __init__(self) -> None:
        self.list_calls: list[str] = []
        self.delete_calls: list[dict[str, object]] = []
        self.items: list[object] = []
        self.raise_on_force_delete = False

    def list(self, *, parent: str):
        self.list_calls.append(parent)
        return list(self.items)

    def delete(self, *, name: str, config: object | None = None) -> None:
        self.delete_calls.append({"name": name, "config": config})
        if self.raise_on_force_delete and config == {"force": True}:
            raise RuntimeError("force delete boom")


class FakeFileSearchStoresApi:
    def __init__(self) -> None:
        self.create_calls: list[dict[str, object]] = []
        self.import_calls: list[dict[str, object]] = []
        self.documents = FakeFileSearchDocumentsApi()
        self.operation = SimpleNamespace(
            done=True,
            error=None,
            response=SimpleNamespace(
                name="fileSearchStores/patient-P0001/documents/result-new"
            ),
        )
        self.raise_on_dict_config = False

    def create(self, *, config: dict[str, object]):
        self.create_calls.append(config)
        return SimpleNamespace(name="fileSearchStores/patient-P0001")

    def import_file(
        self,
        *,
        file_search_store_name: str,
        file_name: str,
        config: object,
    ):
        if self.raise_on_dict_config and isinstance(config, dict):
            raise RuntimeError("dict config boom")
        self.import_calls.append(
            {
                "file_search_store_name": file_search_store_name,
                "file_name": file_name,
                "config": config,
            }
        )
        return self.operation


class FakeFilesApi:
    def __init__(self) -> None:
        self.upload_calls: list[dict[str, object]] = []
        self.uploaded_file = SimpleNamespace(name="files/uploaded-result")

    def upload(self, *, file: str, config: dict[str, object]):
        self.upload_calls.append(
            {
                "file": file,
                "config": config,
            }
        )
        return self.uploaded_file


class FakeOperationsApi:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.next_operation: object | None = None

    def get(self, operation: object) -> object:
        self.calls.append(operation)
        if self.next_operation is not None:
            return self.next_operation
        return operation


class FakeModelsApi:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.next_response: object | None = None

    def generate_content(self, *, model: str, contents: str, config: object):
        self.calls.append(
            {
                "model": model,
                "contents": contents,
                "config": config,
            }
        )
        if self.next_response is not None:
            return self.next_response
        return SimpleNamespace(text="답변")


class GoogleGeminiGatewayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.file_search_patch = patch(
            "app.services.gemini_qa.types.FileSearch",
            side_effect=lambda **kwargs: kwargs,
            create=True,
        )
        self.tool_patch = patch(
            "app.services.gemini_qa.types.Tool",
            side_effect=lambda **kwargs: kwargs,
            create=True,
        )
        self.config_patch = patch(
            "app.services.gemini_qa.types.GenerateContentConfig",
            side_effect=lambda **kwargs: kwargs,
            create=True,
        )
        self.file_search_patch.start()
        self.tool_patch.start()
        self.config_patch.start()
        self.addCleanup(self.file_search_patch.stop)
        self.addCleanup(self.tool_patch.stop)
        self.addCleanup(self.config_patch.stop)

        self.file_search_stores = FakeFileSearchStoresApi()
        self.files = FakeFilesApi()
        self.operations = FakeOperationsApi()
        self.models = FakeModelsApi()
        self.client = SimpleNamespace(
            files=self.files,
            file_search_stores=self.file_search_stores,
            operations=self.operations,
            models=self.models,
        )
        self.gateway = GoogleGeminiGateway(api_key="test-key", client=self.client)

    def test_create_file_search_store_uses_sdk_api(self) -> None:
        name = self.gateway.create_file_search_store(display_name="patient-P0001")

        self.assertEqual(name, "fileSearchStores/patient-P0001")
        self.assertEqual(
            self.file_search_stores.create_calls,
            [{"display_name": "patient-P0001"}],
        )

    def test_upload_document_to_store_uses_files_upload_and_import_file(self) -> None:
        document_name = self.gateway.upload_document_to_store(
            file_search_store_name="fileSearchStores/patient-P0001",
            document=GeminiDocument(name="result_20260421.pdf", content=b"%PDF result pdf bytes"),
            custom_metadata={
                "patient_id": "P0001",
                "filename": "result_20260421.pdf",
                "document_type": "result",
                "document_date": "20260421",
                "drive_file_id": "result-new",
            },
        )

        self.assertEqual(
            document_name,
            "fileSearchStores/patient-P0001/documents/result-new",
        )
        self.assertEqual(len(self.files.upload_calls), 1)
        upload_call = self.files.upload_calls[0]
        self.assertEqual(upload_call["config"]["display_name"], "result_20260421.pdf")
        self.assertTrue(upload_call["file"].endswith(".pdf"))
        self.assertEqual(len(self.file_search_stores.import_calls), 1)
        call = self.file_search_stores.import_calls[0]
        self.assertEqual(call["file_search_store_name"], "fileSearchStores/patient-P0001")
        self.assertEqual(call["file_name"], "files/uploaded-result")
        self.assertIsInstance(call["config"], dict)
        first_metadata = call["config"]["custom_metadata"][0]
        self.assertEqual(first_metadata, {"key": "patient_id", "string_value": "P0001"})

    def test_upload_document_to_store_uses_latest_operation_response(self) -> None:
        self.file_search_stores.operation = SimpleNamespace(done=False, error=None, response=None)
        self.operations.next_operation = SimpleNamespace(
            done=True,
            error=None,
            response=SimpleNamespace(
                name="fileSearchStores/patient-P0001/documents/from-operation-get"
            ),
        )

        document_name = self.gateway.upload_document_to_store(
            file_search_store_name="fileSearchStores/patient-P0001",
            document=GeminiDocument(name="result_20260421.pdf", content=b"%PDF result pdf bytes"),
            custom_metadata={
                "patient_id": "P0001",
                "filename": "result_20260421.pdf",
                "document_type": "result",
                "document_date": "20260421",
                "drive_file_id": "result-new",
            },
        )

        self.assertEqual(
            document_name,
            "fileSearchStores/patient-P0001/documents/from-operation-get",
        )
        self.assertEqual(len(self.operations.calls), 1)

    def test_upload_document_to_store_uses_response_document_name(self) -> None:
        self.file_search_stores.operation = SimpleNamespace(
            done=True,
            error=None,
            response=SimpleNamespace(
                name="",
                document=SimpleNamespace(
                    name="fileSearchStores/patient-P0001/documents/from-document-field"
                ),
            ),
        )

        document_name = self.gateway.upload_document_to_store(
            file_search_store_name="fileSearchStores/patient-P0001",
            document=GeminiDocument(name="result_20260421.pdf", content=b"%PDF result pdf bytes"),
            custom_metadata={
                "patient_id": "P0001",
                "filename": "result_20260421.pdf",
                "document_type": "result",
                "document_date": "20260421",
                "drive_file_id": "result-new",
            },
        )

        self.assertEqual(
            document_name,
            "fileSearchStores/patient-P0001/documents/from-document-field",
        )

    def test_upload_document_to_store_falls_back_to_list_lookup(self) -> None:
        self.file_search_stores.operation = SimpleNamespace(done=True, error=None, response=None)
        self.file_search_stores.documents.items = [
            SimpleNamespace(
                name="fileSearchStores/patient-P0001/documents/result-new",
                display_name="result_20260421.pdf",
                custom_metadata=[
                    SimpleNamespace(key="drive_file_id", string_value="result-new", numeric_value=None)
                ],
            )
        ]

        document_name = self.gateway.upload_document_to_store(
            file_search_store_name="fileSearchStores/patient-P0001",
            document=GeminiDocument(name="result_20260421.pdf", content=b"%PDF result pdf bytes"),
            custom_metadata={
                "patient_id": "P0001",
                "filename": "result_20260421.pdf",
                "document_type": "result",
                "document_date": "20260421",
                "drive_file_id": "result-new",
            },
        )

        self.assertEqual(
            document_name,
            "fileSearchStores/patient-P0001/documents/result-new",
        )
        self.assertEqual(
            self.file_search_stores.documents.list_calls,
            ["fileSearchStores/patient-P0001"],
        )

    def test_upload_document_to_store_falls_back_to_typed_import_config(self) -> None:
        self.file_search_stores.raise_on_dict_config = True

        document_name = self.gateway.upload_document_to_store(
            file_search_store_name="fileSearchStores/patient-P0001",
            document=GeminiDocument(name="result_20260421.pdf", content=b"%PDF result pdf bytes"),
            custom_metadata={
                "patient_id": "P0001",
                "filename": "result_20260421.pdf",
                "document_type": "result",
                "document_date": "20260421",
                "drive_file_id": "result-new",
            },
        )

        self.assertEqual(
            document_name,
            "fileSearchStores/patient-P0001/documents/result-new",
        )
        self.assertEqual(len(self.file_search_stores.import_calls), 1)
        typed_call = self.file_search_stores.import_calls[0]
        self.assertFalse(isinstance(typed_call["config"], dict))
        first_metadata = typed_call["config"].custom_metadata[0]
        self.assertEqual(first_metadata.key, "patient_id")
        self.assertEqual(first_metadata.string_value, "P0001")

    def test_upload_document_to_store_logs_and_wraps_sdk_error(self) -> None:
        self.file_search_stores.import_file = MockRaiser(RuntimeError("import boom"))

        with self.assertLogs("app.services.gemini_qa", level="ERROR") as logs:
            with self.assertRaises(GeminiQaError):
                self.gateway.upload_document_to_store(
                    file_search_store_name="fileSearchStores/patient-P0001",
                    document=GeminiDocument(name="result_20260421.pdf", content=b"%PDF result pdf bytes"),
                    custom_metadata={
                        "patient_id": "P0001",
                        "filename": "result_20260421.pdf",
                        "document_type": "result",
                        "document_date": "20260421",
                        "drive_file_id": "result-new",
                    },
                )

        self.assertIn("drive_file_id=result-new", "\n".join(logs.output))
        self.assertIn("import boom", "\n".join(logs.output))

    def test_list_store_documents_uses_sdk_document_api(self) -> None:
        self.file_search_stores.documents.items = [
            SimpleNamespace(
                name="fileSearchStores/patient-P0001/documents/result-new",
                display_name="result_20260421.pdf",
                custom_metadata=[
                    SimpleNamespace(key="drive_file_id", string_value="result-new", numeric_value=None)
                ],
            )
        ]

        documents = self.gateway.list_store_documents(
            file_search_store_name="fileSearchStores/patient-P0001"
        )

        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].display_name, "result_20260421.pdf")
        self.assertEqual(documents[0].custom_metadata["drive_file_id"], "result-new")

    def test_delete_store_document_uses_sdk_document_api(self) -> None:
        self.gateway.delete_store_document(
            document_name="fileSearchStores/patient-P0001/documents/result-new"
        )

        self.assertEqual(
            self.file_search_stores.documents.delete_calls,
            [
                {
                    "name": "fileSearchStores/patient-P0001/documents/result-new",
                    "config": {"force": True},
                }
            ],
        )

    def test_delete_store_document_falls_back_when_force_delete_fails(self) -> None:
        self.file_search_stores.documents.raise_on_force_delete = True

        self.gateway.delete_store_document(
            document_name="fileSearchStores/patient-P0001/documents/result-new"
        )

        self.assertEqual(
            self.file_search_stores.documents.delete_calls,
            [
                {
                    "name": "fileSearchStores/patient-P0001/documents/result-new",
                    "config": {"force": True},
                },
                {
                    "name": "fileSearchStores/patient-P0001/documents/result-new",
                    "config": None,
                },
            ],
        )

    def test_generate_answer_uses_file_search_tool(self) -> None:
        answer = self.gateway.generate_answer(
            model="gemini-2.5-flash",
            system_instruction="시스템 지시",
            prompt="질문",
            file_search_store_name="fileSearchStores/patient-P0001",
            temperature=0.2,
            max_output_tokens=1024,
            thinking_budget=0,
            file_search_top_k=7,
            log_retrieval=True,
        )

        self.assertEqual(answer, "답변")
        self.assertEqual(len(self.models.calls), 1)
        config = self.models.calls[0]["config"]
        self.assertEqual(
            config["tools"][0]["file_search"]["file_search_store_names"],
            ["fileSearchStores/patient-P0001"],
        )
        self.assertEqual(config["tools"][0]["file_search"]["top_k"], 7)
        self.assertEqual(config["system_instruction"], "시스템 지시")
        self.assertEqual(config["temperature"], 0.2)
        self.assertEqual(config["max_output_tokens"], 1024)
        self.assertEqual(config["thinking_config"].thinking_budget, 0)

    def test_generate_answer_logs_finish_reason_and_retrieval_metadata_without_chunk_text(self) -> None:
        self.models.next_response = SimpleNamespace(
            text="답변",
            usage_metadata=SimpleNamespace(
                prompt_token_count=100,
                candidates_token_count=70,
                total_token_count=170,
            ),
            candidates=[
                SimpleNamespace(
                    finish_reason="MAX_TOKENS",
                    finish_message="token limit",
                    token_count=70,
                    grounding_metadata=SimpleNamespace(
                        grounding_chunks=[
                            SimpleNamespace(
                                retrieved_context=SimpleNamespace(
                                    document_name="fileSearchStores/patient-P0001/documents/result-new",
                                    title="result_20260421.pdf",
                                    uri="gemini://result",
                                    file_search_store="fileSearchStores/patient-P0001",
                                    rag_chunk=SimpleNamespace(
                                        page_span=SimpleNamespace(first_page=2, last_page=3),
                                        text="민감한 PDF 원문 chunk",
                                    ),
                                    text="민감한 retrieved context",
                                    custom_metadata=[
                                        SimpleNamespace(
                                            key="drive_file_id",
                                            string_value="result-new",
                                            numeric_value=None,
                                        )
                                    ],
                                )
                            )
                        ],
                        grounding_supports=[
                            SimpleNamespace(
                                grounding_chunk_indices=[0],
                                confidence_scores=[0.83],
                            )
                        ],
                    ),
                )
            ],
        )

        with self.assertLogs("app.services.gemini_qa", level="INFO") as logs:
            answer = self.gateway.generate_answer(
                model="gemini-2.5-flash",
                system_instruction="시스템 지시",
                prompt="질문",
                file_search_store_name="fileSearchStores/patient-P0001",
                temperature=0.2,
                max_output_tokens=700,
                thinking_budget=0,
                file_search_top_k=5,
                log_retrieval=True,
            )

        log_output = "\n".join(logs.output)
        self.assertEqual(answer, "답변")
        self.assertIn("finish_reason=MAX_TOKENS", log_output)
        self.assertIn("grounding_chunks=1", log_output)
        self.assertIn("document_name=fileSearchStores/patient-P0001/documents/result-new", log_output)
        self.assertIn("confidence_scores=[0.83]", log_output)
        self.assertNotIn("민감한 PDF 원문 chunk", log_output)
        self.assertNotIn("민감한 retrieved context", log_output)

    def test_chunking_config_helpers_use_large_pdf_defaults(self) -> None:
        rest_config = self.gateway._build_rest_chunking_config(
            max_tokens_per_chunk=512,
            max_overlap_tokens=100,
        )
        dict_config = self.gateway._build_dict_chunking_config(
            max_tokens_per_chunk=512,
            max_overlap_tokens=100,
        )
        typed_config = self.gateway._build_typed_chunking_config(
            max_tokens_per_chunk=512,
            max_overlap_tokens=100,
        )

        self.assertEqual(
            rest_config,
            {
                "whiteSpaceConfig": {
                    "maxTokensPerChunk": 512,
                    "maxOverlapTokens": 100,
                }
            },
        )
        self.assertEqual(
            dict_config,
            {
                "white_space_config": {
                    "max_tokens_per_chunk": 512,
                    "max_overlap_tokens": 100,
                }
            },
        )
        self.assertEqual(typed_config.white_space_config.max_tokens_per_chunk, 512)
        self.assertEqual(typed_config.white_space_config.max_overlap_tokens, 100)

    def test_chunking_config_helpers_clamp_values_above_api_limit(self) -> None:
        rest_config = self.gateway._build_rest_chunking_config(
            max_tokens_per_chunk=1000,
            max_overlap_tokens=100,
        )
        typed_config = self.gateway._build_typed_chunking_config(
            max_tokens_per_chunk=1000,
            max_overlap_tokens=100,
        )

        self.assertEqual(rest_config["whiteSpaceConfig"]["maxTokensPerChunk"], 512)
        self.assertEqual(typed_config.white_space_config.max_tokens_per_chunk, 512)

    def test_gateway_requires_file_search_sdk_support(self) -> None:
        with self.assertRaises(GeminiQaError):
            GoogleGeminiGateway(
                api_key="test-key",
                client=SimpleNamespace(models=self.models),
            )


class MockRaiser:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def __call__(self, *args, **kwargs):
        raise self.exc


if __name__ == "__main__":
    unittest.main()
