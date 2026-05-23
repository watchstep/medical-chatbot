from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from fastapi import BackgroundTasks

from app.config import Settings
from app.repositories import InMemoryMedicalRepository
from app.schemas import (
    KakaoSkillRequest,
    KakaoUserMapping,
    MedicalWikiIndex,
    MedicalWikiIndexPage,
    PatientProfile,
)
from app.services.firestore_chatbot import FirestoreChatbotService
from app.services.medical_wiki import now_kst_iso
from app.services.security import hash_kakao_user_id

KST = timezone(timedelta(hours=9))


class FakeCallbackProcessor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def process_job(self, job_id: str) -> None:
        self.calls.append(job_id)


class FakeCallbackTaskEnqueueService:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def enqueue_callback_job(self, *, job_id: str):
        if self.fail:
            raise RuntimeError("enqueue failed")
        self.calls.append(job_id)
        return type("Result", (), {"task_name": f"tasks/{job_id}", "duplicate": False})()


def skill_request(*, utterance: str, callback_url: str | None = "https://callback.example") -> KakaoSkillRequest:
    return KakaoSkillRequest.model_validate(
        {
            "userRequest": {
                "user": {"id": "kakao-user-1"},
                "utterance": utterance,
                "callbackUrl": callback_url,
            }
        }
    )


class FirestoreChatbotCloudTasksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            _env_file=None,
            callback_worker_mode="cloud_tasks",
            medical_wiki_extraction_mode="metadata",
        )
        self.repository = InMemoryMedicalRepository()
        self.patient = PatientProfile(
            patient_id="P0001",
            name="손창선",
            birth="19461230",
            drive_folder_id="folder-p1",
        )
        self.repository.upsert_patient(self.patient)
        now = datetime.now(KST)
        self.repository.upsert_kakao_mapping(
            KakaoUserMapping(
                kakao_user_id_hash=hash_kakao_user_id("kakao-user-1"),
                patient_id=self.patient.patient_id,
                authenticated_at=now.isoformat(),
                expires_at=(now + timedelta(minutes=60)).isoformat(),
                status="active",
            )
        )
        self.repository.upsert_wiki_index(
            MedicalWikiIndex(
                patient_id=self.patient.patient_id,
                pages=[
                    MedicalWikiIndexPage(
                        page_id="PAGE_SRC_1",
                        source_id="SRC_1",
                        category="lab_result",
                        confidence=0.9,
                    )
                ],
                updated_at=now_kst_iso(),
            )
        )
        self.processor = FakeCallbackProcessor()
        self.enqueue_service = FakeCallbackTaskEnqueueService()
        self.chatbot = FirestoreChatbotService(
            settings=self.settings,
            repository=self.repository,
            callback_job_processor=self.processor,  # type: ignore[arg-type]
            callback_task_enqueue_service=self.enqueue_service,  # type: ignore[arg-type]
        )

    def test_cloud_tasks_mode_enqueues_callback_job_without_background_task(self) -> None:
        background_tasks = BackgroundTasks()

        response = asyncio.run(
            self.chatbot.handle_chat(skill_request(utterance="검사 결과 알려줘"), background_tasks)
        )

        self.assertEqual(response["version"], "2.0")
        self.assertEqual(len(self.enqueue_service.calls), 1)
        self.assertEqual(len(background_tasks.tasks), 0)
        self.assertEqual(self.processor.calls, [])
        job_id = self.enqueue_service.calls[0]
        stored = self.repository.get_callback_job(job_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, "PENDING")

    def test_enqueue_failure_still_returns_ack_and_leaves_job_for_polling_fallback(self) -> None:
        self.chatbot.callback_task_enqueue_service = FakeCallbackTaskEnqueueService(fail=True)  # type: ignore[assignment]
        background_tasks = BackgroundTasks()

        response = asyncio.run(
            self.chatbot.handle_chat(skill_request(utterance="검사 결과 알려줘"), background_tasks)
        )

        self.assertEqual(response["version"], "2.0")
        self.assertEqual(len(background_tasks.tasks), 0)
        jobs = list(self.repository.callback_jobs.values())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].status, "PENDING")
        self.assertTrue(jobs[0].runnable)


if __name__ == "__main__":
    unittest.main()
