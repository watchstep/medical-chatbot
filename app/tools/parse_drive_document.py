from __future__ import annotations

import argparse
import json
import logging
from io import BytesIO
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from app.config import Settings
from app.services.document_parsing import (
    DocumentParsingError,
    PDF_MIME_TYPE,
    ParsingDocument,
    build_default_document_parsing_service,
    default_parsed_markdown_path,
    extract_drive_file_id,
    parsing_result_metadata,
    write_parsed_markdown,
    write_parsing_metadata,
)

logger = logging.getLogger(__name__)

DRIVE_READONLY_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
GOOGLE_APPS_MIME_PREFIX = "application/vnd.google-apps."
GOOGLE_DOC_EXPORT_MIME_TYPE = PDF_MIME_TYPE
DEFAULT_OUTPUT_BASE_DIR = "artifacts/parsing"
DEFAULT_TOKEN_PATH = "token.json"
DEFAULT_OAUTH_CLIENT_SECRET_PATH = "credentials.json"
DEFAULT_MARKDOWN_FILENAME = "parsed.md"


class DriveDocumentParsingError(DocumentParsingError):
    pass


def build_drive_client(
    *,
    oauth_client_secret_path: str | Path | None = DEFAULT_OAUTH_CLIENT_SECRET_PATH,
    token_path: str | Path | None = DEFAULT_TOKEN_PATH,
    service_account_path: str | Path | None = None,
) -> Any:
    """Google Drive readonly client를 생성한다.

    우선순위는 service account, OAuth token, OAuth browser login 순서다.
    서버 환경에서는 service_account_path 사용을 권장한다.
    로컬 테스트에서는 credentials.json과 token.json을 사용할 수 있다.
    """

    credentials = load_drive_credentials(
        oauth_client_secret_path=oauth_client_secret_path,
        token_path=token_path,
        service_account_path=service_account_path,
    )
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def load_drive_credentials(
    *,
    oauth_client_secret_path: str | Path | None = DEFAULT_OAUTH_CLIENT_SECRET_PATH,
    token_path: str | Path | None = DEFAULT_TOKEN_PATH,
    service_account_path: str | Path | None = None,
) -> Credentials | ServiceAccountCredentials:
    if service_account_path:
        service_account_file = Path(service_account_path)
        if not service_account_file.exists():
            raise DriveDocumentParsingError(
                f"Google service account 파일을 찾을 수 없습니다: {service_account_file}"
            )
        return ServiceAccountCredentials.from_service_account_file(
            service_account_file,
            scopes=DRIVE_READONLY_SCOPES,
        )

    token_file = Path(token_path) if token_path else None
    credentials: Credentials | None = None
    if token_file and token_file.exists():
        credentials = Credentials.from_authorized_user_file(
            str(token_file),
            DRIVE_READONLY_SCOPES,
        )

    if credentials and credentials.valid:
        return credentials

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        if token_file:
            token_file.write_text(credentials.to_json(), encoding="utf-8")
        return credentials

    if not oauth_client_secret_path:
        raise DriveDocumentParsingError(
            "OAuth client secret 경로가 없어서 Google Drive 인증을 진행할 수 없습니다."
        )

    client_secret_file = Path(oauth_client_secret_path)
    if not client_secret_file.exists():
        raise DriveDocumentParsingError(
            f"OAuth client secret 파일을 찾을 수 없습니다: {client_secret_file}"
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(client_secret_file),
        DRIVE_READONLY_SCOPES,
    )
    credentials = flow.run_local_server(port=0)
    if token_file:
        token_file.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def download_drive_pdf(
    *,
    drive_client: Any,
    file_id_or_url: str,
) -> ParsingDocument:
    """Google Drive 파일을 PDF bytes로 내려받아 ParsingDocument로 반환한다.

    Google Docs, Sheets, Slides 계열 파일은 PDF로 export한다.
    일반 Drive 파일은 application/pdf인 경우만 다운로드한다.
    """

    file_id = extract_drive_file_id(file_id_or_url)
    metadata = get_drive_file_metadata(drive_client=drive_client, file_id=file_id)
    name = metadata.get("name") or f"drive-{file_id}.pdf"
    mime_type = metadata.get("mimeType") or ""

    if mime_type.startswith(GOOGLE_APPS_MIME_PREFIX):
        content = export_google_workspace_file_as_pdf(
            drive_client=drive_client,
            file_id=file_id,
        )
        output_name = ensure_pdf_suffix(name)
    elif mime_type == PDF_MIME_TYPE or name.lower().endswith(".pdf"):
        content = download_binary_drive_file(
            drive_client=drive_client,
            file_id=file_id,
        )
        output_name = ensure_pdf_suffix(name)
    else:
        raise DriveDocumentParsingError(
            "지원하지 않는 Google Drive 파일 형식입니다. "
            f"PDF 또는 Google Workspace 문서만 처리할 수 있습니다: {mime_type}"
        )

    if not content.startswith(b"%PDF"):
        raise DriveDocumentParsingError(
            f"다운로드된 파일이 PDF 바이트로 보이지 않습니다: {output_name}"
        )

    return ParsingDocument(
        name=output_name,
        content=content,
        mime_type=PDF_MIME_TYPE,
        source_id=file_id,
    )


