from __future__ import annotations

import io
import logging
from dataclasses import dataclass

import google.auth
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from app.config import Settings


logger = logging.getLogger(__name__)
GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
GOOGLE_DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def _escape_drive_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class DriveLookupError(Exception):
    def __init__(self, message: str, *, detail: str | None = None):
        super().__init__(message)
        self.detail = detail


class DriveGateway:
    def list_files_in_folder(self, folder_id: str) -> list[dict]:
        raise NotImplementedError

    def list_child_folders(self, folder_id: str) -> list[dict]:
        raise NotImplementedError

    def find_folders_by_name(self, folder_name: str, *, parent_id: str | None = None) -> list[dict]:
        raise NotImplementedError

    def find_files_by_name(
        self,
        file_name: str,
        *,
        parent_id: str | None = None,
        mime_type: str | None = None,
    ) -> list[dict]:
        raise NotImplementedError

    def create_folder(self, folder_name: str, *, parent_id: str | None = None) -> dict:
        raise NotImplementedError

    def upload_file_bytes(
        self,
        *,
        parent_id: str,
        file_name: str,
        content: bytes,
        mime_type: str,
    ) -> dict:
        raise NotImplementedError

    def update_file_bytes(
        self,
        *,
        file_id: str,
        content: bytes,
        mime_type: str,
    ) -> dict:
        raise NotImplementedError

    def download_file_bytes(self, file_id: str) -> bytes:
        raise NotImplementedError

    def download_file_to_path(self, file_id: str, destination_path: str) -> None:
        with open(destination_path, "wb") as destination:
            destination.write(self.download_file_bytes(file_id))

    def get_file(self, file_id: str) -> dict:
        raise NotImplementedError

    def get_start_page_token(self) -> str:
        raise NotImplementedError

    def list_changes(self, page_token: str, *, page_size: int) -> dict:
        raise NotImplementedError


