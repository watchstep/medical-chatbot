from __future__ import annotations

import json
from typing import Any


SYSTEM_INSTRUCTION_TEMPLATE = """
당신은 사용자의 질문 의도(Intent)를 분류하고, 필요한 경우 답변에 사용할 최적의 원본 문서를 매핑하는 '전문 내비게이터(Router)'입니다.
당신은 의료 전문가가 아니며, 절대로 질문에 직접 답변하거나 의학적 조언/처방을 내리지 않습니다.

[핵심 목표]
사용자의 질문(`current_question`)과 대화 맥락(`prior_context`)을 분석하여 차단 대상인지 먼저 판단하고, 정상 질문인 경우 [Medical Wiki Catalog]에서 답변에 사용할 수 있는 단 하나의 `primary_source_id`를 선택합니다.

[1. 의도 분류 기준 (Intent)]
- OK: 의료, 건강, 의료 기록과 관련 정상 질문  (일반 의학 설명, 개인 기록 확인 등 모두 포함)
- EMERGENCY: 흉통, 호흡곤란, 의식저하, 심한 출혈, 극심한 통증 등 즉시 의료기관 방문 안내가 필요한 상황
- PRIVACY_BLOCK: 주민등록번호, 주소, 전화번호, 환자번호 등 성함 외 세부 개인정보 조회 요청
- COST_BLOCK: 비용, 금액, 보험료, 결제, 진료비 등 경제적 비용 관련 문의
- OUT_OF_SCOPE: 의료/건강/의료 기록과 무관한 일상 질문

[2. 판단 및 문서 선택 규칙 (Selection Rules)]

1. **차단 우선 판정**: 질문이 EMERGENCY, PRIVACY_BLOCK, COST_BLOCK, OUT_OF_SCOPE에 해당하면 intent를 분류한 뒤, 무조건 `selection_status`는 "insufficient"로, `primary_source_id`는 ""(빈 문자열)로 반환하세요.
2. **OK 처리**: 차단 대상이 아닌 경우에만 intent를 "OK"로 지정합니다.
3. **카탈로그 대조**: OK 질문에서 답변에 유용한 원본 문서가 명확한지 `category`, `date`, `description`, `tags`, `anchors`를 질문과 대조합니다.
4. **조건 검증 (Critical)**:
    - `open_when`: 질문이 이 조건에 부합하는지 확인합니다.
    - `skip_when`: 질문이 이 제외 조건에 해당한다면 절대 선택하지 않습니다.
    - `confidence` & `needs_review`: 신뢰도가 낮거나 검토가 필요한 문서는 다른 대안이 있을 경우 우선순위에서 제외합니다.
5. **안전 실패(Safe-Failure) 원칙**: 질문에 완벽히 매칭되는 문서가 없거나 모호하다면, 무리하게 선택하지 말고 `selection_status`를 "insufficient"로, `primary_source_id`를 ""로 반환하세요. (카탈로그 외의 ID를 임의로 조작/생성 금지)
6. **맥락 활용**: `prior_context`는 대명사(그거, 저번 문서 등)를 해석하는 용도로만 제한적으로 사용하세요.
7. **보안 유지**: 내부 경로, 파일명, Drive ID 등을 `reason` 필드에 절대 노출하지 마세요.
8. **역할 제한**: Router는 차단 판정과 source 선택만 수행합니다.
9. **최종 결정**: intent를 반드시 반환합니다. 차단 intent는 반드시 `insufficient`와 빈 `primary_source_id`를 반환합니다. OK에서 적합한 문서가 명확하면 `selected`, 없거나 모호하면 `insufficient`를 반환합니다.

[출력]
반드시 지정된 JSON 스키마 구조를 엄격히 따라 답변하세요.
""".strip()


ROUTER_TASK = "classify_intent_and_select_primary_source_id_for_final_qa"

SELECTION_RULES = [
    "의도는 반드시 OK, EMERGENCY, PRIVACY_BLOCK, COST_BLOCK, OUT_OF_SCOPE 중 하나로 분류해야 합니다.",
    "정상적인 질문은 모두 OK로 분류하되, 차단 대상 의도(EMERGENCY 등)는 무조건 insufficient 상태와 빈 primary_source_id를 반환하세요.",
    "카탈로그 문서의 open_when, skip_when, confidence, needs_review 조건을 엄격하게 검증하세요.",
    "질문에 확실하게 매칭되는 문서가 없을 경우, 안전하게 selection_status를 insufficient로 설정하세요.",
    "의학적 질문에 절대 직접 답변하지 마세요.",
    "reason 필드에는 의료 조언이나 내부 시스템 ID, 드라이브 경로 등을 포함하지 마세요.",
    "카탈로그에 없는 가짜 source_id를 절대 발급하거나 지셔내지 마세요."
]

OUTPUT_CONTRACT = {
    "selection_status": "selected 또는 insufficient",
    "intent": "OK, EMERGENCY, PRIVACY_BLOCK, COST_BLOCK, OUT_OF_SCOPE",
    "primary_source_id": "선택된 카탈로그의 source_id (미선택 시 빈 문자열 '')",
    "confidence": "라우팅 신뢰도 (0.0 ~ 1.0)",
    "reason": "간결한 라우팅 사유 (의료 답변 및 내부 경로 포함 금지)",
}

def build_medical_router_system_instruction() -> str:
    return SYSTEM_INSTRUCTION_TEMPLATE


def build_medical_router_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "selection_status": {
                "type": "string",
                "enum": ["selected", "insufficient"],
                "description": "Whether a catalog source was selected.",
            },
            "intent": {
                "type": "string",
                "enum": [
                    "OK",
                    "EMERGENCY",
                    "PRIVACY_BLOCK",
                    "COST_BLOCK",
                    "OUT_OF_SCOPE",
                ],
                "description": "Router intent. OK may select a source; fixed-response intents must not select a source.",
            },
            "primary_source_id": {
                "type": "string",
                "description": "Selected catalog source_id, or an empty string when insufficient.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Routing confidence from 0.0 to 1.0.",
            },
            "reason": {
                "type": "string",
                "description": "Brief routing reason without medical advice or internal storage details.",
            },
        },
        "required": [
            "selection_status",
            "intent",
            "primary_source_id",
            "confidence",
            "reason",
        ],
    }


def build_medical_router_prompt(
    *,
    question: str,
    prior_context: str,
    catalog: dict[str, Any],
) -> str:
    payload = {
        "task": ROUTER_TASK,
        "question": question,
        "prior_context": prior_context or "",
        "catalog": catalog,
        "selection_rules": SELECTION_RULES,
        "output_contract": OUTPUT_CONTRACT,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
