from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    google_service_account_path: Path = Path("credentials/google-service-account.json")
    drive_root_folder_name: str = "medical-chatbot"
    drive_system_folder_name: str = "_system"
    patient_index_file_name: str = "patient_index.json"
    document_registry_file_name: str = "document_registry.json"
    session_ttl_minutes: int = 1440
    patient_index_cache_ttl_seconds: int = 300
    document_registry_cache_ttl_seconds: int = 300
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3-flash-preview"
    gemini_temperature: float = 0.1
    gemini_max_output_tokens: int = 3072
    gemini_thinking_level: Literal["minimal", "low", "medium", "high"] = "low"
    gemini_file_search_top_k: int = 8
    gemini_file_search_chunk_max_tokens: int = 512
    gemini_file_search_chunk_overlap_tokens: int = 100
    gemini_file_search_log_retrieval: bool = True
    gemini_context_cache_ttl_hours: int = 24

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
