from __future__ import annotations

import io
import json
import logging
import mimetypes
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

try:
    import google.auth
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseDownload
except ImportError:  # pragma: no cover - optional dependency guard
    google = None  # type: ignore[assignment]
    service_account = None  # type: ignore[assignment]
    build = None  # type: ignore[assignment]
    MediaIoBaseDownload = None  # type: ignore[assignment]

from app.config import Settings
from app.prompts.gemini_qa import (
    build_gemini_qa_question_prompt,
    build_gemini_qa_system_instruction,
)
from app.schemas import DocumentRegistryEntry, PatientDocumentRegistryContext


logger = logging.getLogger(__name__)
FILE_SEARCH_OPERATION_POLL_SECONDS = 5
FILE_SEARCH_OPERATION_MAX_ATTEMPTS = 60
FILE_SEARCH_STORE_PREFIX = "fileSearchStores/"
FILE_SEARCH_DOCUMENT_SEGMENT = "/documents/"
PDF_MIME_TYPE = "application/pdf"
GEMINI_API_BASE_URL = "https://generativelanguage.googleapis.com"
GEMINI_API_VERSION = "v1beta"
GOOGLE_DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
GOOGLE_APPS_MIME_PREFIX = "application/vnd.google-apps."
GOOGLE_DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
GOOGLE_DRIVE_SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"
GOOGLE_WORKSPACE_EXPORTABLE_MIME_TYPES = {
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.presentation",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.drawing",
}


class GeminiQaError(Exception):
    pass


class GeminiRecordNotFoundError(GeminiQaError):
    pass


class GoogleDrivePdfError(GeminiQaError):
    pass


@dataclass(frozen=True)
class GeminiDocument:
    name: str
    content: bytes


@dataclass(frozen=True)
class FileSearchStoreDocument:
    name: str
    display_name: str
    custom_metadata: dict[str, str]


@dataclass(frozen=True)
class GoogleDriveFileReference:
    file_id: str
    name: str
    mime_type: str


@dataclass
class GeminiGateway:
    def create_file_search_store(self, *, display_name: str) -> str:
        raise NotImplementedError

    def upload_document_to_store(
        self,
        *,
        file_search_store_name: str,
        document: GeminiDocument,
        custom_metadata: dict[str, str],
    ) -> str:
        raise NotImplementedError

    def list_store_documents(self, *, file_search_store_name: str) -> list[FileSearchStoreDocument]:
        raise NotImplementedError

    def delete_store_document(self, *, document_name: str) -> None:
        raise NotImplementedError

    def generate_answer(
        self,
        *,
        model: str,
        system_instruction: str,
        prompt: str,
        file_search_store_name: str,
    ) -> str:
        raise NotImplementedError


def is_file_search_store_name(name: str) -> bool:
    return name.startswith(FILE_SEARCH_STORE_PREFIX)


def is_file_search_document_name(name: str) -> bool:
    return name.startswith(FILE_SEARCH_STORE_PREFIX) and FILE_SEARCH_DOCUMENT_SEGMENT in name


def extract_google_drive_id(file_id_or_url: str) -> str:
    """Extracts a Google Drive file or folder ID from common Drive URL forms."""
    value = file_id_or_url.strip()
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"/document/d/([a-zA-Z0-9_-]+)",
        r"/presentation/d/([a-zA-Z0-9_-]+)",
        r"/spreadsheets/d/([a-zA-Z0-9_-]+)",
        r"/drawings/d/([a-zA-Z0-9_-]+)",
        r"/folders/([a-zA-Z0-9_-]+)",
        r"[?&]id=([a-zA-Z0-9_-]+)",
        r"^([a-zA-Z0-9_-]{10,})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    raise GoogleDrivePdfError(f"Google Drive ID를 찾을 수 없습니다: {file_id_or_url}")


def _ensure_pdf_filename(name: str) -> str:
    clean_name = Path(name).name.strip() or "document.pdf"
    if not clean_name.lower().endswith(".pdf"):
        clean_name = f"{Path(clean_name).stem}.pdf"
    return clean_name


