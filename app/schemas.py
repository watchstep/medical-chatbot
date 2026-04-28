from __future__ import annotations

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
    phone_last4: str | None = None

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


class PatientFileMeta(BaseModel):
    type: str
    date: str
    filename: str
    description: str | None = None
    file_id: str | None = None

    @model_validator(mode="after")
    def normalize_date(self) -> "PatientFileMeta":
        self.date = self.date.replace("-", "")
        return self


class LatestSummary(BaseModel):
    date: str
    title: str
    description: str


class PatientMeta(BaseModel):
    patient_id: str
    name: str
    birth: str
    latest_visit_date: str | None = None
    files: list[PatientFileMeta]
    latest_summary: LatestSummary | None = None
    drive_folder_id: str | None = None
    allowed_kakao_user_ids: list[str] = Field(default_factory=list)
    created_at: str | None = None

    @model_validator(mode="after")
    def fill_derived_fields(self) -> "PatientMeta":
        if self.latest_visit_date is not None:
            self.latest_visit_date = self.latest_visit_date.replace("-", "")
        elif self.files:
            self.latest_visit_date = max(file.date for file in self.files)

        if self.latest_summary is None and self.latest_visit_date is not None:
            self.latest_summary = LatestSummary(
                date=self.latest_visit_date,
                title=(
                    f"{self.latest_visit_date[:4]}년 {int(self.latest_visit_date[4:6])}월 "
                    f"{int(self.latest_visit_date[6:8])}일 진료 및 검사 기록"
                ),
                description="최근 검사결과지와 진료기록부가 등록되어 있습니다.",
            )

        return self


class DriveFile(BaseModel):
    file_id: str
    name: str
    mime_type: str
    date: str | None = None
    type: str | None = None


class PatientRecordContext(BaseModel):
    patient: PatientIndexEntry
    meta: PatientMeta
    latest_result: DriveFile | None
    latest_chart: DriveFile | None