def get_drive_file_metadata(*, drive_client: Any, file_id: str) -> dict[str, Any]:
    return (
        drive_client.files()
        .get(fileId=file_id, fields="id,name,mimeType,modifiedTime,size")
        .execute()
    )


def download_binary_drive_file(*, drive_client: Any, file_id: str) -> bytes:
    request = drive_client.files().get_media(fileId=file_id)
    return download_request_to_bytes(request)


def export_google_workspace_file_as_pdf(*, drive_client: Any, file_id: str) -> bytes:
    request = drive_client.files().export_media(
        fileId=file_id,
        mimeType=GOOGLE_DOC_EXPORT_MIME_TYPE,
    )
    return download_request_to_bytes(request)


def download_request_to_bytes(request: Any) -> bytes:
    buffer = BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
        if status:
            logger.info("Google Drive download progress %.1f%%", status.progress() * 100)
    return buffer.getvalue()


def ensure_pdf_suffix(name: str) -> str:
    return name if name.lower().endswith(".pdf") else f"{name}.pdf"


def validate_output_filename(filename: str | None, *, default: str | None = None) -> str:
    """저장 파일명만 허용한다. 디렉터리 경로가 섞이면 실수를 막기 위해 거부한다."""

    normalized = (filename or default or "").strip()
    if not normalized:
        raise DriveDocumentParsingError("저장 파일명이 비어 있습니다.")

    path = Path(normalized)
    if path.name != normalized or path.is_absolute():
        raise DriveDocumentParsingError(
            f"저장 파일명에는 디렉터리 경로를 포함할 수 없습니다: {filename}"
        )
    if normalized in {".", ".."}:
        raise DriveDocumentParsingError(f"저장 파일명이 올바르지 않습니다: {filename}")
    return normalized


def build_metadata_filename_from_markdown(markdown_filename: str) -> str:
    """Markdown 파일명을 기준으로 metadata 파일명을 만든다.

    예: parsed.md -> parsed_metadata.json, result.markdown -> result_metadata.json
    """

    markdown_path = Path(markdown_filename)
    stem = markdown_path.stem or markdown_filename
    return f"{stem}_metadata.json"


