from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.repositories import InMemoryMedicalRepository
from app.schemas import (
    DriveFileIndexEntry,
    KakaoCallbackJob,
    MedicalSource,
    MedicalSourceRuntime,
    MedicalWikiIndex,
    MedicalWikiIndexPage,
    RuntimeSyncState,
    SourceRef,
)


KST = timezone(timedelta(hours=9))


def now_iso() -> str:
    return datetime.now(KST).isoformat()


class FirestoreAtomicityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryMedicalRepository()
        self.source = MedicalSource(
            source_id="SRC_P0001_TEST",
            patient_id="P0001",
            source_ref=SourceRef(
                drive_file_id="drive-file-1",
                drive_folder_id="folder-p1",
                original_filename="검사결과.pdf",
                mime_type="application/pdf",
                file_hash="hash-1",
                drive_modified_at="2026-04-21T10:00:00+09:00",
            ),
            source_status="ACTIVE",
            created_at=now_iso(),
            updated_at=now_iso(),
        )
        self.runtime = MedicalSourceRuntime(
            source_id=self.source.source_id,
            patient_id=self.source.patient_id,
            sync=RuntimeSyncState(status="READY"),
            wiki_sync=RuntimeSyncState(status="READY"),
            updated_at=now_iso(),
        )
        self.drive_index = DriveFileIndexEntry(
            drive_file_id="drive-file-1",
            patient_id="P0001",
            source_id=self.source.source_id,
            drive_folder_id="folder-p1",
            status="ACTIVE",
            created_at=now_iso(),
            updated_at=now_iso(),
        )

    def test_upsert_source_bundle_writes_source_runtime_and_drive_file_index(self) -> None:
        self.repository.upsert_source_bundle(
            source=self.source,
            runtime=self.runtime,
            drive_file_index=self.drive_index,
        )

        stored_source = self.repository.get_medical_source("P0001", self.source.source_id)
        stored_runtime = self.repository.get_source_runtime("P0001", self.source.source_id)
        stored_index = self.repository.get_drive_file_index("drive-file-1")

        self.assertIsNotNone(stored_source)
        self.assertIsNotNone(stored_runtime)
        self.assertIsNotNone(stored_index)
        assert stored_source is not None
        assert stored_runtime is not None
        assert stored_index is not None
        self.assertEqual(stored_source.source_status, "ACTIVE")
        self.assertEqual(stored_runtime.sync.status, "READY")
        self.assertEqual(stored_index.source_id, self.source.source_id)

    def test_upsert_source_status_bundle_updates_deleted_state_consistently(self) -> None:
        self.repository.upsert_source_bundle(
            source=self.source,
            runtime=self.runtime,
            drive_file_index=self.drive_index,
        )
        deleted_source = self.source.model_copy(update={"source_status": "DELETED", "updated_at": now_iso()})
        deleted_runtime = self.runtime.model_copy(
            update={
                "sync": self.runtime.sync.model_copy(update={"status": "EXPIRED"}),
                "wiki_sync": self.runtime.wiki_sync.model_copy(update={"status": "EXPIRED"}),
                "updated_at": now_iso(),
            }
        )
        deleted_index = self.drive_index.model_copy(update={"status": "DELETED", "updated_at": now_iso()})

        self.repository.upsert_source_status_bundle(
            source=deleted_source,
            runtime=deleted_runtime,
            drive_file_index=deleted_index,
        )

        stored_source = self.repository.get_medical_source("P0001", self.source.source_id)
        stored_runtime = self.repository.get_source_runtime("P0001", self.source.source_id)
        stored_index = self.repository.get_drive_file_index("drive-file-1")
        assert stored_source is not None
        assert stored_runtime is not None
        assert stored_index is not None
        self.assertEqual(stored_source.source_status, "DELETED")
        self.assertEqual(stored_runtime.sync.status, "EXPIRED")
        self.assertEqual(stored_runtime.wiki_sync.status, "EXPIRED")
        self.assertEqual(stored_index.status, "DELETED")

    def test_upsert_wiki_index_with_log_writes_index_and_log_together(self) -> None:
        index = MedicalWikiIndex(
            patient_id="P0001",
            pages=[
                MedicalWikiIndexPage(
                    page_id="PAGE_SRC_P0001_TEST",
                    source_id=self.source.source_id,
                    category="lab_result",
                    confidence=0.9,
                )
            ],
            updated_at=now_iso(),
        )

        log_id = self.repository.upsert_wiki_index_with_log(
            index=index,
            log_payload={
                "event_type": "index_compiled",
                "title": "medical wiki index compiled",
                "status": "success",
                "created_at": now_iso(),
            },
        )

        stored_index = self.repository.get_wiki_index("P0001")
        self.assertIsNotNone(stored_index)
        assert stored_index is not None
        self.assertEqual(len(stored_index.pages), 1)
        self.assertIn(("P0001", log_id), self.repository.wiki_logs)
        self.assertEqual(self.repository.wiki_logs[("P0001", log_id)]["event_type"], "index_compiled")

    def test_callback_job_runnable_fields_drive_polling_query(self) -> None:
        ready_job = KakaoCallbackJob(
            job_id="JOB_READY",
            patient_id="P0001",
            kakao_user_id_hash="sha256:test",
            chat_log_id="LOG_1",
            callback_url="https://callback.example.test",
            runnable=True,
            next_run_at="2000-01-01T00:00:00+09:00",
            expires_at="2999-01-01T00:00:00+09:00",
        )
        future_job = ready_job.model_copy(
            update={
                "job_id": "JOB_FUTURE",
                "next_run_at": "2999-01-01T00:00:00+09:00",
            }
        )
        disabled_job = ready_job.model_copy(
            update={
                "job_id": "JOB_DISABLED",
                "runnable": False,
            }
        )
        self.repository.create_callback_job(ready_job)
        self.repository.create_callback_job(future_job)
        self.repository.create_callback_job(disabled_job)

        runnable = self.repository.list_runnable_callback_jobs(
            now="2026-01-01T00:00:00+09:00",
            limit=10,
        )

        self.assertEqual([job.job_id for job in runnable], ["JOB_READY"])


if __name__ == "__main__":
    unittest.main()
