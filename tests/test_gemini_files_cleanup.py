from __future__ import annotations

import unittest

from app.config import Settings
from app.repositories import InMemoryMedicalRepository
from app.schemas import GeminiFileRuntime, MedicalSource, MedicalSourceRuntime, RuntimeSyncState, SourceRef
from app.services.gemini_files_cleanup import GeminiFilesCleanupService


class FakeGeminiFilesGateway:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.fail_names: set[str] = set()

    def delete_file(self, *, file_name: str) -> None:
        if file_name in self.fail_names:
            raise RuntimeError("delete failed")
        self.deleted.append(file_name)


class GeminiFilesCleanupServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            _env_file=None,
            google_service_account_path="",
            gemini_files_storage_soft_limit_bytes=1_000,
            gemini_files_storage_target_bytes=500,
            gemini_files_storage_hard_limit_bytes=1_500,
            gemini_files_cleanup_batch_size=10,
        )
        self.repository = InMemoryMedicalRepository()
        self.gateway = FakeGeminiFilesGateway()
        self.service = GeminiFilesCleanupService(
            settings=self.settings,
            repository=self.repository,
            gateway=self.gateway,  # type: ignore[arg-type]
        )

    def _add_runtime(
        self,
        *,
        patient_id: str,
        source_id: str,
        file_name: str,
        state: str,
        size_bytes: int,
        last_checked_at: str,
        source_status: str = "ACTIVE",
    ) -> None:
        self.repository.upsert_medical_source(
            MedicalSource(
                source_id=source_id,
                patient_id=patient_id,
                source_status=source_status,  # type: ignore[arg-type]
                source_ref=SourceRef(
                    drive_file_id=f"drive-{source_id}",
                    file_size_bytes=size_bytes,
                    file_hash=f"hash-{source_id}",
                    drive_modified_at="2026-01-01T00:00:00Z",
                ),
            )
        )
        self.repository.upsert_source_runtime(
            MedicalSourceRuntime(
                source_id=source_id,
                patient_id=patient_id,
                gemini_file=GeminiFileRuntime(
                    file_name=file_name,
                    state=state,  # type: ignore[arg-type]
                    source_file_size_bytes=size_bytes,
                    source_file_hash=f"hash-{source_id}",
                    source_drive_modified_at="2026-01-01T00:00:00Z",
                    last_checked_at=last_checked_at,
                    uploaded_at=last_checked_at,
                ),
                sync=RuntimeSyncState(status="READY"),
                wiki_sync=RuntimeSyncState(status="READY"),
            )
        )

    def test_cleanup_skips_when_below_soft_limit(self) -> None:
        self._add_runtime(
            patient_id="P1",
            source_id="S1",
            file_name="files/one",
            state="ACTIVE",
            size_bytes=400,
            last_checked_at="2026-01-01T00:00:00+00:00",
        )

        result = self.service.cleanup_if_needed()

        self.assertFalse(result.triggered)
        self.assertEqual(result.reason, "below_soft_limit")
        self.assertEqual(self.gateway.deleted, [])

    def test_cleanup_deletes_until_target_using_priority(self) -> None:
        self._add_runtime(
            patient_id="P1",
            source_id="S_EXPIRED",
            file_name="files/expired",
            state="EXPIRED",
            size_bytes=400,
            last_checked_at="2026-01-01T00:00:00+00:00",
        )
        self._add_runtime(
            patient_id="P1",
            source_id="S_ACTIVE_OLD",
            file_name="files/active-old",
            state="ACTIVE",
            size_bytes=400,
            last_checked_at="2026-01-02T00:00:00+00:00",
        )
        self._add_runtime(
            patient_id="P1",
            source_id="S_ACTIVE_NEW",
            file_name="files/active-new",
            state="ACTIVE",
            size_bytes=400,
            last_checked_at="2026-01-03T00:00:00+00:00",
        )

        result = self.service.cleanup_if_needed()

        self.assertTrue(result.triggered)
        self.assertEqual(result.before_usage_bytes, 1200)
        self.assertLessEqual(result.after_usage_bytes, 500)
        self.assertEqual(self.gateway.deleted, ["files/expired", "files/active-old"])
        cleaned = self.repository.get_source_runtime("P1", "S_EXPIRED")
        assert cleaned is not None
        self.assertEqual(cleaned.gemini_file.state, "NONE")
        self.assertEqual(cleaned.sync.status, "READY")

    def test_delete_failure_is_reported_without_mutating_runtime(self) -> None:
        self._add_runtime(
            patient_id="P1",
            source_id="S1",
            file_name="files/fail",
            state="EXPIRED",
            size_bytes=1200,
            last_checked_at="2026-01-01T00:00:00+00:00",
        )
        self.gateway.fail_names.add("files/fail")

        result = self.service.cleanup_if_needed()

        self.assertFalse(result.ok)
        self.assertEqual(len(result.errors), 1)
        runtime = self.repository.get_source_runtime("P1", "S1")
        assert runtime is not None
        self.assertEqual(runtime.gemini_file.file_name, "files/fail")
        self.assertEqual(runtime.gemini_file.state, "EXPIRED")


if __name__ == "__main__":
    unittest.main()