class GoogleDrivePdfGateway:
    """Downloads Drive PDF files or exportable Google Workspace files as PDF bytes."""

    def __init__(self, *, service: Any):
        self.service = service
        self._ensure_drive_support()

    @classmethod
    def from_adc(cls) -> "GoogleDrivePdfGateway":
        """Builds a Drive client with Application Default Credentials."""
        cls._ensure_drive_support()
        credentials, _ = google.auth.default(scopes=[GOOGLE_DRIVE_READONLY_SCOPE])  # type: ignore[union-attr]
        return cls(
            service=build(  # type: ignore[misc]
                "drive",
                "v3",
                credentials=credentials,
                cache_discovery=False,
            )
        )

    @classmethod
    def from_service_account_file(
        cls,
        service_account_file: str,
        *,
        delegated_user: str = "",
    ) -> "GoogleDrivePdfGateway":
        """Builds a Drive client from a service-account JSON file.

        If the PDFs live in a user's My Drive, share the files or containing folder
        with the service-account email. For Google Workspace domain-wide delegation,
        pass delegated_user.
        """
        cls._ensure_drive_support()
        credentials = service_account.Credentials.from_service_account_file(  # type: ignore[union-attr]
            service_account_file,
            scopes=[GOOGLE_DRIVE_READONLY_SCOPE],
        )
        if delegated_user:
            credentials = credentials.with_subject(delegated_user)
        return cls(
            service=build(  # type: ignore[misc]
                "drive",
                "v3",
                credentials=credentials,
                cache_discovery=False,
            )
        )

    def download_pdf(self, file_id_or_url: str, *, output_name: str = "") -> GeminiDocument:
        file_id = extract_google_drive_id(file_id_or_url)
        metadata = self._get_file_metadata(file_id)

        if metadata.get("mimeType") == GOOGLE_DRIVE_SHORTCUT_MIME_TYPE:
            shortcut = metadata.get("shortcutDetails") or {}
            target_id = shortcut.get("targetId")
            if not target_id:
                raise GoogleDrivePdfError(f"Drive shortcut targetId가 없습니다: {file_id}")
            file_id = target_id
            metadata = self._get_file_metadata(file_id)

        self._validate_downloadable(metadata)
        name = output_name or metadata.get("name") or f"{file_id}.pdf"
        mime_type = metadata.get("mimeType") or ""

        logger.info(
            "Google Drive PDF download start file_id=%s name=%s mime_type=%s",
            file_id,
            name,
            mime_type,
        )

        if mime_type == PDF_MIME_TYPE:
            request = self.service.files().get_media(
                fileId=file_id,
                supportsAllDrives=True,
            )
        elif mime_type in GOOGLE_WORKSPACE_EXPORTABLE_MIME_TYPES:
            request = self.service.files().export_media(
                fileId=file_id,
                mimeType=PDF_MIME_TYPE,
            )
        elif mime_type.startswith(GOOGLE_APPS_MIME_PREFIX):
            raise GoogleDrivePdfError(
                f"PDF로 export할 수 없는 Google Workspace 파일입니다: {name} ({mime_type})"
            )
        else:
            raise GoogleDrivePdfError(
                f"PDF 파일이 아닙니다. Drive에서 PDF만 업로드할 수 있습니다: {name} ({mime_type})"
            )

        content = self._download_request_to_bytes(request)
        if not content:
            raise GoogleDrivePdfError(f"Drive 파일 다운로드 결과가 비어 있습니다: {name}")
        if not content.startswith(b"%PDF"):
            raise GoogleDrivePdfError(
                f"다운로드한 파일이 PDF 바이트가 아닙니다. Drive 권한 또는 mimeType을 확인하세요: {name}"
            )

        return GeminiDocument(name=_ensure_pdf_filename(name), content=content)

    def list_pdf_files_in_folder(
        self,
        folder_id_or_url: str,
        *,
        include_google_workspace_exports: bool = False,
    ) -> list[GoogleDriveFileReference]:
        folder_id = extract_google_drive_id(folder_id_or_url)
        mime_types = [PDF_MIME_TYPE]
        if include_google_workspace_exports:
            mime_types.extend(sorted(GOOGLE_WORKSPACE_EXPORTABLE_MIME_TYPES))
        mime_query = " or ".join(f"mimeType='{mime_type}'" for mime_type in mime_types)
        query = f"'{folder_id}' in parents and trashed=false and ({mime_query})"

        references: list[GoogleDriveFileReference] = []
        page_token: str | None = None
        while True:
            response = (
                self.service.files()
                .list(
                    q=query,
                    fields="nextPageToken, files(id, name, mimeType)",
                    pageToken=page_token,
                    pageSize=1000,
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                    corpora="allDrives",
                )
                .execute()
            )
            for item in response.get("files", []):
                references.append(
                    GoogleDriveFileReference(
                        file_id=item["id"],
                        name=item.get("name", item["id"]),
                        mime_type=item.get("mimeType", ""),
                    )
                )
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return references

    def download_pdfs_from_folder(
        self,
        folder_id_or_url: str,
        *,
        include_google_workspace_exports: bool = False,
    ) -> list[GeminiDocument]:
        return [
            self.download_pdf(reference.file_id, output_name=reference.name)
            for reference in self.list_pdf_files_in_folder(
                folder_id_or_url,
                include_google_workspace_exports=include_google_workspace_exports,
            )
        ]

    def _get_file_metadata(self, file_id: str) -> dict[str, Any]:
        return (
            self.service.files()
            .get(
                fileId=file_id,
                fields=(
                    "id, name, mimeType, size, capabilities/canDownload, "
                    "shortcutDetails(targetId,targetMimeType)"
                ),
                supportsAllDrives=True,
            )
            .execute()
        )

    def _validate_downloadable(self, metadata: dict[str, Any]) -> None:
        can_download = (metadata.get("capabilities") or {}).get("canDownload")
        if can_download is False:
            raise GoogleDrivePdfError(
                f"Drive 파일 다운로드 권한이 없습니다: {metadata.get('name', metadata.get('id', 'unknown'))}"
            )

    def _download_request_to_bytes(self, request: Any) -> bytes:
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)  # type: ignore[misc]
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue()

    @staticmethod
    def _ensure_drive_support() -> None:
        if google is None or service_account is None or build is None or MediaIoBaseDownload is None:
            raise GoogleDrivePdfError(
                "Google Drive PDF 다운로드에 필요한 패키지가 없습니다. "
                "google-api-python-client, google-auth를 설치하세요."
            )


