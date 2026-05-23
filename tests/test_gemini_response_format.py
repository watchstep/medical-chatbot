from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any

from app.prompts.gemini_files_qa import build_gemini_files_qa_response_schema
from app.prompts.document_parsing import build_page_parsing_response_schema
from app.prompts.medical_router import build_medical_router_response_schema
from app.prompts.medical_wiki import build_medical_wiki_extraction_response_schema
from app.services.document_parsing import (
    GoogleGeminiDocumentParsingGateway,
    PNG_MIME_TYPE,
    ParsingDocument,
)
from app.services.gemini_files_qa import GoogleGeminiFilesGateway
from app.services.medical_wiki_extractor import (
    GoogleGeminiMedicalWikiExtractionGateway,
    WikiExtractionDocument,
)


class CapturingModels:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(text=self.response_text)


class CapturingClient:
    def __init__(self, response_text: str) -> None:
        self.models = CapturingModels(response_text)


def dumped_config(call: dict[str, Any]) -> dict[str, Any]:
    return call["config"].model_dump(by_alias=True, exclude_none=True)


def collect_key_paths(value: Any, key_name: str, path: str = "$") -> list[str]:
    if isinstance(value, list):
        paths: list[str] = []
        for index, item in enumerate(value):
            paths.extend(collect_key_paths(item, key_name, f"{path}[{index}]"))
        return paths
    if not isinstance(value, dict):
        return []

    paths = [f"{path}.{key_name}"] if key_name in value else []
    for key, item in value.items():
        paths.extend(collect_key_paths(item, key_name, f"{path}.{key}"))
    return paths


class GeminiResponseFormatTest(unittest.TestCase):
    def test_response_schemas_use_gemini_json_subset(self) -> None:
        schemas = [
            build_medical_router_response_schema(),
            build_gemini_files_qa_response_schema(),
            build_medical_wiki_extraction_response_schema(),
            build_page_parsing_response_schema(),
        ]

        for schema in schemas:
            self.assertEqual(collect_key_paths(schema, "$defs"), [])
            self.assertEqual(collect_key_paths(schema, "$ref"), [])
            self.assertEqual(collect_key_paths(schema, "default"), [])
            self.assertEqual(collect_key_paths(schema, "maxLength"), [])

    def test_final_qa_schema_does_not_ask_model_to_control_source_rendering(self) -> None:
        schema = build_gemini_files_qa_response_schema()

        self.assertNotIn("show_sources", schema["properties"])
        self.assertNotIn("evidence_refs", schema["properties"])
        self.assertNotIn("record_claim_answered", schema["properties"])
        self.assertNotIn("show_sources", schema["required"])
        self.assertNotIn("evidence_refs", schema["required"])
        self.assertNotIn("record_claim_answered", schema["required"])

    def test_medical_wiki_schema_does_not_request_title(self) -> None:
        schema = build_medical_wiki_extraction_response_schema()

        self.assertNotIn("title", schema["properties"])
        self.assertNotIn("title", schema["required"])

    def test_files_gateway_sends_response_json_schema_without_extra_body(self) -> None:
        client = CapturingClient(
            json.dumps(
                {
                    "status": "cannot_verify",
                    "kakaotalk_render": "",
                    "used_source_ids": [],
                }
            )
        )
        gateway = GoogleGeminiFilesGateway("test-key", client=client)
        schema = build_gemini_files_qa_response_schema()

        gateway.generate_json(
            model="gemini-test",
            system_instruction="system",
            contents=["prompt"],
            response_schema=schema,
            temperature=0.0,
            max_output_tokens=128,
            thinking_level="low",
        )

        config = dumped_config(client.models.calls[0])
        self.assertNotIn("httpOptions", config)
        self.assertNotIn("responseSchema", config)
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertEqual(config["responseJsonSchema"], schema)

    def test_wiki_gateway_sends_response_json_schema_without_extra_body(self) -> None:
        client = CapturingClient(json.dumps({"category": "unknown"}))
        gateway = GoogleGeminiMedicalWikiExtractionGateway("test-key", client=client)
        schema = build_medical_wiki_extraction_response_schema()

        gateway.extract_source_summary(
            model="gemini-test",
            document=WikiExtractionDocument(
                content=b"document",
                mime_type="text/plain",
                source_id="SRC_P1_A",
            ),
            system_instruction="system",
            prompt="prompt",
            response_schema=schema,
            temperature=0.0,
            max_output_tokens=128,
            thinking_level="low",
        )

        config = dumped_config(client.models.calls[0])
        self.assertNotIn("httpOptions", config)
        self.assertNotIn("responseSchema", config)
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertEqual(config["responseJsonSchema"], schema)

    def test_document_parsing_gateway_sends_response_json_schema_without_extra_body(self) -> None:
        client = CapturingClient(json.dumps({"markdown": "# ok"}))
        gateway = GoogleGeminiDocumentParsingGateway("test-key", client=client)

        result = gateway.parse_page_to_json(
            model="gemini-test",
            document=ParsingDocument(
                name="page.png",
                content=b"\x89PNG\r\n\x1a\nfake",
                mime_type=PNG_MIME_TYPE,
            ),
            system_instruction="system",
            prompt="prompt",
            max_output_tokens=128,
            temperature=0.0,
            top_k=None,
            thinking_level="low",
        )

        config = dumped_config(client.models.calls[0])
        self.assertEqual(result, {"markdown": "# ok"})
        self.assertNotIn("httpOptions", config)
        self.assertNotIn("responseSchema", config)
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertEqual(config["responseJsonSchema"], build_page_parsing_response_schema())


if __name__ == "__main__":
    unittest.main()
