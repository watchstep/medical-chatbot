from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    google_service_account_path: Path = Path("credentials/google-service-account.json")
    drive_root_folder_name: str = "medical-chatbot"
    drive_system_folder_name: str = "_system"
    patient_index_file_name: str = "patient_index.json"
    document_registry_file_name: str = "document_registry.json"
    meta_file_name: str = "meta.json"
    session_ttl_minutes: int = 1440
    patient_index_cache_ttl_seconds: int = 300
    document_registry_cache_ttl_seconds: int = 300
    patient_record_cache_ttl_seconds: int = 600
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_context_cache_ttl_hours: int = 24

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