class GoogleGeminiGateway(GeminiGateway):
    def __init__(self, api_key: str, *, client: Any | None = None):
        normalized_api_key = (api_key or "").strip().strip('"').strip("'")
        if not normalized_api_key:
            raise GeminiQaError("Gemini API key is empty.")
        if os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in {"1", "true", "yes"}:
            raise GeminiQaError(
                "GOOGLE_GENAI_USE_VERTEXAI=true 상태입니다. "
                "Gemini Developer API의 File Search Store를 API Key로 사용하려면 Vertex AI 모드를 끄세요."
            )
        self.api_key = normalized_api_key
        self.client = client or genai.Client(api_key=normalized_api_key)
        self._ensure_file_search_support()

    def create_file_search_store(self, *, display_name: str) -> str:
        store = self.client.file_search_stores.create(
            config={"display_name": display_name},
        )
        name = getattr(store, "name", "")
        if not name:
            raise GeminiQaError("Gemini File Search Store 이름이 없습니다.")
        return name

    def is_file_search_store_accessible(self, *, file_search_store_name: str) -> bool:
        if not is_file_search_store_name(file_search_store_name):
            return False
        try:
            url = self._gemini_rest_url(
                f"/{GEMINI_API_VERSION}/{file_search_store_name}",
                query={"key": self.api_key},
            )
            self._rest_request_json(method="GET", url=url)
            return True
        except Exception:
            logger.warning(
                "Gemini File Search Store is not accessible with current API key store=%s",
                file_search_store_name,
                exc_info=True,
            )
            return False

    def upload_document_to_store(
        self,
        *,
        file_search_store_name: str,
        document: GeminiDocument,
        custom_metadata: dict[str, str],
    ) -> str:
        metadata = self._sanitize_custom_metadata(custom_metadata)
        self._validate_document_before_upload(document)

        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=self._build_temp_suffix(document.name),
                delete=False,
            ) as temp_file:
                temp_file.write(document.content)
                temp_path = temp_file.name

            logger.info(
                "Gemini File Search upload start file=%s store=%s size=%s header=%s",
                document.name,
                file_search_store_name,
                len(document.content),
                document.content[:8],
            )

            operation = self._upload_file_to_store(
                file_search_store_name=file_search_store_name,
                temp_path=temp_path,
                display_name=document.name,
                custom_metadata=metadata,
            )
            operation = self._wait_for_operation(operation)

            response = self._get_operation_response(operation)
            document_name = self._extract_document_name(response)
            if is_file_search_document_name(document_name):
                return document_name

            documents = self.list_store_documents(file_search_store_name=file_search_store_name)
            for item in documents:
                if item.custom_metadata.get("drive_file_id") == metadata.get("drive_file_id"):
                    return item.name
                if item.display_name == document.name:
                    return item.name
            raise GeminiQaError("Gemini File Search 문서 이름을 찾지 못했습니다.")
        except GeminiQaError:
            raise
        except Exception as exc:
            logger.exception(
                "Gemini File Search upload failed file=%s store=%s drive_file_id=%s error_type=%s error=%s",
                document.name,
                file_search_store_name,
                metadata.get("drive_file_id", ""),
                type(exc).__name__,
                str(exc),
            )
            raise GeminiQaError("Gemini File Search 문서 업로드에 실패했습니다.") from exc
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)

    def list_store_documents(self, *, file_search_store_name: str) -> list[FileSearchStoreDocument]:
        documents: list[FileSearchStoreDocument] = []
        for item in self.client.file_search_stores.documents.list(parent=file_search_store_name):
            documents.append(
                FileSearchStoreDocument(
                    name=getattr(item, "name", ""),
                    display_name=getattr(item, "display_name", ""),
                    custom_metadata=self._parse_custom_metadata(
                        getattr(item, "custom_metadata", None) or []
                    ),
                )
            )
        return documents

    def delete_store_document(self, *, document_name: str) -> None:
        try:
            self.client.file_search_stores.documents.delete(
                name=document_name,
                config={"force": True},
            )
            return
        except Exception:
            logger.warning(
                "Gemini File Search force delete failed document_name=%s",
                document_name,
                exc_info=True,
            )
        try:
            self.client.file_search_stores.documents.delete(name=document_name)
        except Exception as exc:
            raise GeminiQaError("Gemini File Search 문서 삭제에 실패했습니다.") from exc

    def generate_answer(
        self,
        *,
        model: str,
        system_instruction: str,
        prompt: str,
        file_search_store_name: str,
    ) -> str:
        try:
            logger.info(
                "Gemini QA using File Search Store model=%s store=%s",
                model,
                file_search_store_name,
            )

            response = self.client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    tools=[
                        types.Tool(
                            file_search=types.FileSearch(
                                file_search_store_names=[file_search_store_name]
                            )
                        )
                    ],
                    temperature=0.1,
                    max_output_tokens=700,
                ),
            )
        except Exception as exc:
            raise GeminiQaError("Gemini 답변 생성에 실패했습니다.") from exc
        text = getattr(response, "text", "") or ""
        if not text:
            raise GeminiQaError("Gemini 응답이 비어 있습니다.")
        return text

    def _wait_for_operation(self, operation: Any) -> Any:
        for _ in range(FILE_SEARCH_OPERATION_MAX_ATTEMPTS):
            done = self._operation_get(operation, "done", False)
            if done:
                error = self._operation_get(operation, "error", None)
                if error:
                    logger.error(
                        "Gemini File Search operation failed operation=%s error=%s",
                        self._operation_get(operation, "name", ""),
                        error,
                    )
                    raise GeminiQaError("Gemini File Search 작업이 실패했습니다.")
                return operation
            try:
                operation_name = self._operation_get(operation, "name", "")
                if isinstance(operation, dict) and operation_name:
                    operation = self._get_rest_operation(operation_name)
                else:
                    operation = self.client.operations.get(operation)
            except Exception as exc:
                logger.exception(
                    "Gemini File Search operation lookup failed operation=%s",
                    self._operation_get(operation, "name", ""),
                )
                raise GeminiQaError("Gemini File Search 작업 조회에 실패했습니다.") from exc
            time.sleep(FILE_SEARCH_OPERATION_POLL_SECONDS)
        raise GeminiQaError("Gemini File Search 작업이 지연되고 있습니다.")

    def _upload_file_to_store(
        self,
        *,
        file_search_store_name: str,
        temp_path: str,
        display_name: str,
        custom_metadata: dict[str, str],
    ) -> Any:
        """Uploads a local file directly into a File Search Store.

        The latest logs show that Files API upload succeeds but importFile
        consistently returns 401. Therefore the primary path now uses the
        official media.uploadToFileSearchStore endpoint, which uploads, chunks,
        and stores the PDF in one operation without calling importFile.
        """
        metadata = self._sanitize_custom_metadata(custom_metadata)
        mime_type = mimetypes.guess_type(display_name)[0] or PDF_MIME_TYPE

        try:
            return self._upload_to_file_search_store_rest(
                file_search_store_name=file_search_store_name,
                temp_path=temp_path,
                display_name=display_name,
                mime_type=mime_type,
                custom_metadata=metadata,
            )
        except Exception:
            logger.warning(
                "Gemini REST uploadToFileSearchStore failed; falling back to SDK upload_to_file_search_store store=%s file=%s",
                file_search_store_name,
                display_name,
                exc_info=True,
            )

        if hasattr(self.client.file_search_stores, "upload_to_file_search_store"):
            try:
                return self.client.file_search_stores.upload_to_file_search_store(
                    file_search_store_name=file_search_store_name,
                    file=temp_path,
                    config={"display_name": display_name},
                )
            except Exception:
                logger.warning(
                    "Gemini SDK upload_to_file_search_store minimal config failed; falling back to Files API import flow store=%s file=%s",
                    file_search_store_name,
                    display_name,
                    exc_info=True,
                )

        uploaded_file = self.client.files.upload(
            file=temp_path,
            config={"display_name": display_name},
        )
        uploaded_file_name = getattr(uploaded_file, "name", "")
        if not uploaded_file_name:
            raise GeminiQaError("Gemini 업로드 파일 이름이 없습니다.")
        return self._import_file_to_store(
            file_search_store_name=file_search_store_name,
            file_name=uploaded_file_name,
            custom_metadata=metadata,
        )

    def _import_file_to_store(
        self,
        *,
        file_search_store_name: str,
        file_name: str,
        custom_metadata: dict[str, str],
    ) -> Any:
        metadata = self._sanitize_custom_metadata(custom_metadata)
        if metadata:
            try:
                return self.client.file_search_stores.import_file(
                    file_search_store_name=file_search_store_name,
                    file_name=file_name,
                    config=types.ImportFileConfig(
                        custom_metadata=self._build_typed_custom_metadata(metadata)
                    ),
                )
            except Exception:
                logger.warning(
                    "Gemini File Search import_file with metadata failed store=%s file_name=%s",
                    file_search_store_name,
                    file_name,
                    exc_info=True,
                )
        try:
            return self.client.file_search_stores.import_file(
                file_search_store_name=file_search_store_name,
                file_name=file_name,
            )
        except Exception:
            logger.warning(
                "Gemini File Search SDK import_file failed; retrying REST importFile store=%s file_name=%s",
                file_search_store_name,
                file_name,
                exc_info=True,
            )
        return self._import_file_to_store_rest(
            file_search_store_name=file_search_store_name,
            file_name=file_name,
            custom_metadata=metadata,
        )

    def _upload_to_file_search_store_rest(
        self,
        *,
        file_search_store_name: str,
        temp_path: str,
        display_name: str,
        mime_type: str,
        custom_metadata: dict[str, str],
    ) -> dict[str, Any]:
        metadata = self._sanitize_custom_metadata(custom_metadata)
        body: dict[str, Any] = {
            "displayName": display_name,
            "mimeType": mime_type,
        }
        if metadata:
            body["customMetadata"] = self._build_rest_custom_metadata(metadata)

        try:
            return self._post_upload_to_file_search_store_rest(
                file_search_store_name=file_search_store_name,
                temp_path=temp_path,
                display_name=display_name,
                mime_type=mime_type,
                metadata_body=body,
            )
        except Exception:
            if not metadata:
                raise
            logger.warning(
                "Gemini REST uploadToFileSearchStore with metadata failed; retrying without metadata store=%s file=%s",
                file_search_store_name,
                display_name,
                exc_info=True,
            )
            return self._post_upload_to_file_search_store_rest(
                file_search_store_name=file_search_store_name,
                temp_path=temp_path,
                display_name=display_name,
                mime_type=mime_type,
                metadata_body={
                    "displayName": display_name,
                    "mimeType": mime_type,
                },
            )

    def _post_upload_to_file_search_store_rest(
        self,
        *,
        file_search_store_name: str,
        temp_path: str,
        display_name: str,
        mime_type: str,
        metadata_body: dict[str, Any],
    ) -> dict[str, Any]:
        boundary = f"===============gemini-fss-{uuid.uuid4().hex}=="
        metadata_part = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="metadata"\r\n'
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata_body, ensure_ascii=False)}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{self._escape_multipart_filename(display_name)}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8")
        closing = f"\r\n--{boundary}--".encode("utf-8")

        with open(temp_path, "rb") as file_obj:
            payload = metadata_part + file_obj.read() + closing

        url = self._gemini_rest_url(
            f"/upload/{GEMINI_API_VERSION}/{file_search_store_name}:uploadToFileSearchStore",
            query={"uploadType": "multipart"},
        )
        response_json = self._rest_request_json(
            method="POST",
            url=url,
            body=payload,
            headers={
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
        )
        logger.info(
            "Gemini REST uploadToFileSearchStore started store=%s file=%s operation=%s",
            file_search_store_name,
            display_name,
            response_json.get("name", ""),
        )
        return response_json

    def _upload_file_to_gemini_files_rest(
        self,
        *,
        temp_path: str,
        display_name: str,
        mime_type: str,
    ) -> str:
        boundary = f"===============gemini-{uuid.uuid4().hex}=="
        metadata = {"file": {"displayName": display_name}}
        metadata_part = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="metadata"\r\n'
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata, ensure_ascii=False)}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{self._escape_multipart_filename(display_name)}"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode("utf-8")
        closing = f"\r\n--{boundary}--".encode("utf-8")

        with open(temp_path, "rb") as file_obj:
            payload = metadata_part + file_obj.read() + closing

        url = self._gemini_rest_url(
            f"/upload/{GEMINI_API_VERSION}/files",
            query={"uploadType": "multipart"},
        )
        response_json = self._rest_request_json(
            method="POST",
            url=url,
            body=payload,
            headers={
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
        )
        file_info = response_json.get("file", response_json)
        file_name = file_info.get("name", "") if isinstance(file_info, dict) else ""
        if not file_name:
            raise GeminiQaError(f"Gemini Files REST 업로드 응답에서 file.name을 찾지 못했습니다: {response_json}")
        logger.info(
            "Gemini Files REST upload complete file=%s uploaded_file_name=%s",
            display_name,
            file_name,
        )
        return file_name

    def _import_file_to_store_rest(
        self,
        *,
        file_search_store_name: str,
        file_name: str,
        custom_metadata: dict[str, str],
    ) -> dict[str, Any]:
        metadata = self._sanitize_custom_metadata(custom_metadata)
        body: dict[str, Any] = {"fileName": file_name}
        if metadata:
            body["customMetadata"] = self._build_rest_custom_metadata(metadata)

        try:
            return self._post_import_file_rest(
                file_search_store_name=file_search_store_name,
                body=body,
            )
        except Exception:
            if not metadata:
                raise
            logger.warning(
                "Gemini REST importFile with metadata failed; retrying without metadata store=%s file_name=%s",
                file_search_store_name,
                file_name,
                exc_info=True,
            )
            return self._post_import_file_rest(
                file_search_store_name=file_search_store_name,
                body={"fileName": file_name},
            )

    def _post_import_file_rest(
        self,
        *,
        file_search_store_name: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        url = self._gemini_rest_url(
            f"/{GEMINI_API_VERSION}/{file_search_store_name}:importFile",
        )
        response_json = self._rest_request_json(
            method="POST",
            url=url,
            body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=UTF-8"},
        )
        logger.info(
            "Gemini REST importFile started store=%s file_name=%s operation=%s",
            file_search_store_name,
            body.get("fileName", ""),
            response_json.get("name", ""),
        )
        return response_json

    def _get_rest_operation(self, operation_name: str) -> dict[str, Any]:
        url = self._gemini_rest_url(
            f"/{GEMINI_API_VERSION}/{operation_name}",
        )
        return self._rest_request_json(method="GET", url=url)

    def _rest_request_json(
        self,
        *,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        # Official Gemini REST auth uses x-goog-api-key.
        # Do not also put ?key=... on File Search upload/import URLs because
        # duplicate credentials can make endpoint behavior harder to diagnose.
        request_headers = {
            "x-goog-api-key": self.api_key,
        }
        if body is not None:
            request_headers["Content-Type"] = "application/json; charset=UTF-8"
        request_headers.update(headers or {})

        max_attempts = 3
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            request = urllib.request.Request(
                url,
                data=body,
                headers=request_headers,
                method=method,
            )
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    response_body = response.read().decode("utf-8")
                    if not response_body:
                        return {}
                    return json.loads(response_body)
            except urllib.error.HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="replace")
                last_error = GeminiQaError(
                    f"Gemini REST 요청 실패 status={exc.code} body={error_body[:500]}"
                )
                logger.error(
                    "Gemini REST request failed method=%s status=%s attempt=%s/%s url=%s body=%s",
                    method,
                    exc.code,
                    attempt,
                    max_attempts,
                    self._redact_api_key_from_url(url),
                    error_body[:2000],
                )
                if exc.code in {429, 500, 502, 503, 504} and attempt < max_attempts:
                    time.sleep(attempt)
                    continue
                raise last_error from exc
            except urllib.error.URLError as exc:
                last_error = GeminiQaError(f"Gemini REST 요청 네트워크 오류: {exc}")
                if attempt < max_attempts:
                    time.sleep(attempt)
                    continue
                raise last_error from exc

        if last_error:
            raise last_error
        raise GeminiQaError("Gemini REST 요청에 실패했습니다.")

    def _gemini_rest_url(self, path: str, *, query: dict[str, str] | None = None) -> str:
        encoded_query = urllib.parse.urlencode(query or {})
        url = f"{GEMINI_API_BASE_URL}{path}"
        if encoded_query:
            url = f"{url}?{encoded_query}"
        return url

    def _redact_api_key_from_url(self, url: str) -> str:
        return re.sub(r"([?&]key=)[^&]+", r"\1***", url)

    def _build_rest_custom_metadata(self, custom_metadata: dict[str, str]) -> list[dict[str, str]]:
        return [
            {"key": key, "stringValue": value}
            for key, value in self._sanitize_custom_metadata(custom_metadata).items()
        ]

    def _operation_get(self, operation: Any, key: str, default: Any = None) -> Any:
        if isinstance(operation, dict):
            return operation.get(key, default)
        return getattr(operation, key, default)

    def _escape_multipart_filename(self, filename: str) -> str:
        return filename.replace("\\", "_").replace('"', "_").replace("\r", "_").replace("\n", "_")

    def _build_dict_custom_metadata(self, custom_metadata: dict[str, str]) -> list[dict[str, str]]:
        return [
            {"key": key, "string_value": value}
            for key, value in self._sanitize_custom_metadata(custom_metadata).items()
        ]

    def _build_typed_custom_metadata(
        self,
        custom_metadata: dict[str, str],
    ) -> list[Any]:
        return [
            types.CustomMetadata(key=key, string_value=value)
            for key, value in self._sanitize_custom_metadata(custom_metadata).items()
        ]

    def _parse_custom_metadata(self, items: list[Any]) -> dict[str, str]:
        metadata: dict[str, str] = {}
        for item in items:
            if isinstance(item, dict):
                key = item.get("key")
                string_value = item.get("string_value", item.get("stringValue"))
                numeric_value = item.get("numeric_value", item.get("numericValue"))
            else:
                key = getattr(item, "key", None)
                string_value = getattr(item, "string_value", None) or getattr(item, "stringValue", None)
                numeric_value = getattr(item, "numeric_value", None) or getattr(item, "numericValue", None)
            if not key:
                continue
            value = string_value
            if value is None and numeric_value is not None:
                value = str(numeric_value)
            if value is not None:
                metadata[key] = value
        return metadata

    def _ensure_file_search_support(self) -> None:
        if not hasattr(self.client, "file_search_stores"):
            raise GeminiQaError(
                "현재 google-genai SDK는 File Search Store를 지원하지 않습니다. "
                "requirements.txt의 google-genai 버전을 업그레이드하세요."
            )
        if not hasattr(self.client, "files"):
            raise GeminiQaError(
                "현재 google-genai SDK에는 Files API가 없습니다. "
                "requirements.txt의 google-genai 버전을 업그레이드하세요."
            )
        if not hasattr(self.client, "operations"):
            raise GeminiQaError(
                "현재 google-genai SDK에는 operations API가 없습니다. "
                "requirements.txt의 google-genai 버전을 업그레이드하세요."
            )
        if not hasattr(types, "FileSearch"):
            raise GeminiQaError(
                "현재 google-genai SDK에는 FileSearch 타입이 없습니다. "
                "requirements.txt의 google-genai 버전을 업그레이드하세요."
            )

    def _build_temp_suffix(self, document_name: str) -> str:
        _, extension = os.path.splitext(document_name)
        return extension or ".pdf"

    def _get_operation_response(self, operation: Any) -> Any:
        if isinstance(operation, dict):
            if "response" in operation:
                return operation.get("response")
            if "document" in operation:
                return operation.get("document")
            result = operation.get("result")
            if isinstance(result, dict):
                return result.get("response", result)
            return result
        response = getattr(operation, "response", None)
        if response is not None:
            return response
        result = getattr(operation, "result", None)
        if isinstance(result, dict):
            return result.get("response", result)
        return result

    def _extract_document_name(self, response: Any) -> str:
        if response is None:
            return ""
        if isinstance(response, dict):
            for key in ("document_name", "documentName", "name"):
                value = response.get(key, "")
                if value:
                    return value
            document = response.get("document") or {}
            if isinstance(document, dict):
                return document.get("name", "")
            return getattr(document, "name", "")
        for attr in ("document_name", "documentName", "name"):
            value = getattr(response, attr, "")
            if value:
                return value
        document = getattr(response, "document", None)
        if document is None:
            return ""
        return getattr(document, "name", "")

    def _sanitize_custom_metadata(self, custom_metadata: dict[str, Any]) -> dict[str, str]:
        sanitized: dict[str, str] = {}
        for key, value in custom_metadata.items():
            if value is None:
                continue
            string_value = str(value).strip()
            if string_value:
                sanitized[str(key)] = string_value
        return sanitized

    def _validate_document_before_upload(self, document: GeminiDocument) -> None:
        if not document.name:
            raise GeminiQaError("업로드할 문서명이 비어 있습니다.")
        if not document.content:
            raise GeminiQaError(f"업로드할 문서 내용이 비어 있습니다: {document.name}")
        if document.name.lower().endswith(".pdf") and not document.content.startswith(b"%PDF"):
            raise GeminiQaError(
                f"PDF 파일명이지만 PDF 바이트가 아닙니다. Drive URL 자체를 저장한 HTML인지 확인하세요: {document.name}"
            )


@dataclass
class GeminiQaService:
    settings: Settings
    gateway: GeminiGateway

    def answer_question(
        self,
        *,
        question: str,
        context: PatientDocumentRegistryContext,
    ) -> str:
        logger.info("gemini_qa answer_question patient_id=%s", context.patient.patient_id)
        if not context.file_search_store_name:
            raise GeminiRecordNotFoundError("질의응답에 사용할 문서 저장소가 없습니다.")

        ready_documents = [item for item in context.documents if item.sync_status == "READY"]
        if not ready_documents:
            raise GeminiRecordNotFoundError("질의응답에 사용할 진단기록 PDF가 없습니다.")

        document_descriptions = self._build_document_descriptions(ready_documents)
        system_instruction = build_gemini_qa_system_instruction(
            document_descriptions=document_descriptions,
        )
        prompt = build_gemini_qa_question_prompt(question=question)
        try:
            return self.gateway.generate_answer(
                model=self.settings.gemini_model,
                system_instruction=system_instruction,
                prompt=prompt,
                file_search_store_name=context.file_search_store_name,
            )
        except GeminiQaError:
            raise
        except Exception as exc:
            logger.exception("Gemini generate_answer failed")
            raise GeminiQaError("Gemini 답변 생성에 실패했습니다.") from exc

    def _build_document_descriptions(self, documents: list[DocumentRegistryEntry]) -> str:
        return "\n".join(
            (
                f"- {document.filename} "
                f"({document.document_type or '문서'}, {document.document_date or '날짜 미상'})"
            )
            for document in documents
        )


@dataclass
class GeminiPatientStoreSyncService:
    settings: Settings
    gateway: GeminiGateway

    def ensure_file_search_store(
        self,
        *,
        patient_id: str,
        existing_store_name: str = "",
    ) -> str:
        if existing_store_name and is_file_search_store_name(existing_store_name):
            accessibility_checker = getattr(self.gateway, "is_file_search_store_accessible", None)
            if not callable(accessibility_checker) or accessibility_checker(
                file_search_store_name=existing_store_name
            ):
                return existing_store_name
            logger.warning(
                "Existing Gemini File Search Store is not accessible; creating a new one patient_id=%s old_store=%s",
                patient_id,
                existing_store_name,
            )
        return self.gateway.create_file_search_store(
            display_name=f"patient-{patient_id}",
        )

    def upsert_document(
        self,
        *,
        file_search_store_name: str,
        document: GeminiDocument,
        document_entry: DocumentRegistryEntry,
        existing_document_name: str = "",
    ) -> str:
        if existing_document_name:
            self.gateway.delete_store_document(document_name=existing_document_name)
        return self.gateway.upload_document_to_store(
            file_search_store_name=file_search_store_name,
            document=document,
            custom_metadata={
                "patient_id": document_entry.patient_id,
                "filename": document_entry.filename,
                "document_type": document_entry.document_type,
                "document_date": document_entry.document_date,
                "drive_file_id": document_entry.drive_file_id,
            },
        )

    def upsert_drive_pdf_document(
        self,
        *,
        file_search_store_name: str,
        drive_gateway: GoogleDrivePdfGateway,
        document_entry: DocumentRegistryEntry,
        existing_document_name: str = "",
    ) -> str:
        """Downloads a Drive PDF by document_entry.drive_file_id and uploads it to File Search Store."""
        if not document_entry.drive_file_id:
            raise GoogleDrivePdfError("document_entry.drive_file_id가 비어 있습니다.")
        document = drive_gateway.download_pdf(
            document_entry.drive_file_id,
            output_name=document_entry.filename,
        )
        return self.upsert_document(
            file_search_store_name=file_search_store_name,
            document=document,
            document_entry=document_entry,
            existing_document_name=existing_document_name,
        )

    def upload_drive_pdf_sources(
        self,
        *,
        file_search_store_name: str,
        drive_gateway: GoogleDrivePdfGateway,
        drive_file_ids_or_urls: list[str],
        patient_id: str = "",
        document_type: str = "PDF",
        document_date: str = "",
    ) -> list[str]:
        """Convenience helper for uploading multiple Drive PDF URLs or IDs without registry objects."""
        document_names: list[str] = []
        existing_documents = self.gateway.list_store_documents(
            file_search_store_name=file_search_store_name
        )
        existing_by_drive_id = {
            item.custom_metadata.get("drive_file_id", ""): item.name
            for item in existing_documents
            if item.custom_metadata.get("drive_file_id")
        }

        for source in drive_file_ids_or_urls:
            drive_file_id = extract_google_drive_id(source)
            document = drive_gateway.download_pdf(source)
            existing_document_name = existing_by_drive_id.get(drive_file_id, "")
            if existing_document_name:
                self.gateway.delete_store_document(document_name=existing_document_name)
            document_name = self.gateway.upload_document_to_store(
                file_search_store_name=file_search_store_name,
                document=document,
                custom_metadata={
                    "patient_id": patient_id,
                    "filename": document.name,
                    "document_type": document_type,
                    "document_date": document_date,
                    "drive_file_id": drive_file_id,
                },
            )
            document_names.append(document_name)
        return document_names

    def upload_drive_folder_pdfs(
        self,
        *,
        file_search_store_name: str,
        drive_gateway: GoogleDrivePdfGateway,
        folder_id_or_url: str,
        patient_id: str = "",
        document_type: str = "PDF",
        document_date: str = "",
        include_google_workspace_exports: bool = False,
    ) -> list[str]:
        references = drive_gateway.list_pdf_files_in_folder(
            folder_id_or_url,
            include_google_workspace_exports=include_google_workspace_exports,
        )
        return self.upload_drive_pdf_sources(
            file_search_store_name=file_search_store_name,
            drive_gateway=drive_gateway,
            drive_file_ids_or_urls=[reference.file_id for reference in references],
            patient_id=patient_id,
            document_type=document_type,
            document_date=document_date,
        )

    def list_documents(self, *, file_search_store_name: str) -> list[FileSearchStoreDocument]:
        return self.gateway.list_store_documents(file_search_store_name=file_search_store_name)

    def delete_document(self, *, document_name: str) -> None:
        self.gateway.delete_store_document(document_name=document_name)


def build_default_google_drive_pdf_gateway(
    *,
    service_account_file: str = "",
    delegated_user: str = "",
) -> GoogleDrivePdfGateway:
    credentials_file = service_account_file or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    if credentials_file:
        return GoogleDrivePdfGateway.from_service_account_file(
            credentials_file,
            delegated_user=delegated_user,
        )
    return GoogleDrivePdfGateway.from_adc()


def build_default_gemini_qa_service(settings: Settings) -> GeminiQaService:
    if not settings.gemini_api_key:
        raise GeminiQaError("Gemini API key is not configured.")
    return GeminiQaService(
        settings=settings,
        gateway=GoogleGeminiGateway(settings.gemini_api_key),
    )


def build_default_gemini_patient_store_sync_service(
    settings: Settings,
) -> GeminiPatientStoreSyncService:
    if not settings.gemini_api_key:
        raise GeminiQaError("Gemini API key is not configured.")
    return GeminiPatientStoreSyncService(
        settings=settings,
        gateway=GoogleGeminiGateway(settings.gemini_api_key),
    )
