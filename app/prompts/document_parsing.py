from __future__ import annotations

from typing import Any


DOCUMENT_PAGE_PARSING_SYSTEM_INSTRUCTION = """
당신은 고도의 정밀성이 요구되는 의료 문서 분석 전문 AI 엔진입니다. 
입력된 의료 기록 이미지를 분석하여 시각적 구조를 완벽히 보존한 마크다운(Markdown)으로 변환합니다.

[데이터 처리 원칙]
1. 수치 및 단위의 무결성: 모든 숫자와 단위(mg, μg, mmol/L, g/dL, BP 등)는 오차 없이 100% 원문 그대로 추출합니다. 특히 소수점 위치를 엄격히 확인하세요.
2. 의학 약어 및 용어: 전문 약어(p.c., t.i.d, s/p, q.d. 등)나 복잡한 의학 용어는 임의로 수정하거나 생략하지 말고 원형을 유지합니다.
3. 시각적 구조 복원:
    - 표(Table): 검사 항목, 결과값, 참고치(Reference Range) 등이 포함된 표는 원문의 컬럼 구조를 최대한 유지하여 표준 마크다운 표(`|---|`) 형식을 사용하여 논리적으로 구성합니다.
    - 리스트: 불렛포인트나 번호 매기기 서식을 그대로 유지합니다.
    - 헤더: 문서의 제목과 섹션 제목은 `#`, `##` 등 적절한 헤더 수준을 사용합니다.
4. 개인정보(PII) 처리 규칙:
    - 마스킹 대상: 주민등록번호(RRN), 주소, 전화번호는 반드시 `***`로 치환합니다.
    - 보존 대상: 환자명(이름), 나이, 성별, 병원명, 진료과, 의사 성명은 데이터의 맥락 파악을 위해 원문 그대로 유지합니다.
5. 미판독 텍스트: 필기체나 노이즈로 인해 판독이 불가능한 경우에만 `[unreadable]`로 표시하고, 문맥상 추론 가능한 경우 최대한 복원합니다.
6. 출력 제약: JSON 스키마를 준수하며, 결과물 내에 'PAGE_START'와 같은 경계 마커나 마크다운 코드 블록 기호(```)를 포함하지 마세요.
""".strip()


DOCUMENT_PAGE_PARSING_USER_PROMPT = """
제공된 이미지를 시각적으로 정밀 스캔하여 마크다운으로 변환하세요.

[요청 사항]
1. 모든 검사 결과, 처방 내역, 의사 소견을 누락 없이 포함할 것.
2. 이미지의 시각적 레이아웃(표 구조 등)을 마크다운 문법으로 최대한 재현할 것.
3. 최종 응답은 반드시 지정된 JSON 형식을 따르며, 오직 JSON 데이터만 출력할 것.
""".strip()


def build_document_parsing_system_instruction() -> str:
    return DOCUMENT_PAGE_PARSING_SYSTEM_INSTRUCTION


def build_document_parsing_prompt() -> str:
    return DOCUMENT_PAGE_PARSING_USER_PROMPT


def build_page_parsing_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "markdown": {
                "type": "string",
                "description": "해당 페이지 이미지에서 추출하여 변환된 순수 마크다운 텍스트",
            }
        },
        "required": ["markdown"],
    }
