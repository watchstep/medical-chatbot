from __future__ import annotations

import re
import unicodedata


def normalize_patient_name(value: str | None) -> str:
    text = " ".join(str(value or "").strip().split())
    return unicodedata.normalize("NFC", text)


def normalize_patient_birth(value: str | None) -> str:
    return re.sub(r"\D", "", str(value or ""))


def make_patient_name_key(value: str | None) -> str:
    return normalize_patient_name(value)


def make_patient_birth_key(value: str | None) -> str:
    return normalize_patient_birth(value)


def normalize_drive_folder_name(value: str | None) -> str:
    text = " ".join(str(value or "").strip().split())
    return unicodedata.normalize("NFC", text)