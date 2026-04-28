from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.schemas import PatientIndexEntry


KST = timezone(timedelta(hours=9))


@dataclass
class SessionRecord:
    patient: PatientIndexEntry
    expires_at: datetime


class InMemorySessionStore:
    def __init__(self, ttl_minutes: int = 30):
        self.ttl = timedelta(minutes=ttl_minutes)
        self._store: dict[str, SessionRecord] = {}

    def get(self, kakao_user_id: str) -> PatientIndexEntry | None:
        record = self._store.get(kakao_user_id)
        if record is None:
            return None
        if record.expires_at <= datetime.now(KST):
            self._store.pop(kakao_user_id, None)
            return None
        return record.patient

    def set(self, kakao_user_id: str, patient: PatientIndexEntry) -> None:
        self._store[kakao_user_id] = SessionRecord(
            patient=patient,
            expires_at=datetime.now(KST) + self.ttl,
        )

    def delete(self, kakao_user_id: str) -> None:
        self._store.pop(kakao_user_id, None)