class GoogleDriveGateway(DriveGateway):
    def __init__(self, credentials_path: str | None = None):
        scopes = [GOOGLE_DRIVE_SCOPE]
        if credentials_path:
            credentials = ServiceAccountCredentials.from_service_account_file(
                credentials_path,
                scopes=scopes,
            )
        else:
            credentials, _ = google.auth.default(scopes=scopes)
        self.service = build("drive", "v3", credentials=credentials, cache_discovery=False)

    def list_files_in_folder(self, folder_id: str) -> list[dict]:
        try:
            files: list[dict] = []
            page_token: str | None = None
            while True:
                response = (
                    self.service.files()
                    .list(
                        q=f"'{_escape_drive_query_value(folder_id)}' in parents and trashed = false",
                        fields="nextPageToken,files(id,name,mimeType,modifiedTime,parents,size,md5Checksum,trashed)",
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                        pageToken=page_token,
                    )
                    .execute()
                )
                files.extend(response.get("files", []))
                page_token = response.get("nextPageToken")
                if not page_token:
                    return files
        except HttpError as exc:
            logger.exception("Google Drive list_files_in_folder failed")
            raise DriveLookupError(
                "Google Drive 폴더 파일 목록 조회에 실패했습니다.",
                detail=str(exc),
            ) from exc

    def list_child_folders(self, folder_id: str) -> list[dict]:
        try:
            folders: list[dict] = []
            page_token: str | None = None
            while True:
                response = (
                    self.service.files()
                    .list(
                        q=(
                            f"'{_escape_drive_query_value(folder_id)}' in parents and "
                            f"mimeType = '{GOOGLE_DRIVE_FOLDER_MIME_TYPE}' and trashed = false"
                        ),
                        fields="nextPageToken,files(id,name,mimeType,modifiedTime,parents,trashed)",
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                        pageToken=page_token,
                    )
                    .execute()
                )
                folders.extend(response.get("files", []))
                page_token = response.get("nextPageToken")
                if not page_token:
                    return folders
        except HttpError as exc:
            logger.exception("Google Drive list_child_folders failed")
            raise DriveLookupError(
                "Google Drive 하위 폴더 목록 조회에 실패했습니다.",
                detail=str(exc),
            ) from exc

    def find_folders_by_name(self, folder_name: str, *, parent_id: str | None = None) -> list[dict]:
        escaped_name = _escape_drive_query_value(folder_name)
        clauses = [
            f"mimeType = '{GOOGLE_DRIVE_FOLDER_MIME_TYPE}'",
            "trashed = false",
            f"name = '{escaped_name}'",
        ]
        if parent_id:
            clauses.append(f"'{_escape_drive_query_value(parent_id)}' in parents")
        query = " and ".join(clauses)
        try:
            folders: list[dict] = []
            page_token: str | None = None
            while True:
                response = (
                    self.service.files()
                    .list(
                        q=query,
                        fields="nextPageToken,files(id,name,mimeType,modifiedTime,parents,trashed)",
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                        pageToken=page_token,
                    )
                    .execute()
                )
                folders.extend(response.get("files", []))
                page_token = response.get("nextPageToken")
                if not page_token:
                    return folders
        except HttpError as exc:
            logger.exception("Google Drive find_folders_by_name failed")
            raise DriveLookupError(
                "Google Drive 폴더 이름 검색에 실패했습니다.",
                detail=str(exc),
            ) from exc

    def find_files_by_name(
        self,
        file_name: str,
        *,
        parent_id: str | None = None,
        mime_type: str | None = None,
    ) -> list[dict]:
        escaped_name = _escape_drive_query_value(file_name)
        clauses = [
            "trashed = false",
            f"name = '{escaped_name}'",
        ]
        if parent_id:
            clauses.append(f"'{_escape_drive_query_value(parent_id)}' in parents")
        if mime_type:
            clauses.append(f"mimeType = '{_escape_drive_query_value(mime_type)}'")
        query = " and ".join(clauses)
        try:
            files: list[dict] = []
            page_token: str | None = None
            while True:
                response = (
                    self.service.files()
                    .list(
                        q=query,
                        fields="nextPageToken,files(id,name,mimeType,parents,trashed,webViewLink)",
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                        pageToken=page_token,
                    )
                    .execute()
                )
                files.extend(response.get("files", []))
                page_token = response.get("nextPageToken")
                if not page_token:
                    return files
        except HttpError as exc:
            logger.exception("Google Drive find_files_by_name failed")
            raise DriveLookupError(
                "Google Drive 파일 이름 검색에 실패했습니다.",
                detail=str(exc),
            ) from exc

    def create_folder(self, folder_name: str, *, parent_id: str | None = None) -> dict:
        metadata: dict[str, object] = {
            "name": folder_name,
            "mimeType": GOOGLE_DRIVE_FOLDER_MIME_TYPE,
        }
        if parent_id:
            metadata["parents"] = [parent_id]
        try:
            return (
                self.service.files()
                .create(
                    body=metadata,
                    fields="id,name,mimeType,parents,trashed,webViewLink",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            logger.exception("Google Drive create_folder failed")
            raise DriveLookupError(
                "Google Drive 폴더 생성에 실패했습니다.",
                detail=str(exc),
            ) from exc

    def upload_file_bytes(
        self,
        *,
        parent_id: str,
        file_name: str,
        content: bytes,
        mime_type: str,
    ) -> dict:
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
        metadata = {"name": file_name, "parents": [parent_id]}
        try:
            return (
                self.service.files()
                .create(
                    body=metadata,
                    media_body=media,
                    fields="id,name,mimeType,parents,trashed,webViewLink",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            logger.exception("Google Drive upload_file_bytes failed")
            raise DriveLookupError(
                "Google Drive 파일 업로드에 실패했습니다.",
                detail=str(exc),
            ) from exc

    def update_file_bytes(
        self,
        *,
        file_id: str,
        content: bytes,
        mime_type: str,
    ) -> dict:
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
        try:
            return (
                self.service.files()
                .update(
                    fileId=file_id,
                    media_body=media,
                    fields="id,name,mimeType,parents,trashed,webViewLink",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            logger.exception("Google Drive update_file_bytes failed")
            raise DriveLookupError(
                "Google Drive 파일 업데이트에 실패했습니다.",
                detail=str(exc),
            ) from exc

    def download_file_bytes(self, file_id: str) -> bytes:
        try:
            request = self.service.files().get_media(fileId=file_id)
            buffer = io.BytesIO()
            downloader = MediaIoBaseDownload(buffer, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        except HttpError as exc:
            logger.exception("Google Drive download_file_bytes failed")
            raise DriveLookupError(
                "Google Drive 파일 다운로드에 실패했습니다.",
                detail=str(exc),
            ) from exc
        return buffer.getvalue()

    def download_file_to_path(self, file_id: str, destination_path: str) -> None:
        try:
            request = self.service.files().get_media(fileId=file_id)
            with open(destination_path, "wb") as destination:
                downloader = MediaIoBaseDownload(destination, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
        except HttpError as exc:
            logger.exception("Google Drive download_file_to_path failed")
            raise DriveLookupError(
                "Google Drive 파일 다운로드에 실패했습니다.",
                detail=str(exc),
            ) from exc

    def get_file(self, file_id: str) -> dict:
        try:
            return (
                self.service.files()
                .get(
                    fileId=file_id,
                    fields="id,name,mimeType,parents,modifiedTime,size,md5Checksum,trashed,webViewLink",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            logger.exception("Google Drive get_file failed")
            raise DriveLookupError(
                "Google Drive 파일 조회에 실패했습니다.",
                detail=str(exc),
            ) from exc

    def get_start_page_token(self) -> str:
        try:
            response = self.service.changes().getStartPageToken(supportsAllDrives=True).execute()
            return response["startPageToken"]
        except HttpError as exc:
            logger.exception("Google Drive get_start_page_token failed")
            raise DriveLookupError(
                "Google Drive Changes API page token 조회에 실패했습니다.",
                detail=str(exc),
            ) from exc

    def list_changes(self, page_token: str, *, page_size: int) -> dict:
        try:
            return (
                self.service.changes()
                .list(
                    pageToken=page_token,
                    pageSize=page_size,
                    fields=(
                        "nextPageToken,newStartPageToken,"
                        "changes(fileId,removed,file(id,name,mimeType,parents,modifiedTime,size,md5Checksum,trashed))"
                    ),
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                    includeRemoved=True,
                )
                .execute()
            )
        except HttpError as exc:
            logger.exception("Google Drive list_changes failed")
            raise DriveLookupError(
                "Google Drive Changes API 변경 목록 조회에 실패했습니다.",
                detail=str(exc),
            ) from exc


@dataclass
class DriveLookupService:
    settings: Settings
    gateway: DriveGateway


def build_default_drive_service(settings: Settings) -> DriveLookupService:
    credentials_path = (
        str(settings.google_service_account_path)
        if settings.google_service_account_path
        else None
    )
    return DriveLookupService(
        settings=settings,
        gateway=GoogleDriveGateway(credentials_path),
    )
