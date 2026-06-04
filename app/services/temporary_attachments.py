from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import Settings
from app.repositories import MedicalRepository
from app.schemas import ActiveAttachment, GeminiFileRuntime, UploadToken
from app.services.gemini_files_qa import (
    GeminiFileNotReadyError,
    GeminiFilesGateway,
    GoogleGeminiFilesGateway,
    PreparedGeminiFile,
)
from app.services.medical_wiki import KST, now_kst_iso


ALLOWED_UPLOAD_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
}

TEMPORARY_ATTACHMENT_SOURCE_LABEL = "업로드한 파일"


class TemporaryAttachmentError(Exception):
    pass


class TemporaryAttachmentConfigError(TemporaryAttachmentError):
    pass


class TemporaryAttachmentTokenError(TemporaryAttachmentError):
    pass


class TemporaryAttachmentValidationError(TemporaryAttachmentError):
    pass


@dataclass(frozen=True)
class UploadLink:
    token_id: str
    token: str
    url: str
    expires_at: str


@dataclass(frozen=True)
class TemporaryAttachmentCleanupResult:
    ok: bool
    deleted_count: int = 0
    error_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "deleted_count": self.deleted_count,
            "error_count": self.error_count,
        }


@dataclass
class TemporaryAttachmentService:
    settings: Settings
    repository: MedicalRepository
    gateway: GeminiFilesGateway | None = None

    def create_upload_link(self, *, patient_id: str, kakao_user_id_hash: str) -> UploadLink:
        self._require_upload_config()
        token_id = f"UPTOK_{uuid.uuid4().hex[:24].upper()}"
        token = self._encode_token(token_id)
        now = datetime.now(KST)
        expires_at = (now + timedelta(minutes=self.settings.upload_token_ttl_minutes)).isoformat()
        self.repository.upsert_upload_token(
            UploadToken(
                token_id=token_id,
                patient_id=patient_id,
                kakao_user_id_hash=kakao_user_id_hash,
                status="PENDING",
                expires_at=expires_at,
                created_at=now.isoformat(),
                updated_at=now.isoformat(),
            )
        )
        base_url = (self.settings.upload_base_url or "").rstrip("/")
        return UploadLink(
            token_id=token_id,
            token=token,
            url=f"{base_url}/upload/{token}",
            expires_at=expires_at,
        )

    def validate_upload_token(self, token: str) -> UploadToken:
        self._require_upload_config()
        token_id = self._decode_token_id(token)
        stored = self.repository.get_upload_token(token_id)
        if stored is None or stored.status != "PENDING":
            raise TemporaryAttachmentTokenError("upload token is not active")
        if self._is_expired(stored.expires_at):
            self.repository.upsert_upload_token(
                stored.model_copy(update={"status": "EXPIRED", "updated_at": now_kst_iso()})
            )
            raise TemporaryAttachmentTokenError("upload token expired")
        return stored

    def get_completed_upload_attachment(self, *, token: str) -> ActiveAttachment | None:
        self._require_upload_config()
        token_id = self._decode_token_id(token)
        stored = self.repository.get_upload_token(token_id)
        if stored is None or stored.status != "USED":
            return None
        attachment = self.get_current_attachment(
            patient_id=stored.patient_id,
            kakao_user_id_hash=stored.kakao_user_id_hash,
        )
        if attachment is None or attachment.upload_token_id != stored.token_id:
            return None
        return attachment

    def upload_file(
        self,
        *,
        token: str,
        content: bytes,
        content_type: str,
    ) -> ActiveAttachment:
        upload_token = self.validate_upload_token(token)
        mime_type = self._validate_file(content=content, content_type=content_type)

        uploaded_file = self._gateway().upload_file(
            display_name="temporary-upload",
            content=content,
            mime_type=mime_type,
        )
        file_object = self._wait_for_active_file(uploaded_file)
        token_used = self.repository.mark_upload_token_used(upload_token.token_id, now=now_kst_iso())
        if token_used is None:
            self._delete_file_best_effort(file_object)
            raise TemporaryAttachmentTokenError("upload token was already used")

        previous = self.repository.get_active_attachment(
            upload_token.patient_id,
            upload_token.kakao_user_id_hash,
        )
        if previous is not None and previous.gemini_file.file_name:
            self._delete_file_name_best_effort(previous.gemini_file.file_name)

        now = datetime.now(KST)
        attachment = ActiveAttachment(
            attachment_id=f"ATT_{uuid.uuid4().hex[:24].upper()}",
            upload_token_id=upload_token.token_id,
            patient_id=upload_token.patient_id,
            kakao_user_id_hash=upload_token.kakao_user_id_hash,
            gemini_file=GeminiFileRuntime(
                file_name=self._file_name(file_object),
                uri=getattr(file_object, "uri", "") or "",
                mime_type=getattr(file_object, "mime_type", "") or mime_type,
                state="ACTIVE",
                expiration_time=self._file_expiration_time(file_object),
                uploaded_at=now.isoformat(),
                last_checked_at=now.isoformat(),
            ),
            mime_type=mime_type,
            file_size_bytes=len(content),
            status="ACTIVE",
            expires_at=(now + timedelta(minutes=self.settings.chat_attachment_ttl_minutes)).isoformat(),
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
        self.repository.upsert_active_attachment(attachment)
        return attachment

    def get_current_attachment(self, *, patient_id: str, kakao_user_id_hash: str) -> ActiveAttachment | None:
        attachment = self.repository.get_active_attachment(patient_id, kakao_user_id_hash)
        if attachment is None or attachment.status != "ACTIVE":
            return None
        if self._is_expired(attachment.expires_at):
            self._delete_attachment(attachment)
            return None
        return attachment

    def prepare_attachment_file(self, *, attachment: ActiveAttachment) -> PreparedGeminiFile:
        if attachment.status != "ACTIVE" or self._is_expired(attachment.expires_at):
            raise GeminiFileNotReadyError("temporary attachment expired")
        file_name = attachment.gemini_file.file_name
        if not file_name:
            raise GeminiFileNotReadyError("temporary attachment file is missing")
        file_object = self._gateway().get_file(file_name=file_name)
        state = self._normalize_file_state(file_object)
        if state != "ACTIVE":
            raise GeminiFileNotReadyError("temporary attachment is not active")
        return PreparedGeminiFile(
            source_id=attachment.attachment_id,
            file_name=file_name,
            uri=getattr(file_object, "uri", "") or attachment.gemini_file.uri,
            mime_type=getattr(file_object, "mime_type", "") or attachment.mime_type,
            file_object=file_object,
            source_kind="temporary_attachment",
        )

    def cleanup_expired(self, *, limit: int = 20) -> TemporaryAttachmentCleanupResult:
        now = now_kst_iso()
        deleted_count = 0
        error_count = 0
        for attachment in self.repository.list_expired_active_attachments(now=now, limit=limit):
            try:
                self._delete_attachment(attachment)
                deleted_count += 1
            except Exception:
                error_count += 1
        return TemporaryAttachmentCleanupResult(ok=error_count == 0, deleted_count=deleted_count, error_count=error_count)

    def _wait_for_active_file(self, file_object: Any) -> Any:
        file_name = self._file_name(file_object)
        if not file_name:
            raise TemporaryAttachmentError("Gemini Files API upload result has no file name")
        state = self._normalize_file_state(file_object)
        if state == "ACTIVE":
            return file_object
        deadline = datetime.now(timezone.utc).timestamp() + self.settings.file_ready_wait_seconds
        latest = file_object
        while datetime.now(timezone.utc).timestamp() <= deadline:
            latest = self._gateway().get_file(file_name=file_name)
            state = self._normalize_file_state(latest)
            if state == "ACTIVE":
                return latest
            if state in {"FAILED", "EXPIRED"}:
                break
            time.sleep(self.settings.file_ready_poll_interval_seconds)
        self._delete_file_name_best_effort(file_name)
        raise GeminiFileNotReadyError("temporary attachment did not become active")

    def _validate_file(self, *, content: bytes, content_type: str) -> str:
        if not content:
            raise TemporaryAttachmentValidationError("empty upload")
        if len(content) > self.settings.max_upload_file_bytes:
            raise TemporaryAttachmentValidationError("upload file is too large")
        detected = self._detect_mime_type(content)
        declared = (content_type or "").split(";")[0].strip().lower()
        mime_type = detected or declared
        if mime_type not in ALLOWED_UPLOAD_MIME_TYPES:
            raise TemporaryAttachmentValidationError("unsupported upload file type")
        if declared and declared in ALLOWED_UPLOAD_MIME_TYPES and detected and declared != detected:
            raise TemporaryAttachmentValidationError("upload file type does not match content")
        return mime_type

    def _detect_mime_type(self, content: bytes) -> str:
        if content.startswith(b"%PDF"):
            return "application/pdf"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            return "image/webp"
        return ""

    def _encode_token(self, token_id: str) -> str:
        signature = hmac.new(
            self._secret_bytes(),
            token_id.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
        return f"{token_id}.{encoded_signature}"

    def _decode_token_id(self, token: str) -> str:
        try:
            token_id, signature = token.split(".", 1)
        except ValueError as exc:
            raise TemporaryAttachmentTokenError("invalid upload token") from exc
        if not token_id or not signature:
            raise TemporaryAttachmentTokenError("invalid upload token")
        expected = self._encode_token(token_id)
        if not hmac.compare_digest(token, expected):
            raise TemporaryAttachmentTokenError("invalid upload token")
        return token_id

    def _require_upload_config(self) -> None:
        if not self.settings.upload_token_secret or not self.settings.upload_base_url:
            raise TemporaryAttachmentConfigError("temporary upload is not configured")

    def _gateway(self) -> GeminiFilesGateway:
        if self.gateway is None:
            if not self.settings.gemini_api_key:
                raise TemporaryAttachmentConfigError("Gemini API key is not configured")
            self.gateway = GoogleGeminiFilesGateway(
                self.settings.gemini_api_key,
                timeout_ms=self.settings.gemini_http_timeout_ms,
            )
        return self.gateway

    def _secret_bytes(self) -> bytes:
        secret = self.settings.upload_token_secret or ""
        if not secret:
            raise TemporaryAttachmentConfigError("temporary upload token secret is not configured")
        return secret.encode("utf-8")

    def _delete_attachment(self, attachment: ActiveAttachment) -> None:
        if attachment.gemini_file.file_name:
            self._delete_file_name_best_effort(attachment.gemini_file.file_name)
        self.repository.delete_active_attachment(attachment.patient_id, attachment.kakao_user_id_hash)

    def _delete_file_best_effort(self, file_object: Any) -> None:
        file_name = self._file_name(file_object)
        if file_name:
            self._delete_file_name_best_effort(file_name)

    def _delete_file_name_best_effort(self, file_name: str) -> None:
        try:
            self._gateway().delete_file(file_name=file_name)
        except Exception:
            pass

    def _normalize_file_state(self, file_object: Any) -> str:
        raw = getattr(file_object, "state", "") or ""
        if hasattr(raw, "name"):
            raw = raw.name
        value = str(raw).split(".")[-1].upper()
        if value in {"ACTIVE", "UPLOADING", "PROCESSING", "EXPIRED", "FAILED"}:
            return value
        return "PROCESSING"

    def _file_name(self, file_object: Any) -> str:
        return str(getattr(file_object, "name", "") or getattr(file_object, "file_name", "") or "")

    def _file_expiration_time(self, file_object: Any) -> str:
        value = getattr(file_object, "expiration_time", "") or ""
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    def _is_expired(self, value: str) -> bool:
        if not value:
            return True
        try:
            return datetime.fromisoformat(value) <= datetime.now(KST)
        except ValueError:
            return True


def build_default_temporary_attachment_service(
    *,
    settings: Settings,
    repository: MedicalRepository,
) -> TemporaryAttachmentService:
    return TemporaryAttachmentService(
        settings=settings,
        repository=repository,
    )
