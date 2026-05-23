from __future__ import annotations

import json
import unittest
from typing import Any

from app.config import Settings
from app.schemas import MedicalWikiIndex, MedicalWikiIndexPage
from app.services.gemini_files_qa import GeminiFilesGateway, MedicalRouterService


class FakeRouterGateway(GeminiFilesGateway):
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def upload_file(self, *, display_name: str, content: bytes, mime_type: str) -> Any:
        raise NotImplementedError

    def get_file(self, *, file_name: str) -> Any:
        raise NotImplementedError

    def generate_json(
        self,
        *,
        model: str,
        system_instruction: str,
        contents: list[Any],
        response_schema: dict[str, Any],
        temperature: float,
        max_output_tokens: int,
        thinking_level: str,
    ) -> str:
        self.calls.append(
            {
                "model": model,
                "system_instruction": system_instruction,
                "contents": contents,
                "response_schema": response_schema,
                "temperature": temperature,
                "max_output_tokens": max_output_tokens,
                "thinking_level": thinking_level,
            }
        )
        return json.dumps(self.payload, ensure_ascii=False)


def build_index(*, needs_review: bool = False) -> MedicalWikiIndex:
    return MedicalWikiIndex(
        patient_id="P0001",
        pages=[
            MedicalWikiIndexPage(
                page_id="PAGE_SRC_P0001_A",
                source_id="SRC_P0001_A",
                category="health_checkup",
                date="2026-04-21",
                description="건강검진 결과와 검사 항목 확인이 필요한 질문에서 열어볼 수 있는 문서입니다.",
                tags=["건강검진", "혈액검사"],
                anchors=["혈당", "콜레스테롤", "간기능"],
                open_when=["검사 수치와 관련된 질문"],
                skip_when=["처방약 복용법을 묻는 질문"],
                confidence=0.9,
                needs_review=needs_review,
            ),
            MedicalWikiIndexPage(
                page_id="PAGE_SRC_P0001_B",
                source_id="SRC_P0001_B",
                category="prescription",
                date="2026-04-22",
                description="처방약 또는 복용 관련 질문에서 열어볼 수 있는 문서입니다.",
                tags=["처방", "복용"],
                anchors=["처방", "약", "복용"],
                open_when=["처방약 복용법을 묻는 질문"],
                skip_when=["건강검진 검사 수치를 확인하는 질문"],
                confidence=0.85,
            ),
        ],
    )


class MedicalRouterServiceTest(unittest.TestCase):
    def test_gemini_router_selects_catalog_source(self) -> None:
        gateway = FakeRouterGateway(
            {
                "selection_status": "selected",
                "intent": "OK",
                "primary_source_id": "SRC_P0001_A",
                "confidence": 0.91,
                "reason": "질문이 혈액검사 결과 확인과 관련되어 해당 문서가 가장 적합합니다.",
            }
        )
        service = MedicalRouterService(
            settings=Settings(_env_file=None, gemini_api_key="test-key"),
            gateway=gateway,
        )

        selection = service.select_source(
            question="혈당 수치가 어떻게 나왔나요?",
            prior_context="",
            wiki_index=build_index(),
        )

        self.assertEqual(selection.selection_status, "selected")
        self.assertEqual(selection.primary_source_id, "SRC_P0001_A")
        self.assertEqual(len(gateway.calls), 1)
        self.assertIn("catalog", gateway.calls[0]["contents"][0])

    def test_gemini_router_blocks_source_outside_catalog(self) -> None:
        gateway = FakeRouterGateway(
            {
                "selection_status": "selected",
                "intent": "OK",
                "primary_source_id": "SRC_OTHER_PATIENT",
                "confidence": 0.99,
                "reason": "잘못된 source 선택",
            }
        )
        service = MedicalRouterService(
            settings=Settings(_env_file=None, gemini_api_key="test-key"),
            gateway=gateway,
        )

        selection = service.select_source(
            question="혈당 수치가 어떻게 나왔나요?",
            prior_context="",
            wiki_index=build_index(),
        )

        self.assertEqual(selection.selection_status, "insufficient")
        self.assertEqual(selection.primary_source_id, "")
        self.assertEqual(selection.reason, "router selected source outside catalog")

    def test_gemini_router_blocks_low_confidence(self) -> None:
        gateway = FakeRouterGateway(
            {
                "selection_status": "selected",
                "intent": "OK",
                "primary_source_id": "SRC_P0001_A",
                "confidence": 0.2,
                "reason": "낮은 확신",
            }
        )
        service = MedicalRouterService(
            settings=Settings(_env_file=None, gemini_api_key="test-key"),
            gateway=gateway,
        )

        selection = service.select_source(
            question="혈당 수치가 어떻게 나왔나요?",
            prior_context="",
            wiki_index=build_index(),
        )

        self.assertEqual(selection.selection_status, "insufficient")
        self.assertEqual(selection.primary_source_id, "")
        self.assertEqual(selection.reason, "router confidence below threshold")

    def test_needs_review_pages_are_excluded_from_gemini_catalog(self) -> None:
        gateway = FakeRouterGateway(
            {
                "selection_status": "selected",
                "intent": "OK",
                "primary_source_id": "SRC_P0001_A",
                "confidence": 0.91,
                "reason": "should not be called",
            }
        )
        index = MedicalWikiIndex(
            patient_id="P0001",
            pages=[page.model_copy(update={"needs_review": True}) for page in build_index().pages],
        )
        service = MedicalRouterService(
            settings=Settings(_env_file=None, gemini_api_key="test-key"),
            gateway=gateway,
        )

        selection = service.select_source(
            question="혈당 수치가 어떻게 나왔나요?",
            prior_context="",
            wiki_index=index,
        )

        self.assertEqual(selection.selection_status, "insufficient")
        self.assertEqual(selection.reason, "router selected source outside catalog")
        self.assertEqual(len(gateway.calls), 1)
        self.assertIn('"pages": []', gateway.calls[0]["contents"][0])

    def test_gemini_router_fixed_intent_ignores_source(self) -> None:
        gateway = FakeRouterGateway(
            {
                "selection_status": "selected",
                "intent": "COST_BLOCK",
                "primary_source_id": "SRC_P0001_A",
                "confidence": 0.95,
                "reason": "비용 문의",
            }
        )
        service = MedicalRouterService(
            settings=Settings(_env_file=None, gemini_api_key="test-key"),
            gateway=gateway,
        )

        selection = service.select_source(
            question="진료비 얼마야?",
            prior_context="",
            wiki_index=build_index(),
        )

        self.assertEqual(selection.intent, "COST_BLOCK")
        self.assertEqual(selection.selection_status, "insufficient")
        self.assertEqual(selection.primary_source_id, "")

if __name__ == "__main__":
    unittest.main()
