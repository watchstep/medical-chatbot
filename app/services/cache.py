from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.schemas import (
    DocumentRegistry,
    DocumentRegistryEntry,
    PatientDocumentRegistryContext,
    PatientIndex,
    PatientIndexEntry,
)
from app.services.drive import DriveLookupError, DriveLookupService
from app.services.gemini_qa import is_file_search_store_name


logger = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))


@dataclass
class CachedPatientIndexState:
    patient_index: PatientIndex
    patient_index_file_id: str
    expires_at: datetime


@dataclass
class CachedDocumentRegistryState:
    document_registry: DocumentRegistry
    document_registry_file_id: str | None
    expires_at: datetime


@dataclass
class AuthResult:
    patient: PatientIndexEntry
    persistence_required: bool


class PatientDataCacheService:
    def __init__(
        self,
        *,
        settings: Settings,
        drive_service: DriveLookupService,
    ):
        self.settings = settings
        self.drive_service = drive_service
        self._patient_index_state: CachedPatientIndexState | None = None
        self._document_registry_state: CachedDocumentRegistryState | None = None

    def get_patient_by_kakao_user_id(self, kakao_user_id: str) -> PatientIndexEntry | None:
        patient_index = self._get_patient_index()
        return next(
            (item for item in patient_index.patients if kakao_user_id in item.kakao_user_ids),
            None,
        )

    def authenticate_and_map_patient(
        self,
        *,
        kakao_user_id: str,
        name: str,
        birth: str,
    ) -> AuthResult:
        state = self._get_patient_index_state()
        existing_patient = self._find_patient_by_kakao_user_id(state.patient_index, kakao_user_id)
        if existing_patient is not None:
            if existing_patient.name != name or existing_patient.birth != birth:
                raise DriveLookupError("이미 다른 환자에 연결된 사용자입니다.")
            return AuthResult(patient=existing_patient, persistence_required=False)

        patient = self._find_patient_by_identity(state.patient_index, name, birth)
        if patient is None:
            raise DriveLookupError(
                "등록된 환자 정보를 찾지 못했습니다. 병원에 등록 여부를 확인해 주세요."
            )

        updated_index = self._attach_kakao_user_id(
            patient_index=state.patient_index,
            target_patient_id=patient.patient_id,
            kakao_user_id=kakao_user_id,
        )
        self._patient_index_state = CachedPatientIndexState(
            patient_index=updated_index,
            patient_index_file_id=state.patient_index_file_id,
            expires_at=self._build_patient_index_expiry(),
        )
        updated_patient = self._find_patient_by_identity(updated_index, name, birth) or patient
        return AuthResult(patient=updated_patient, persistence_required=True)

    def persist_patient_index(self) -> None:
        state = self._patient_index_state
        if state is None:
            return
        self.drive_service.persist_patient_index(
            patient_index_file_id=state.patient_index_file_id,
            patient_index=state.patient_index,
        )

    def get_patient_document_context(
        self,
        *,
        patient: PatientIndexEntry,
    ) -> PatientDocumentRegistryContext:
        document_registry = self._get_document_registry_state().document_registry
        patient_documents = [
            item
            for item in document_registry.documents
            if item.patient_id == patient.patient_id
        ]
        ready_documents = [
            item for item in patient_documents if item.sync_status == "READY"
        ]
        latest_result = self._latest_document_by_type(ready_documents, "result")
        latest_chart = self._latest_document_by_type(ready_documents, "chart")
        store_names = {
            item.file_search_store_name
            for item in ready_documents
            if is_file_search_store_name(item.file_search_store_name)
        }
        return PatientDocumentRegistryContext(
            patient=patient,
            documents=patient_documents,
            latest_result=latest_result,
            latest_chart=latest_chart,
            file_search_store_name=next(iter(store_names), None),
        )

    def reset_kakao_user_id(self, *, kakao_user_id: str) -> bool:
        state = self._get_patient_index_state()
        updated_patients: list[PatientIndexEntry] = []
        changed = False
        for patient in state.patient_index.patients:
            filtered_user_ids = [
                user_id for user_id in patient.kakao_user_ids if user_id != kakao_user_id
            ]
            if len(filtered_user_ids) != len(patient.kakao_user_ids):
                changed = True
                updated_patients.append(
                    patient.model_copy(update={"kakao_user_ids": filtered_user_ids})
                )
            else:
                updated_patients.append(patient)
        if changed:
            self._patient_index_state = CachedPatientIndexState(
                patient_index=PatientIndex(patients=updated_patients),
                patient_index_file_id=state.patient_index_file_id,
                expires_at=self._build_patient_index_expiry(),
            )
        return changed

    def _get_patient_index(self) -> PatientIndex:
        return self._get_patient_index_state().patient_index

    def _get_patient_index_state(self) -> CachedPatientIndexState:
        now = datetime.now(KST)
        if self._patient_index_state is not None and self._patient_index_state.expires_at > now:
            logger.info("patient_index_cache hit")
            return self._patient_index_state

        logger.info("patient_index_cache miss")
        patient_index, patient_index_file_id = self.drive_service.load_patient_index_with_file_id()
        self._patient_index_state = CachedPatientIndexState(
            patient_index=patient_index,
            patient_index_file_id=patient_index_file_id,
            expires_at=self._build_patient_index_expiry(),
        )
        return self._patient_index_state

    def _build_patient_index_expiry(self) -> datetime:
        return datetime.now(KST) + timedelta(seconds=self.settings.patient_index_cache_ttl_seconds)

    def _build_document_registry_expiry(self) -> datetime:
        return datetime.now(KST) + timedelta(
            seconds=self.settings.document_registry_cache_ttl_seconds
        )

    def _get_document_registry_state(self) -> CachedDocumentRegistryState:
        now = datetime.now(KST)
        if self._document_registry_state is not None and self._document_registry_state.expires_at > now:
            logger.info("document_registry_cache hit")
            return self._document_registry_state

        logger.info("document_registry_cache miss")
        document_registry, document_registry_file_id = (
            self.drive_service.load_document_registry_with_file_id()
        )
        self._document_registry_state = CachedDocumentRegistryState(
            document_registry=document_registry,
            document_registry_file_id=document_registry_file_id,
            expires_at=self._build_document_registry_expiry(),
        )
        return self._document_registry_state

    def _latest_document_by_type(
        self,
        documents: list[DocumentRegistryEntry],
        document_type: str,
    ) -> DocumentRegistryEntry | None:
        matches = [
            item for item in documents if item.document_type == document_type
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: (item.document_date, item.filename))

    def _find_patient_by_kakao_user_id(
        self,
        patient_index: PatientIndex,
        kakao_user_id: str,
    ) -> PatientIndexEntry | None:
        return next(
            (item for item in patient_index.patients if kakao_user_id in item.kakao_user_ids),
            None,
        )

    def _find_patient_by_identity(
        self,
        patient_index: PatientIndex,
        name: str,
        birth: str,
    ) -> PatientIndexEntry | None:
        return next(
            (
                item
                for item in patient_index.patients
                if item.name == name and item.birth == birth
            ),
            None,
        )

    def _attach_kakao_user_id(
        self,
        *,
        patient_index: PatientIndex,
        target_patient_id: str,
        kakao_user_id: str,
    ) -> PatientIndex:
        owner = self._find_patient_by_kakao_user_id(patient_index, kakao_user_id)
        if owner is not None and owner.patient_id != target_patient_id:
            raise DriveLookupError("이미 다른 환자에 연결된 사용자입니다.")

        updated_patients: list[PatientIndexEntry] = []
        for patient in patient_index.patients:
            if patient.patient_id == target_patient_id:
                kakao_user_ids = list(patient.kakao_user_ids)
                if kakao_user_id not in kakao_user_ids:
                    kakao_user_ids.append(kakao_user_id)
                updated_patients.append(
                    patient.model_copy(update={"kakao_user_ids": kakao_user_ids})
                )
                continue
            updated_patients.append(patient)
        return PatientIndex(patients=updated_patients)
