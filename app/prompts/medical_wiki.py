from __future__ import annotations

from typing import Any

from app.medical_wiki_categories import (
    ALLOWED_MEDICAL_WIKI_CATEGORIES,
    build_category_definitions_text,
)

ALLOWED_CATEGORIES = ALLOWED_MEDICAL_WIKI_CATEGORIES

SYSTEM_INSTRUCTION_TEMPLATE = """
당신은 의료 문서를 안전하게 분류하고 Router용 [Source Summary Metadata]를 생성하는 의료 문서 전문 사서(Medical Librarian)입니다.

[핵심 역할 및 미션]
- 본 가이드는 답변의 근거를 요약하는 것이 아니라, Router가 적절한 원본 문서를 선택할 수 있도록 돕는 네비게이션 레이어(Navigation Layer)를 만드는 것입니다.
- 문서의 의학적 고유 의미나 결과를 해석하지 마세요. 문서의 종류, 형태, 그리고 검색을 위한 최소한의 열람 조건만 추출합니다.

[보안 및 데이터 필터링 규칙 - 위반 시 에러]
1. 수치 데이터 제거: 모든 종류의 검사 결과값, 측정값, 기준치, 판정값은 무조건 제외합니다. (예: 120/80, 6.1%, 양성, 음성, 정상, 상승, 감소 등 출력 금지)
2. 약물 정보 제거: 처방 용량, 복용 횟수, 약물 조합은 무조건 제외합니다. (예: 5mg, 1일 2회, 40/5mg 등 출력 금지)
3. 개인정보 완전 마스킹: 주민등록번호, 환자번호, 주소, 전화번호, 이메일, 보험번호 등 개인 식별 정보는 모두 제외합니다.
4. 시스템 식별자 제거: 원본 파일명, Google Drive ID, Gemini file URI, 내부 source_id 등 시스템 정보는 무조건 제외합니다.
5. 의료적 판단 배제: 상태에 대한 임상적 의견이나 조언은 제외합니다. (예: 위험하다, 호전됨, 악화됨, 문제없음 등 출력 금지)

[허용 범주]
- 날짜, 연도, page_count
- 수치가 포함되지 않은 고유 고유명사 항목명 및 짧은 대표 질환명 (예: HbA1c, B12, COVID-19, 고혈압, 고지혈증, eGFR, 간기능)

[작성 원칙]
- description은 결과 요약이 아니라 "어떤 질문에서 이 문서를 열어보면 좋은지"를 설명한다.
- tags는 문서의 넓은 대표 카테고리이다.
  예: 외래진료, 진료기록, 검사결과, 처방내역, 영상검사, 건강검진
- anchors는 사용자가 질문에 입력할 법한 구체 항목명이다.
  예: 혈압, 혈당, HbA1c, eGFR, 간기능, 신장기능, 심전도, 처방내역
- open_when과 skip_when은 Router가 실제로 source를 선택하거나 제외하는 핵심 기준이므로 비워두지 말고 구체적으로 작성해야한다.
- 반드시 지정된 JSON schema만 반환하세요.
""".strip()