def parse_drive_document(
    *,
    file_id_or_url: str,
    settings: Settings,
    output_base_dir: str | Path = DEFAULT_OUTPUT_BASE_DIR,
    markdown_filename: str = DEFAULT_MARKDOWN_FILENAME,
    metadata_filename: str | None = None,
    model: str | None = None,
    max_output_tokens: int | None = None,
    oauth_client_secret_path: str | Path | None = DEFAULT_OAUTH_CLIENT_SECRET_PATH,
    token_path: str | Path | None = DEFAULT_TOKEN_PATH,
    service_account_path: str | Path | None = None,
) -> tuple[Path, Path]:
    drive_client = build_drive_client(
        oauth_client_secret_path=oauth_client_secret_path,
        token_path=token_path,
        service_account_path=service_account_path,
    )
    document = download_drive_pdf(
        drive_client=drive_client,
        file_id_or_url=file_id_or_url,
    )

    parsing_service = build_default_document_parsing_service(settings)
    result = parsing_service.parse_document(
        document=document,
        model=model,
        max_output_tokens=max_output_tokens,
    )

    validated_markdown_filename = validate_output_filename(
        markdown_filename,
        default=DEFAULT_MARKDOWN_FILENAME,
    )
    markdown_path = default_parsed_markdown_path(
        source_id=document.source_id,
        base_dir=str(output_base_dir),
    ).with_name(validated_markdown_filename)

    resolved_metadata_filename = metadata_filename or build_metadata_filename_from_markdown(
        validated_markdown_filename
    )
    metadata_path = markdown_path.with_name(
        validate_output_filename(resolved_metadata_filename)
    )

    write_parsed_markdown(markdown_path, result.markdown)
    write_parsing_metadata(
        metadata_path,
        {
            "source_id": document.source_id,
            "source_name": document.name,
            **parsing_result_metadata(result),
        },
    )
    return markdown_path, metadata_path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Google Drive PDF 문서를 다운로드한 뒤 이미지 렌더링 기반 Gemini Markdown 파싱을 수행합니다."
    )
    parser.add_argument(
        "file_id_or_url",
        help="Google Drive file id 또는 공유 URL",
    )
    parser.add_argument(
        "--output-base-dir",
        default=DEFAULT_OUTPUT_BASE_DIR,
        help=f"파싱 결과 저장 base directory. 기본값: {DEFAULT_OUTPUT_BASE_DIR}",
    )
    parser.add_argument(
        "--markdown-filename",
        default=DEFAULT_MARKDOWN_FILENAME,
        help=f"저장할 Markdown 파일명. 기본값: {DEFAULT_MARKDOWN_FILENAME}",
    )
    parser.add_argument(
        "--metadata-filename",
        default=None,
        help=(
            "저장할 metadata JSON 파일명. "
            "미지정 시 Markdown 파일명을 기준으로 {파일명}_metadata.json 형식으로 저장합니다."
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help="기본 Settings 값을 덮어쓸 Gemini model 이름",
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        help="기본 Settings 값을 덮어쓸 max_output_tokens",
    )
    parser.add_argument(
        "--oauth-client-secret-path",
        default=DEFAULT_OAUTH_CLIENT_SECRET_PATH,
        help=f"OAuth client secret JSON 경로. 기본값: {DEFAULT_OAUTH_CLIENT_SECRET_PATH}",
    )
    parser.add_argument(
        "--token-path",
        default=DEFAULT_TOKEN_PATH,
        help=f"OAuth token JSON 저장 경로. 기본값: {DEFAULT_TOKEN_PATH}",
    )
    parser.add_argument(
        "--service-account-path",
        default=None,
        help="Service account JSON 경로. 지정하면 OAuth 대신 service account를 사용합니다.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = Settings()
    markdown_path, metadata_path = parse_drive_document(
        file_id_or_url=args.file_id_or_url,
        settings=settings,
        output_base_dir=args.output_base_dir,
        markdown_filename=args.markdown_filename,
        metadata_filename=args.metadata_filename,
        model=args.model,
        max_output_tokens=args.max_output_tokens,
        oauth_client_secret_path=args.oauth_client_secret_path,
        token_path=args.token_path,
        service_account_path=args.service_account_path,
    )

    print(
        json.dumps(
            {
                "markdown_path": str(markdown_path),
                "metadata_path": str(metadata_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()