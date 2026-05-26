# Final QA Policy

## Gemini Final QA 정책

Gemini Final QA는 단일 `intent="OK"` 흐름에서 문서가 실제로 전달되었는지 여부와 사용자 질문 내용을 기준으로 답변 JSON을 생성한다.
Final QA는 `GENERAL_MEDICAL_CONCEPT`, `PATIENT_RECORD_QA`, `HYBRID` 같은 세분화 intent를 사용하지 않는다.
Final QA는 `medical_wiki_pages` 또는 `medical_wiki_index`를 답변 근거로 사용하지 않는다.
Final QA는 `📄 출처` 섹션을 절대 작성하지 않는다. 출처 섹션은 백엔드가 검증 후 붙인다.

Final QA 입력:

```text
intent="OK"
현재 사용자 질문
최근 대화 prior_context
선택적으로 준비된 Gemini Files API file object. prepared_file이 없으면 문서 없이 호출
prepared_file이 있을 때만 source_id를 prompt에 전달
출력 JSON schema instruction
```

Final QA 호출 방식:

```text
answer_question(prepared_file=prepared_file, intent="OK")
→ Gemini contents = [prepared_file.file_object, prompt]
→ source_id는 prepared_file.source_id만 사용

answer_question(prepared_file=None, intent="OK")
→ Gemini contents = [prompt]
→ source_id를 prompt에 전달하지 않음
```

중요:

- `answer_without_source()`는 사용하지 않는다.
- source 없는 QA도 `answer_question(prepared_file=None, intent="OK")`로 통합한다.
- `has_source`를 Router 출력, DB 필드, 별도 backend 상태값으로 만들지 않는다.
- 문서 전달 여부는 `prepared_file is not None` 또는 실제 Gemini contents에 file object가 포함되었는지로 판단한다.

Final QA 출력은 반드시 JSON이어야 한다. 답변 본문은 `kakaotalk_render` 하나만 사용한다.
`evidence_refs`는 출력 schema에 포함하지 않는다.

```json
{
  "status": "ok",
  "kakaotalk_render": "검증된 카카오톡 본문. 원본 기록 근거 문장에는 (2쪽)처럼 페이지를 표시",
  "used_source_ids": [
    "SRC_P0001_A8F39C21D4B2"
  ]
}
```

허용 `status`:

```text
ok
cannot_verify
out_of_scope
emergency
blocked
cost_block
```

문서가 전달된 경우의 Final QA 규칙:

```text
- 제공된 원본 의료 기록 전체에서 직접 근거를 찾는다.
- 특정 관련 페이지만 보지 않고 제공된 원본 의료 기록 전체를 검토한다.
- 개인 검사 수치, 처방, 진단, 판독, 의사 소견에 대한 구체적 주장은 원본 기록 근거가 있을 때만 작성한다.
- 개인 기록에 대한 구체적 주장을 작성하면 used_source_ids를 반드시 채운다.
- 원본 기록 근거로 작성한 각 문장 끝에는 `(2쪽)` 또는 `(2쪽, 5쪽)` 형식으로 참고 페이지를 표시한다.
- 원본 기록에서 직접 확인되지 않는 개인 기록 내용은 추정하지 않는다.
- 개인 기록 확인 질문인데 직접 근거가 부족하면 status=cannot_verify를 반환한다.
- 일반 의학 설명이 필요한 질문이면 일반 설명도 함께 작성할 수 있다.
- source가 함께 전달되었더라도 일반 설명만 하는 ok 응답은 허용한다. 이 경우 used_source_ids는 비울 수 있고 페이지 표시는 하지 않는다.
```

문서가 전달되지 않은 경우의 Final QA 규칙:

```text
- 일반 의학 개념, 질환 설명, 검사 항목 의미 설명은 status=ok로 답변할 수 있다.
- 사용자의 개인 검사 결과, 처방, 진단, 영상 판독, 병원 기록 확인만 요구하는 질문은 status=cannot_verify를 반환한다.
- 일반 설명과 개인 기록 확인이 섞인 질문은 일반 설명만 제공할 수 있으며, 개인 기록은 확인할 수 없다고 명시한다.
- 문서가 없는데 개인 기록 내용, 검사 수치, 처방, 진단, 판독 결과를 추정하지 않는다.
- source 없이 호출된 응답의 used_source_ids는 반드시 비어 있어야 한다.
```

차단 status에 대한 방어 규칙:

```text
- EMERGENCY, PRIVACY_BLOCK, COST_BLOCK, OUT_OF_SCOPE는 원칙적으로 Router와 백엔드가 선처리하므로 Final QA를 호출하지 않는다.
- Final QA 단계에 도달했더라도 명백한 경우 emergency, blocked, cost_block, out_of_scope status를 반환할 수 있다.
```

추가 백엔드 검증:

