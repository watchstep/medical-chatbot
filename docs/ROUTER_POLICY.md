# Router Policy

## Router 정책

Router는 의료 답변자가 아니라 intent-aware Medical Wiki Navigator다.
Router는 질문을 답변하지 않고, 차단 여부와 source 선택 여부만 결정한다.
운영 환경에서는 Gemini Router가 intent 판단을 수행해야 하며, 하드코딩 keyword router는 local 또는 test 전용 fallback으로만 허용한다.

입력:

```text
현재 사용자 질문
최근 대화 prior_context
현재 환자의 medical_wiki_index/main
필요한 경우 관련 medical_wiki_pages/{page_id}
```

출력 예시:

```json
{
  "intent": "OK",
  "selection_status": "selected",
  "primary_source_id": "SRC_P0001_A8F39C21D4B2",
  "confidence": 0.89,
  "reason": "질문이 검사 수치와 관련되어 있고 해당 source_summary page가 검사 결과 관련 질문에 적합합니다."
}
```

source를 고르지 못하거나 source가 필요 없다고 판단한 경우:

```json
{
  "intent": "OK",
  "selection_status": "insufficient",
  "primary_source_id": "",
  "confidence": 0.52,
  "reason": "차단 대상은 아니지만 현재 catalog에서 명확한 source를 선택하지 않았습니다."
}
```

허용 `intent`:

```text
OK
EMERGENCY
PRIVACY_BLOCK
COST_BLOCK
OUT_OF_SCOPE
```

intent 의미:

```text
OK
= 차단 대상이 아닌 질문. 답변 가능 여부와 cannot_verify 여부는 판단하지 않는다.

EMERGENCY
= 흉통, 호흡곤란, 의식저하, 심한 출혈, 극심한 통증 등 즉시 의료기관 방문 안내가 필요한 질문.

PRIVACY_BLOCK
= 이름(성함) 외 주민등록번호, 주소, 전화번호, 환자번호, 계좌, 카드번호 등 세부 개인정보 조회 요청.

COST_BLOCK
= 비용, 금액, 보험료, 결제, 진료비, 검사비 등 경제적 비용 관련 문의.

OUT_OF_SCOPE
= 의료, 건강, 의료 기록과 무관한 일상 질문.
```

허용 `selection_status`:

```text
selected
insufficient
```

selection 의미:

```text
selected
= OK 질문이며, Medical Wiki catalog에서 Final QA가 열어볼 가장 관련 있는 primary_source_id를 선택한 상태.
정답 존재를 확정한다는 의미가 아니라 원본 확인을 맡길 최선의 source 선택이다.

insufficient
= OK 질문이지만 source가 필요 없거나, 사용할 수 있는 catalog source가 없거나, 모든 후보가 명백히 제외된 상태. 이때 primary_source_id는 빈 값이다.
단순히 정답이 catalog metadata에 보이지 않는다는 이유만으로 insufficient를 반환하지 않는다.
```

Router 결과 처리:

```text
intent in {EMERGENCY, PRIVACY_BLOCK, COST_BLOCK, OUT_OF_SCOPE}
→ 백엔드가 fixed status로 매핑하고 Gemini Final QA를 호출하지 않음
→ Router가 source_id를 반환해도 백엔드가 무시하거나 제거

intent == OK and selection_status == selected
→ 백엔드가 source 소유권, source_status, runtime readiness 검증
→ 검증 성공 시 prepare_file() 수행
→ 준비된 원본 파일 전체를 prepared_file로 전달하여 answer_question(prepared_file=..., intent="OK") 호출
→ 검증 또는 prepare_file() 실패 시 prepared_file=None으로 fallback하지 않고 기존 안전 실패 흐름을 따름

intent == OK and selection_status == insufficient
→ 백엔드가 source 검증과 prepare_file()을 수행하지 않음
→ answer_question(prepared_file=None, intent="OK") 호출
→ Final QA가 문서 없이 일반 설명 가능 여부 또는 cannot_verify를 판단
```

주의:

- Router는 원본 의료 문서를 보지 않는다.
- Router는 의료 답변을 작성하지 않는다.
- Router는 답변 가능 여부와 `cannot_verify`를 판단하지 않는다.
- Router는 OK 의료기록 질문에서 정답 존재 여부가 아니라 Final QA가 열어볼 최선의 원본 문서를 선택한다.
- Router는 `source_id`를 새로 만들지 않는다.
- Router는 catalog에 없는 source_id를 선택하지 않는다.
- Router는 `medical_wiki_index`와 `medical_wiki_pages`의 category, date, description, tags, anchors, open_when, skip_when, confidence만 사용한다.
- Router는 confidence가 낮거나 `needs_review == true`인 page를 우선 선택하지 않는다.
- `skip_when`은 강한 제외 신호로 사용하며, 약한 불일치만으로 후보를 버리지 않는다.
- Gemini Router가 OK 질문에 `insufficient`를 반환했더라도 usable catalog page가 있으면 백엔드가 deterministic best-effort source를 선택할 수 있다.
- Router에는 `medical_sources.source_ref`, `medical_source_runtime`, `drive_file_index`, `drive_folder_index`를 전달하지 않는다.
- 운영 환경에서는 `ROUTER_MODE=gemini`를 사용한다. keyword intent 분류는 운영 경로에 두지 않는다.