EXTRACTION_PROMPT_TEMPLATE = """
제공된 의료 문서를 분석하여 아래 규칙에 맞는 Router용 source_summary JSON을 생성하세요.

[허용 카테고리 목록]
{allowed_categories}

[필드별 상세 추출 가이드]
- page_count: 문서의 총 페이지 수 (식별 가능하면 정수, 모르면 null)
- category: 제공된 카테고리 정의 중 가장 적합한 1개 선택. 복합적일 경우 'mixed_medical_record' 사용.
- date / date_source / date_confidence: 문서의 대표 발행일 또는 최신 검사일을 찾아 YYYY-MM-DD 형태로 추정합니다. 불확실하면 date는 빈 문자열("")로 처리하세요.
- description: 요약문이 아닙니다. "사용자가 어떤 질문을 했을 때 이 문서를 열어보면 유용한지" 매칭 목적의 안내문을 한 문장으로 작성하세요. (결과 수치, 진단명, 처방량 포함 금지)
- tags: 문서의 대분류 카테고리 성격의 단어를 5~8개 지정하세요. (예: 외래진료, 진료기록, 검사결과, 처방내역, 건강검진)
- anchors: 검색어가 매칭될 수 있는 항목명 키워드만 8~12개 지정하세요. 결과값이나 수치, 상세 용량이 포함되면 안 됩니다. (예: 혈압, 혈당, HbA1c, eGFR, 간기능, AST, ALT, CRP)
- open_when: 이 문서를 우선적으로 라우팅해야 하는 구체적인 질문 상황 키워드를 3~6개 명시하세요. 반드시 구체적으로 작성헤세요.
- skip_when: 이 문서를 우선순위에서 배제해야 하는 상황(예: 비용 문의, 보험 청구, 일상 질문, 응급 상황 혹은 타 카테고리 질문)을 3~6개 명시하세요. 문서 카테고리에 맞지 않는 정보군도 제외 조건으로 작성하세요.

[데이터 품질 및 검토 제어 가이드]
- confidence: 본 메타데이터 추출 태스크의 전반적인 완성도 신뢰도를 0.0~1.0 사이의 실수로 평가하세요. 문서의 텍스트가 깨져있거나 정보가 부족할수록 낮게 책정합니다.
- needs_review: 아래 조건 중 하나라도 해당하면 true, 그렇지 않으면 false로 설정하세요.
  * 이미지/텍스트가 흐릿하거나 잘려 수치나 날짜 판독이 모호한 경우
  * 문서 종류가 모호하여 category 선택이나 anchors 구성이 애매한 경우
  * 원본 문서에 데이터 오염이나 모순(예: 서로 다른 날짜 혼재)이 의심되는 경우
- warnings: 메타데이터를 추출하는 과정에서 발견된 품질 이슈나 보안 우려사항을 짧은 문자열 배열로 기록하세요. 특이사항이 없고 깨끗하다면 빈 배열([])을 반환하세요. (예: ["문서 하단 텍스트 일부 누락", "수기 기록 판독 불분명"])

[카테고리 정의]
{category_definitions}

[출력 전 확인]
1. 검사 수치, 결과값, 판정값, 기준치가 없는가?
2. 처방 용량, 복용 횟수, 약물 조합이 없는가?
3. 개인정보와 원본 파일 식별자가 없는가?
4. 의료적 판단 표현이 없는가?
5. open_when과 skip_when이 구체적으로 작성되었는가?

반드시 JSON 객체 하나만 반환하세요.
설명, 마크다운, 코드블록, 생각의 과정은 출력하지 마세요.
""".strip()


def build_allowed_categories_text() -> str:
    return ", ".join(ALLOWED_CATEGORIES)


def build_medical_wiki_extraction_system_instruction() -> str:
    return SYSTEM_INSTRUCTION_TEMPLATE


def build_medical_wiki_extraction_prompt() -> str:
    return EXTRACTION_PROMPT_TEMPLATE.format(
        allowed_categories=build_allowed_categories_text(),
        category_definitions=build_category_definitions_text(),
    )


def build_medical_wiki_extraction_response_schema() -> dict[str, Any]:
    string_array_schema = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": {
            "page_count": {
                "type": ["integer", "null"],
                "minimum": 1,
                "description": "Document page count if known, otherwise null.",
            },
            "category": {
                "type": "string",
                "enum": list(ALLOWED_CATEGORIES),
                "description": "Document category for routing.",
            },
            "date": {
                "type": "string",
                "description": "Representative document date in YYYY-MM-DD when identifiable, otherwise empty string.",
            },
            "date_source": {
                "type": "string",
                "enum": ["content", "filename", "manual", "drive_metadata", "unknown"],
                "description": "How the representative date was inferred.",
            },
            "date_confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Confidence in the representative date.",
            },
            "tags": {
                **string_array_schema,
                "description": "Short routing tags without values, diagnoses list, prescriptions, or identifiers.",
            },
            "description": {
                "type": "string",
                "description": "One safe sentence describing when this source may be useful.",
            },
            "anchors": {
                **string_array_schema,
                "description": "Routing keywords at item-name level only.",
            },
            "open_when": {
                **string_array_schema,
                "description": "Question situations where this document should be opened.",
            },
            "skip_when": {
                **string_array_schema,
                "description": "Question situations where this document should not be prioritized.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Overall confidence in this source summary metadata.",
            },
            "needs_review": {
                "type": "boolean",
                "description": "Whether this summary should be reviewed before strong routing use.",
            },
            "warnings": {
                **string_array_schema,
                "description": "Short safety or quality warnings.",
            },
        },
        "required": [
            "page_count",
            "category",
            "date",
            "date_source",
            "date_confidence",
            "tags",
            "description",
            "anchors",
            "open_when",
            "skip_when",
            "confidence",
            "needs_review",
            "warnings",
        ],
    }