```text
- JSON 파싱 성공 여부 확인
- schema 유효성 확인
- 허용되지 않은 필드 차단. evidence_refs, show_sources, general_medical_render, patient_record_render, record_claim_answered 금지
- kakaotalk_render에 `📄 출처`, source_id, Drive ID, Gemini file URI, 원본 파일명, 내부 ID가 없는지 확인
- source 없이 호출된 응답은 used_source_ids=[]이어야 함
- source와 함께 호출된 응답의 used_source_ids는 전달된 source_id만 허용
- source와 함께 호출되었더라도 일반 설명만 하는 ok 응답은 used_source_ids=[]를 허용
- source와 함께 호출된 ok 응답은 used_source_ids=[] 또는 [전달된 source_id]만 허용
- 백엔드는 페이지 번호를 생성, 검증, 보정하지 않음. 페이지 번호 검증은 수행하지 않음
- status == ok이면 kakaotalk_render 필수
- status == cannot_verify도 kakaotalk_render를 포함하거나 백엔드 고정 cannot_verify 메시지로 대체
- status == ok인 kakaotalk_render에 마크다운 금지 문법이 포함되지 않았는지 확인
```

## status 처리 정책

Router intent와 Final QA status는 역할이 다르다.

```text
Router intent
= 차단 여부와 OK 여부 결정

Router selection_status
= OK 질문에서 source 선택 여부 결정

Final QA status
= 생성된 답변의 최종 상태. ok 또는 cannot_verify 등을 판단
```

고정 응답 intent는 Final QA를 호출하지 않고 백엔드가 즉시 status로 매핑한다.

```text
EMERGENCY      → emergency    → 🧑‍⚕️ 증상이 지속된다면 의료기관을 찾아 전문의와 상의해 보시길 권합니다.
PRIVACY_BLOCK  → blocked      → 🔒 개인정보 보호 정책에 따라 세부 개인정보는 안내해 드리지 않습니다.
COST_BLOCK     → cost_block   → 💳 비용 관련 정보는 해당 의료기관에 직접 문의하셔야 합니다.
OUT_OF_SCOPE   → out_of_scope → 💬 의료 기록과 관련된 질문에만 답변을 드릴 수 있습니다.
```

`status != ok`인 경우:

```text
- 원칙적으로 status에 매핑된 백엔드 고정 메시지 또는 안전 실패 메시지를 출력한다.
- cannot_verify는 Final QA answer status로만 사용하고 Router intent로 만들지 않는다.
- used_source_ids는 비어 있어야 한다.
- 출처 섹션은 출력하지 않는다.
- 질문 재분석으로 status를 바꾸지 않는다.
```

`status == ok`인 경우:

```text
source 없이 호출된 ok
→ kakaotalk_render만 출력
→ used_source_ids=[] 필수
→ 출처 없음

source와 함께 호출된 ok + used_source_ids 없음
→ 일반 설명만 하는 답변으로 처리
→ kakaotalk_render만 출력
→ 출처 없음

source와 함께 호출된 ok + used_source_ids 있음
→ used_source_ids가 최종 QA에 전달된 selected source_id와 일치하는지 검증
→ 검증 성공 시 kakaotalk_render 출력
→ 백엔드가 category/date/page_count 기반 문서 표시명만 있는 출처 섹션 append
```

안전 실패 메시지:

```python
SAFE_FALLBACK_MESSAGE = "🔍 해당 내용은 제공된 의료 기록에서 확인하기 어렵습니다."
```

## 출처 처리 정책

- 출처 섹션은 Gemini가 작성하지 않는다.
- Gemini의 `kakaotalk_render`에는 `📄 출처` 섹션, 문서 표시명, 파일명, Drive ID, Gemini file URI, 내부 source_id를 넣지 않는다.
- 백엔드가 검증된 `used_source_ids`를 기반으로 하단 출처 섹션을 생성한다.
- 출처는 문서 단위만 표시하며, 문서별 페이지 범위나 원본 파일명은 표시하지 않는다.
- `medical_wiki_pages.frontmatter.title`은 출처 표시명으로 사용하지 않는다.
- 문서 표시명은 `medical_wiki_pages.frontmatter.category`, `date`, `page_count`를 기준으로 백엔드가 생성한다.
- 표시 형식은 `{category_label} ({date}, {page_count}쪽)`이다.
- `date` 또는 `page_count`가 없으면 있는 값만 괄호에 넣고, 둘 다 없으면 괄호를 표시하지 않는다.
- category가 없거나 알 수 없으면 category label은 `의료 문서`를 사용한다.
- 백엔드는 출처 섹션에 원본 근거 페이지 범위를 붙이지 않는다. 단, 문서 전체 쪽수인 `page_count`는 표시명 metadata로 사용할 수 있다.
- 페이지 표시는 원본 기록 근거 문장 끝의 `(2쪽)` 또는 `(2쪽, 5쪽)` 형식으로만 표현한다.
- Google Drive ID, Gemini file_name, file_uri, 내부 source_id, 원본 파일명은 노출하지 않는다.
- source 없이 호출된 ok 응답에는 출처 섹션을 표시하지 않는다.
- source와 함께 호출되었더라도 used_source_ids가 비어 있으면 출처 섹션을 표시하지 않는다.
- source와 함께 호출되고 `used_source_ids == [selected_source_id]` 검증이 성공한 ok 응답에만 출처 섹션을 표시한다.
- cannot_verify, emergency, blocked, cost_block, out_of_scope 응답에는 출처 섹션을 표시하지 않는다.

예시:

```text
📄 출처
1. 검사 결과 (2026-04-21, 5쪽)
2. 처방 기록 (2026-04-22)
3. 의료 문서
```
