from __future__ import annotations

from types import SimpleNamespace

from app.config import Settings
from app.services import admin_auth


def _request(headers: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(headers=headers)


def test_token_mode_accepts_valid_admin_token() -> None:
    settings = Settings(admin_auth_mode="token", admin_sync_token="secret")

    assert admin_auth.is_admin_request(_request({"x-admin-token": "secret"}), settings) is True


def test_token_mode_rejects_missing_configured_token() -> None:
    settings = Settings(admin_auth_mode="token", admin_sync_token=None)

    assert admin_auth.is_admin_request(_request({"x-admin-token": "secret"}), settings) is False


def test_oidc_mode_accepts_allowed_scheduler_email(monkeypatch) -> None:
    def fake_verify_google_id_token(*, token: str, audience: str) -> dict[str, str]:
        assert token == "fake-token"
        assert audience == "https://medical-chatbot.example.run.app"
        return {
            "iss": "https://accounts.google.com",
            "email": "scheduler@example-project.iam.gserviceaccount.com",
            "sub": "1234567890",
        }

    monkeypatch.setattr(admin_auth, "_verify_google_id_token", fake_verify_google_id_token)
    settings = Settings(
        admin_auth_mode="oidc",
        admin_oidc_audience="https://medical-chatbot.example.run.app",
        admin_oidc_allowed_emails="scheduler@example-project.iam.gserviceaccount.com",
    )

    assert (
        admin_auth.is_admin_request(
            _request({"authorization": "Bearer fake-token"}),
            settings,
        )
        is True
    )


def test_oidc_mode_rejects_disallowed_email(monkeypatch) -> None:
    def fake_verify_google_id_token(*, token: str, audience: str) -> dict[str, str]:
        return {
            "iss": "https://accounts.google.com",
            "email": "attacker@example-project.iam.gserviceaccount.com",
            "sub": "1234567890",
        }

    monkeypatch.setattr(admin_auth, "_verify_google_id_token", fake_verify_google_id_token)
    settings = Settings(
        admin_auth_mode="oidc",
        admin_oidc_audience="https://medical-chatbot.example.run.app",
        admin_oidc_allowed_emails="scheduler@example-project.iam.gserviceaccount.com",
    )

    assert (
        admin_auth.is_admin_request(
            _request({"authorization": "Bearer fake-token"}),
            settings,
        )
        is False
    )


def test_token_or_oidc_mode_accepts_token_fallback() -> None:
    settings = Settings(admin_auth_mode="token_or_oidc", admin_sync_token="secret")

    assert admin_auth.is_admin_request(_request({"x-admin-token": "secret"}), settings) is True


def test_oidc_mode_rejects_plain_admin_token() -> None:
    settings = Settings(
        admin_auth_mode="oidc",
        admin_sync_token="secret",
        admin_oidc_audience="https://medical-chatbot.example.run.app",
        admin_oidc_allowed_emails="scheduler@example-project.iam.gserviceaccount.com",
    )

    assert admin_auth.is_admin_request(_request({"x-admin-token": "secret"}), settings) is False
