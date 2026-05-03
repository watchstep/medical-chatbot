from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class KakaoUser(BaseModel):
    id: str


class KakaoUserRequest(BaseModel):
    user: KakaoUser
    utterance: str = ""
    callback_url: str | None = Field(default=None, alias="callbackUrl")


class KakaoSkillRequest(BaseModel):
    user_request: KakaoUserRequest = Field(alias="userRequest")

    model_config = {"populate_by_name": True}


class PatientIndexEntry(BaseModel):
    patient_id: str
    name: str
    birth: str
    folder_name: str | None = None
    folder_id: str | None = None
    kakao_user_ids: list[str] = Field(default_factory=list)
    phone_last4: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_fields(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data

        payload = dict(data)
        payload.setdefault("kakao_user_ids", payload.get("allowed_kakao_user_ids", []))
        if not payload.get("folder_name"):
            patient_id = payload.get("patient_id")
            name = payload.get("name")
            birth = payload.get("birth")
            if patient_id and name and birth:
                payload["folder_name"] = f"{patient_id}_{name}_{birth}"
        return payload


class PatientIndex(BaseModel):
    patients: list[PatientIndexEntry]


class DriveFile(BaseModel):
    file_id: str
    name: str
    mime_type: str
    date: str | None = None
    type: str | None = None


class DocumentRegistryEntry(BaseModel):
    patient_id: str
    filename: str
    document_type: str = ""
    document_date: str = ""
    drive_file_id: str = ""
    drive_modified_time: str = ""
    file_hash: str = ""
    file_search_store_name: str = ""
    file_search_document_name: str = ""
    sync_status: str
    synced_at: str = ""

    @model_validator(mode="after")
    def normalize_dates(self) -> "DocumentRegistryEntry":
        self.document_date = self.document_date.replace("-", "")
        return self


class DocumentRegistry(BaseModel):
    generated_at: str | None = None
    documents: list[DocumentRegistryEntry] = Field(default_factory=list)


class PatientDocumentRegistryContext(BaseModel):
    patient: PatientIndexEntry
    documents: list[DocumentRegistryEntry]
    latest_result: DocumentRegistryEntry | None
    latest_chart: DocumentRegistryEntry | None
    file_search_store_name: str | None = None


Status = Literal[
    "ok",
    "blocked",
    "cannot_verify",
    "emergency",
    "out_of_scope",
    "cost_block",
    "full_doc_block",
]
BlockType = Literal["paragraph", "bullet_list"]


class Block(BaseModel):
    type: BlockType
    text: str | None = None
    items: list[str] | None = None

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def validate_shape(self) -> "Block":
        if self.type == "paragraph":
            if not self.text or self.items is not None:
                raise ValueError("paragraph block must contain only text")
        if self.type == "bullet_list":
            if self.text is not None or not self.items:
                raise ValueError("bullet_list block must contain only items")
        return self


class ModelAnswer(BaseModel):
    status: Status
    blocks: list[Block] = Field(default_factory=list, max_length=3)
    used_source_ids: list[str] = Field(default_factory=list)
    show_sources: bool = True
