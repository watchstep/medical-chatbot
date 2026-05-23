from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import Any

from fastapi import Request

from app.config import Settings

try:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token
except ImportError:  # pragma: no cover - dependency guard for minimal test envs
    google_requests = None  # type: ignore[assignment]
    id_token = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

GOOGLE_OIDC_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}


class AdminAuthError(Exception):
    pass


@dataclass(frozen=True)
class AdminPrincipal:
    email: str
    subject: str = ""


def is_admin_request(request: Request, settings: Settings) -> bool:
    """Return True only when the admin request satisfies the configured auth mode.

    This function is intentionally fail-closed. Missing env vars, malformed headers,
    invalid OIDC tokens, and emails outside the allowlist all return False.
    """

    mode = settings.admin_auth_mode

    if mode in {"token", "token_or_oidc"} and _valid_admin_token(request, settings):
        return True

    if mode == "token":
        return False

    if mode in {"oidc", "token_or_oidc"}:
        try:
            verify_admin_oidc_request(request, settings)
            return True
        except AdminAuthError as exc:
            logger.warning("admin oidc auth failed: %s", exc)
            return False

    return False


def verify_admin_oidc_request(request: Request, settings: Settings) -> AdminPrincipal:
    if not settings.admin_oidc_audience:
        raise AdminAuthError("admin oidc audience is not configured")

    allowed_emails = _parse_allowed_emails(settings.admin_oidc_allowed_emails)
    if not allowed_emails:
        raise AdminAuthError("admin oidc allowed emails are not configured")

    token = _bearer_token(request)
    claims = _verify_google_id_token(token=token, audience=settings.admin_oidc_audience)

    issuer = str(claims.get("iss") or "")
    if issuer not in GOOGLE_OIDC_ISSUERS:
        raise AdminAuthError("invalid oidc issuer")

    email = str(claims.get("email") or "")
    if not email or email not in allowed_emails:
        raise AdminAuthError("oidc email is not allowed")

    return AdminPrincipal(email=email, subject=str(claims.get("sub") or ""))


def _valid_admin_token(request: Request, settings: Settings) -> bool:
    if not settings.admin_sync_token:
        return False
    provided = request.headers.get("x-admin-token", "")
    if not provided:
        return False
    return secrets.compare_digest(provided, settings.admin_sync_token)


def _bearer_token(request: Request) -> str:
    auth_header = request.headers.get("authorization", "")
    scheme, _, value = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        raise AdminAuthError("missing bearer token")
    return value.strip()


def _verify_google_id_token(*, token: str, audience: str) -> dict[str, Any]:
    if google_requests is None or id_token is None:
        raise AdminAuthError("google-auth is not installed")
    try:
        claims = id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            audience=audience,
        )
    except Exception as exc:  # noqa: BLE001 - google-auth raises several concrete errors
        raise AdminAuthError("invalid oidc token") from exc
    if not isinstance(claims, dict):
        raise AdminAuthError("invalid oidc claims")
    return claims


def _parse_allowed_emails(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}
