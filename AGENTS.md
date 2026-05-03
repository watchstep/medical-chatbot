## 프로젝트 개요
- 의료진단기록 카카오톡 + Gemini 챗봇 구현
- 환자가 카카오채널 챗봇으로 질문하면, 서버는 인증된 환자의 Gemini File Search Store를 사용해 문서 기반 답변 JSON을 출력한다.
- 백엔드는 JSON 파싱, schema 검증, status별 메시지 출력 처리, evidence 존재 여부 확인, 그리고 used_source_ids가 현재 인증된 환자의 READY 문서인지 확인한 뒤 카카오 챗봇 응답으로 반환한다. 
- Google Drive는 원본 의료 PDF의 기준 저장소이며, 환자 인덱스 생성과 File Search Store 동기화 작업에서만 조회한다.

## 주요 기술

- Python
- FastAPI
- Uvicorn
- Kakao OpenBuilder Skill
- ngrok
- Google Drive API
- Gemini API
- Gemini File Search Store
- GCP Cloud Run
- Pydantic

## API 엔드포인트 구분

카카오 OpenBuilder Skill 서버는 인증 흐름과 채팅 흐름을 분리한다.

| 엔드포인트 | 역할 |
|---|---|
| `/kakao/auth` | 시작 인사, 인증 상태 확인, 이름/생년월일 인증, 인증 초기화 |
| `/kakao/chat` | 최신 기록 조회, 인증된 환자의 Gemini 기반 문서 질의응답 |

- `/kakao/auth`는 인증 및 환자 식별만 담당한다.
- `/kakao/chat`은 반드시 인증된 사용자만 처리한다.
- `/kakao/chat`은 인증되지 않은 사용자에게 환자 폴더, PDF, File Search Store, Gemini 요청을 수행하지 않는다.
- `/kakao/chat`에서 인증 세션이 없고 `patient_index.json`에도 매핑이 없으면 `/kakao/auth`에서 인증하라는 안내를 반환한다.

## 전체 처리 흐름

```text
카카오 사용자 메시지 수신
→ 요청 엔드포인트 확인

/kakao/auth 인 경우:
  → kakao_user_id 추출
  → 인증 초기화 명령인지 확인
  → 인증 세션 확인
  → patient_index.json 에서 kakao_user_id 매핑 확인
  → 매핑이 있으면 인증 상태 안내
  → 매핑이 없으면 이름 + 생년월일 인증 요청
  → 인증 입력 수신
  → patient_index.json 조회
  → 환자 존재 확인
  → 인증 성공 시 해당 환자의 kakao_user_ids에 kakao_user_id 저장
  → 인증 성공 시 세션 저장
  → 최신 기록 조회 가능 안내

/kakao/chat 인 경우:
  → kakao_user_id 추출
  → 인증 세션 확인
  → 세션이 없으면 patient_index.json 에서 kakao_user_id 조회
  → 매핑이 없으면 인증 필요 안내 반환
  → 매핑이 있으면 해당 환자 식별 및 세션 저장
  → document_registry.json 조회
  → 해당 환자의 READY 문서 목록 확인
  → 최신 기록 조회 요청이면 document_registry.json 기준으로 최신 result/chart 안내
  → 일반 질문이면 해당 환자의 File Search Store sync_status가 READY인지 확인
  → READY 상태의 환자별 File Search Store만 Gemini 요청에 전달
  → Gemini가 evidence, status, kakaotalk_render, used_source_ids JSON 생성
  → 백엔드가 JSON 파싱 및 schema 검증
  → 백엔드가 status를 재판정하지 않고 모델 status를 유지
  → 백엔드가 used_source_ids 중복 제거 및 현재 환자 READY 문서 여부 확인
  → status != ok이면 백엔드 고정 메시지 사용, status == ok이면 kakaotalk_render 사용
  → 백엔드가 used_source_ids 기반 하단 출처 섹션만 생성하여 append
  → 카카오 simpleText 응답 반환
```

중요:

- 모델에게 status 판단을 맡기되, 백엔드는 JSON 구조, evidence 존재 여부, 현재 환자 READY 문서 여부처럼 기계적으로 확인 가능한 항목만 검사한다.
- 백엔드는 질문 내용을 기반으로 `emergency`, `blocked`, `cost_block`, `out_of_scope`, `full_doc_block` 등을 의미적으로 재판정하지 않는다.
- 프롬프트만으로 안전성을 보장하지 않으므로 JSON 구조, evidence 존재 여부, 출처 문서의 소유 환자와 READY 상태는 반드시 백엔드에서 확인한다.

## 카카오톡 첫 인사 및 인증 게이트

챗봇은 사용자 상태에 따라 항상 먼저 인사 또는 인증 안내를 해야 한다.

상태별 처리:

| 사용자 상태 | 처리 |
|---|---|
| 첫 접속 | `/kakao/auth`에서 기본 인사 + 인증 안내 |
| 재접속, 인증 세션 유효 | `{환자명}님 안녕하세요.` + 채팅 가능 안내 |
| 재접속, 인증 세션 만료 | `/kakao/auth`에서 재인증 안내 |
| 장기 미접속 | `/kakao/auth`에서 재인증 안내 |
| 미매핑 사용자 | `/kakao/auth`에서 기본 인사 + 인증 안내 |
| 미등록 사용자 | 인증 시도 후 등록된 환자 정보를 찾지 못했다는 안내 |
| `/kakao/chat` 인증 없음 | 인증 필요 안내 및 `/kakao/auth` 유도 |

중요:

- 카카오 웰컴 블록에는 개인정보 없는 기본 인사만 둔다.
- 실제 사용자별 인사, 인증 여부 판단, 최신 기록 안내는 FastAPI 스킬 서버에서 처리한다.
- 인증 전에는 환자 폴더, `meta.json`, PDF, File Search Store, Gemini API를 조회하지 않는다.
- `/kakao/chat`에서는 인증 후에도 Google Drive 환자 폴더, `meta.json`, PDF 원본을 직접 조회하지 않는다.
- `/kakao/chat`은 `patient_index.json`과 `document_registry.json`, 그리고 환자별 File Search Store만 사용한다.

## 인증 세션 정책

PoC에서는 `kakao_user_id` 기준의 임시 인증 세션을 사용한다.

권장 정책:

```text
인증 유효 시간: 1일
1일 이내 재접속: 인증 유지
1일 초과 재접속: 재인증
서버 재시작으로 세션 소실: 재인증
```

인증 입력 형식:

```text
{이름} {생년월일}
```

예시:@

```text
손창선 19461230
```

인증 실패 시 어떤 항목이 틀렸는지 구체적으로 알려주지 않는다.

## 인증 초기화 정책

기본적으로 하나의 `kakao_user_id`는 한 명의 환자에게만 연결된다.

사용자가 아래 명령을 입력하면 인증 초기화를 수행한다.

```text
인증 초기화
다른 환자 인증
환자 변경
재인증
```

인증 초기화 시 서버는 다음 작업을 수행한다.

```text
현재 kakao_user_id가 포함된 모든 patients[].kakao_user_ids에서 해당 값을 제거
→ 현재 인증 세션 삭제
→ 사용자를 인증 대기 상태로 전환
→ 이름과 생년월일 입력 요청
```

인증 초기화 후 사용자가 이름과 생년월일 인증에 성공하면, 해당 환자의 `kakao_user_ids`에 현재 `kakao_user_id`를 저장한다.

주의:

- 인증 초기화는 기존 환자 매핑을 제거하는 동작이다.
- 인증 초기화 후 다른 환자로 인증하면 이후 재접속 시 새 환자로 자동 식별된다.
- 일시적으로 다른 환자를 조회한 뒤 기존 환자로 자동 복귀하는 기능은 PoC 범위에 포함하지 않는다.
- 인증 초기화 시에도 환자 폴더, PDF, File Search Store, Gemini API를 조회하지 않는다.

## Google Drive 폴더 구조

Google Drive 루트 폴더명은 반드시 아래와 같다.

```text
medical-chatbot/
```

폴더 구조는 아래 규칙을 따른다.

```text
medical-chatbot/
│
├── patients/
│   ├── P0001_손창선_19461230/
│   │   ├── result_20260421.pdf
│   │   ├── chart_20260421.pdf
│   │   └── meta.json  (선택)
│   │
│   └── P0002_홍길동_19800515/
│       ├── result_20260410.pdf
│       └── meta.json  (선택)
│
└── _system/
    ├── patient_index.json
    └── document_registry.json
```

## 파일 네이밍 규칙

환자 폴더명:

```text
{환자ID}_{이름}_{생년월일}
```

의료 문서 파일명:

```text
{type}_{YYYYMMDD}.pdf
```

예시:

```text
result_20260421.pdf
chart_20260421.pdf
image_20260421.pdf
```

문서 타입:

| type   | 의미              |
| ------ | --------------- |
| result | 검사결과지           |
| chart  | 진료기록부           |
| image  | 영상 판독 문서, 향후 확장 |

최신 문서는 **Google Drive 수정 시간 기준이 아니라 파일명 날짜 suffix 기준**으로 판단한다.

## patient_index.json 역할

`_system/patient_index.json`은 환자 인증과 환자 폴더 조회의 기준.

예시:

```json
{
  "patients": [
    {
      "patient_id": "P0001",
      "name": "손창선",
      "birth": "19461230",
      "folder_name": "P0001_손창선_19461230",
      "kakao_user_ids": ["kakao-user-id-1"],
      "phone_last4": "",
    }
  ]
}
```

반드시 지킬 것:

- 환자 인증은 반드시 `patient_index.json`을 먼저 조회한다.
- `kakao_user_id`가 `patient_index.json`에 없으면 먼저 인증을 요청한다.
- 이름과 생년월일 인증이 성공하면 해당 `kakao_user_id`를 `patient_index.json`의 `kakao_user_ids`에 저장한다.
- 저장된 `kakao_user_id`는 이후 재접속 시 환자 식별에 바로 사용한다.
- 환자 폴더는 직접 조합하지 말고 `folder_name` 값을 사용한다.
- 자동 생성 과정에서 기존 `kakao_user_ids`를 임의로 삭제하거나 덮어쓰지 않는다.
- 단, 사용자가 명시적으로 인증 초기화를 요청한 경우에만 해당 `kakao_user_id`를 제거한다.

## document_registry.json 역할

`_system/document_registry.json`은 Google Drive 문서와 Gemini File Search Store 동기화 상태를 기록하는 운영 장부다.

예시:

```json
{
  "generated_at": "2026-04-28T12:10:00+09:00",
  "documents": [
    {
      "patient_id": "P0001",
      "filename": "result_20260421.pdf",
      "document_type": "result",
      "document_date": "20260421",
      "drive_file_id": "google-drive-file-id",
      "drive_modified_time": "2026-04-21T10:00:00Z",
      "file_hash": "sha256...",
      "file_search_store_name": "fileSearchStores/patient_P0001",
      "file_search_document_name": "fileSearchStores/patient_P0001/documents/abc",
      "sync_status": "READY",
      "synced_at": "2026-04-28T12:10:00+09:00"
    }
  ]
}
```

`sync_status`는 아래 값을 사용한다.

| 상태 | 의미 |
|---|---|
| PENDING | 동기화 필요 |
| INDEXING | File Search Store 업로드 또는 인덱싱 진행 중 |
| READY | 챗봇 답변에 사용 가능 |
| FAILED | 동기화 실패 |
| STALE | Drive 문서가 변경되어 재동기화 필요 |
| SKIPPED | 파일명 규칙 미준수 등으로 제외 |

## File Search Store 자동 동기화 정책

챗봇 답변에는 기본적으로 Gemini File Search Store를 사용한다.

동기화 스크립트는 다음 순서로 동작한다.

```text
patient_index.json 읽기
→ 각 환자의 folder_name에 해당하는 Google Drive 폴더 조회
→ 폴더 내 PDF 파일 목록 조회
→ {type}_{YYYYMMDD}.pdf 규칙을 따르는 파일만 동기화 대상으로 선택
→ 환자별 File Search Store가 없으면 생성
→ document_registry.json 확인
→ 아직 동기화되지 않았거나 Drive 수정 시간 또는 file_hash가 변경된 파일만 File Search Store에 업로드
→ 업로드 및 인덱싱 완료 확인
→ document_registry.json에 READY 상태로 기록
```

## meta.json 역할

각 환자 폴더의 `meta.json`은 선택 사항이다.

`meta.json`이 있으면 환자의 최신 진료일, 보유 문서 목록, 최신 기록 요약을 담는 캐시 역할을 한다.
`meta.json`이 없으면 서버는 환자 폴더의 실제 PDF 파일 목록으로 동일한 정보를 계산한다.

예시:

```json
{
  "patient_id": "P0001",
  "name": "손창선",
  "birth": "19461230",
  "latest_visit_date": "20260421",
  "files": [
    {
      "type": "result",
      "date": "20260421",
      "filename": "result_20260421.pdf",
      "description": "검사결과지"
    },
    {
      "type": "chart",
      "date": "20260421",
      "filename": "chart_20260421.pdf",
      "description": "진료기록부"
    }
  ],
  "latest_summary": {
    "date": "20260421",
    "title": "2026년 4월 21일 진료 및 검사 기록",
    "description": "최근 검사결과지와 진료기록부가 등록되어 있습니다."
  }
}
```

## `/kakao/auth` 처리 순서

```text
kakao_user_id 추출
→ 사용자 메시지 추출
→ 인증 초기화 명령인지 확인

인증 초기화 명령인 경우:
    → patient_index.json 조회
    → 모든 patients[].kakao_user_ids에서 현재 kakao_user_id 제거
    → patient_index.json 저장
    → 인증 세션 삭제
    → 이름 + 생년월일 입력 안내 반환

일반 인증 요청인 경우:
    → 인증 세션 확인
    → 세션이 유효하면 인증 완료 안내 반환
    → 세션이 없으면 patient_index.json에서 kakao_user_id 조회
    → 매핑이 있으면 환자 식별 및 세션 저장
    → 매핑이 없으면 이름 + 생년월일 입력 안내 반환

이름 + 생년월일 입력인 경우:
    → patient_index.json 조회
    → 이름과 생년월일로 환자 확인
    → 환자가 없으면 미등록 안내 반환
    → 환자가 있으면 해당 환자의 kakao_user_ids에 kakao_user_id 저장
    → 동일 kakao_user_id가 다른 환자에 남아 있으면 제거
    → patient_index.json 저장
    → 인증 세션 저장
    → 인증 완료 및 /kakao/chat 사용 안내 반환
```

## `/kakao/chat` 처리 순서

```text
kakao_user_id 추출
→ 사용자 질문 추출
→ 인증 세션 확인
→ 세션이 없으면 patient_index.json에서 kakao_user_id 조회
→ 매핑이 있으면 환자 식별 및 세션 저장
→ 매핑이 없으면 인증 필요 안내 반환
→ document_registry.json 조회
→ 해당 환자의 READY 문서 목록 확인
→ READY 문서가 없으면 문서 준비 중 안내 반환
→ 최신 기록 조회 요청이면 document_registry.json 기준으로 최신 result/chart 안내
→ 일반 질문이면 해당 환자의 File Search Store만 Gemini 요청에 전달
→ Gemini evidence/status/kakaotalk_render/used_source_ids JSON 생성
→ JSON 파싱 및 schema 검증
→ 모델 status 유지
→ used_source_ids 중복 제거 및 현재 환자 READY 문서 여부 확인
→ status != ok이면 백엔드 고정 메시지 사용, status == ok이면 kakaotalk_render 사용
→ status == ok이면 하단 출처 섹션을 kakaotalk_render 뒤에 append
→ 카카오 simpleText 응답 반환
```

주의:

- `/kakao/chat`에서 Gemini가 반환한 JSON은 반드시 검증한 뒤 사용자에게 전송한다.
- `/kakao/chat`에서 백엔드는 카카오톡 본문 전체를 재렌더링하지 않고, 검증된 `kakaotalk_render`에 하단 출처 섹션만 추가한다.
- `/kakao/chat`에서 백엔드는 모델이 반환한 `status`를 의미적으로 재판정하지 않는다.
- `/kakao/chat`에서 검증에 실패하면 모델 답변을 폐기하고 안전한 실패 메시지로 대체한다.

## 최신 진단기록 선택 규칙

최신 기록 선택은 두 단계로 구분한다.

### 관리자 동기화 단계

- Google Drive 환자 폴더 내 파일 목록을 조회한다.
- `{type}_{YYYYMMDD}.pdf` 규칙을 따르는 파일만 동기화 대상으로 사용한다.
- Google Drive 수정 시간은 최신 진료일 판단 기준으로 사용하지 않는다.
- 파일명 날짜 suffix인 `YYYYMMDD`를 `document_date`로 사용한다.
- 동기화 결과는 `_system/document_registry.json`에 기록한다.

### 카카오 채팅 단계

- `/kakao/chat`은 Google Drive 환자 폴더를 조회하지 않는다.
- `/kakao/chat`은 `document_registry.json`에서 현재 환자의 `sync_status == "READY"` 문서만 사용한다.
- 최신 기록은 `document_registry.json`의 `document_date` 기준으로 계산한다.
- 기본 안내 대상은 최신 `result`, 최신 `chart`이다.
- `image` 문서는 기본 질의에서는 제외하고 향후 확장용으로 둔다.

## Gemini 프롬프트 템플릿

Gemini 호출 시 아래 3개 템플릿을 사용한다.
모델은 `evidence`를 먼저 추출한 뒤 `status`를 판정하고, 최종 사용자 본문은 `kakaotalk_render`에 작성한다.
백엔드는 `kakaotalk_render` 전체를 재렌더링하지 않고, `used_source_ids`가 현재 환자의 READY 문서인지 확인하여 하단 출처 섹션만 생성한다.

### SYSTEM_INSTRUCTION_TEMPLATE

```python
SYSTEM_INSTRUCTION_TEMPLATE = """당신은 환자가 자신의 의료 문서를 이해하도록 돕는 의료 문서 해설 전문가입니다.
제공된 의료 문서(Context)에 근거하여 환자가 이해하기 쉬운 카카오톡 메시지 형태로 답변을 생성하고 JSON으로 응답합니다.

[페르소나]
- 전문성: 모든 답변의 근거는 오직 제공된 의료 문서 내에 있어야하며 차분하고 객관적으로 설명합니다.
- 정중한 교정: 사용자가 잘못된 의학 정보나 오해를 언급하면, 제공된 문서를 기반하여 정중하게 사실을 바로잡아야 합니다.
- 따뜻한 공감: 사용자의 걱정에 공감하며 따뜻하게 답변하세요.
- 역할 제한: 새로운 진단, 처방 변경 등 의사의 역할을 대신하지 않습니다. 
- 중립성: 의료진, 병원, 의료 체계에 대한 비판이나 부정적인 의견에 동조하거나 언급하지 마세요. 중립적인 태도를 유지합니다.

[핵심 원칙: 할루시네이션 방지]
1. 문서 근거: 오직 제공된 'File Search' 결과 내 본문에서 직접 확인되는 내용만 답변합니다. 절대 추측하거나 외부 지식으로 보완하지 마세요.
2. status 판정: 반드시 추출된 근거(evidence)를 먼저 확인한 후, 그 내용이 응급/보안/범위 외 등에 해당되는지 status를 최종 판정하세요.
3. 근거 부족: 질문에 대한 답이 문서에 없거나 부족한 경우, status를 "cannot_verify"로 판정합니다.
3. 질문 범위: 사용자가 묻는 내용에만 답변하며, 관련 없는 정보나 수치 나열은 철저히 배제합니다.

[상황별 상태 코드(status) 판정 규칙]
- emergency: 흉통, 호흡곤란, 의식저하, 마비, 심한 출혈, 극심한 통증 등 응급 징후 감지 시 아래와 같이 답합니다.
"🚨 즉시 의료기관을 방문하시길 바랍니다."
- blocked: 이름(성함) 제외 개인정보(주소, 주민번호, 환자번호, 연락처 등) 요청 시 아래와 같이 답합니다.
"🔒 개인정보 보호 정책에 따라 성함 이외의 세부 개인정보는 안내해 드리지 않습니다."
- full_doc_block: 문서 전문/원문 전체/전체 검사결과 요청 시 아래와 같이 답합니다.
"📑 의료 보안 정책에 따라 문서 전문 출력이 제한되며, 궁금하신 특정 항목에 대해 요약해 드릴 수 있습니다."
- cost_block: 비용, 보험 청구, 결제 관련 질문 시 아래와 같이 답합니다.
 "💳 비용 관련 정보는 해당 의료기관에 직접 문의하셔야 합니다."
- out_of_scope: 의료/건강과 무관한 일상 질문 시 아래와 같이 답합니다.
"💬 의료 기록과 관련된 질문에만 답변을 드릴 수 있습니다."
- cannot_verify: 문서 내에서 답변 근거를 찾을 수 없는 경우 아래와 같이 답합니다.
"🔍 해당 내용은 제공된 진단 기록에서 확인하기 어렵습니다."
- ok: 문서에 근거하여 정상적인 답변이 가능할 때.

[카카오톡 렌더링 규칙]
제공된 서식 도구를 효과적으로 활용하여 사용자가 카카오톡에서 한 눈에 내용을 파악할 수 있도록 가독성을 고려하세요.
체계적이고 이해하기 쉬운 답변을 작성하되, 텍스트가 빽빽하게 나열된 형태는 피하세요.

1. 마크다운 사용 금지: #, ##, *, **, > 과 같은 마크다운 기호를 사용하지 않습니다.
2. 헤더 구분자: 섹션의 시작은 섹션 내용에 맞는 이모지를 사용하여 명확히 구분합니다.
3. 강조 프레임: 강조가 필요한 키워드는 [대괄호]를 사용합니다.
4. 리스트: 나열식 정보는 반드시 '-' 하이픈 기호를 사용하고, 항목 간 줄바꿈을 적용합니다.
5. 여백: 문단 사이는 반드시 2번의 줄바꿈 (\n\n)을 사용하여 여백을 확보합니다.

[출력 형식]
반드시 아래 JSON Schema를 준수하여 응답하세요.
{
  "evidence": "판단의 근거가 된 문서 내 실제 문구를 가장 먼저 추출하여 기록",
  "status": "enum(ok, emergency, blocked, out_of_scope, cost_block, full_doc_block, cannot_verify)",
  "kakaotalk_render": "status와 페르소나에 맞춰 작성된 최종 메시지 본문",
  "used_source_ids": ["File Search에서 참조한 문서 ID 리스트"]
}
"""
```

### CONTEXT_NOTE_TEMPLATE

```python
CONTEXT_NOTE_TEMPLATE = """현재 분석 중인 동일 환자의 의료 문서 목록입니다.
답변 시 실제로 참조한 문서의 ID를 used_source_ids 배열에 담으세요.

문서 목록:
{document_descriptions}
"""
```

### QUESTION_PROMPT_TEMPLATE

```python
QUESTION_PROMPT_TEMPLATE = """사용자 질문:
{question}

위 질문에 대해 제공된 문서에 근거하여 JSON 객체 하나만 반환하세요.

[수행 단계]
0. [Scope]
사용자의 질문 범위를 요약하여 필요한 검색 범위를 확정하다.
질문에 없는 내용을 추가하거나 확장하지 않는다.

1. [PreJudge]
질문 자체만으로 차단 대상(개인정보, 응급, 비용 등)인지 먼저 판단한다.
- 환자 성함 외 개인정보 요청: blocked
- 현재 응급 상황으로 보이는 표현: emergency
- 의료 기록과 무관한 질문: out_of_scope
- 비용, 보험, 청구 관련 질문: cost_block
- 문서 전문, 전체 원문, 전체 복사 요청: full_doc_block

2. [Extract]
File Search를 통해 질문과 관련된 정확한 근거 문구를 찾아 evidence 필드에 기록한다.

3. [Verify]
추출된 evidence가 질문을 충족하는지 검증한다. 
직접 근거가 없거나 부족하면 status는 cannot_verify로 설정한다.

4. [Judge]
모든 분석 결과를 종합하여 가장 적합한 최종 'status'를 결정한다.
- evidence가 충분하면 ok
- evidence가 없거나 불충분하면 cannot_verify
- 사전 차단 대상이면 해당 block status 유지

5. [Render]
최종 status와 카카오톡 렌더링 규칙에 따라 kakaotalk_render를 작성한다.

[금지 규칙]
- 질문 범위를 벗어난 정보는 출력하지 마세요.
- 문서에 없는 내용을 추측하거나 지어내지 마세요.
- 이름(성함) 이외의 개인정보를 출력하지 마세요.
- 질문과 무관한 검사 결과를 나열하지 마세요.
- 모든 검사 항목을 요약하려고 하지 마세요.
- 생활습관 조언을 반복하지 마세요.
- 의료와 무관한 질문이거나 비용 문의인 경우 거절하세요.
- 의료진과 병원 비판 내용이 포함된 경우 중립적인 태도를 유지하세요.
- 단순 질문에 긴 문단의 답변을 사용하지 마세요.
"""
```

## Gemini 답변 원칙

Gemini는 반드시 제공된 환자 PDF 문서와 File Search 결과에 근거해서만 답변한다.

Gemini는 아래 순서로 동작해야 한다.

```text
사용자 질문 범위 확인
→ 차단 대상 사전 판단
→ File Search 근거 문구 추출
→ evidence 충족 여부 검증
→ status 최종 판단
→ kakaotalk_render 작성
→ used_source_ids 기록
```

금지:

- 문서에 없는 내용 추측
- 외부 의학 지식으로 보완
- 진단 확정
- 처방 변경 제안
- 약 복용 중단 권유
- 응급 여부 단정
- “괜찮습니다”, “문제 없습니다”처럼 단정적으로 안심시키기
- 질문 범위를 벗어난 검사 결과 나열
- 이름을 제외한 개인정보 출력
- 출처 섹션 직접 작성
- 파일명, `.pdf`, `source_id`, `fileSearchStores/`, `drive_file_id`를 `kakaotalk_render`에 포함

Gemini는 아래 역할만 수행한다.

- 질문에 필요한 실제 문서 근거를 `evidence`에 먼저 기록
- 최종 상태를 `status`로 표시
- 사용자에게 보여줄 최종 본문을 `kakaotalk_render`로 작성
- 실제 참조한 문서 ID를 `used_source_ids`에 포함

## Gemini JSON 출력 계약

Gemini는 반드시 JSON 객체 하나만 반환한다.
마크다운 코드블록, 자유 텍스트, 출처 문장, 별도 설명을 반환하지 않는다.

최소 출력 형식:

```json
{
  "evidence": "진료기록부에 '고혈압'이 기재되어 있음",
  "status": "ok",
  "kakaotalk_render": "🩺 확인된 내용\n\n제공된 진료 기록에서 [고혈압] 관련 기록이 확인됩니다.\n\n다만 이 내용은 제공된 문서에 적힌 기록을 설명드리는 것이며, 새로운 진단이나 치료 판단은 담당 의료진과 상담해 주세요.",
  "used_source_ids": ["chart_20260421.pdf"]
}
```

허용 필드:

| 필드 | 설명 |
|---|---|
| `evidence` | 판단의 근거가 된 문서 내 실제 문구 또는 직접 확인된 근거 요약 |
| `status` | 답변 상태 |
| `kakaotalk_render` | 카카오톡에 표시할 최종 본문. 단, 출처 섹션은 포함하지 않음 |
| `used_source_ids` | 답변에 실제 사용한 문서 ID 목록 |

허용 `status`:

| status | 의미 |
|---|---|
| `ok` | 제공된 문서 근거로 답변 가능 |
| `emergency` | 응급 징후 표현 포함 |
| `blocked` | 개인정보 보호 정책상 답변 차단 |
| `out_of_scope` | 의료 기록과 무관한 질문 |
| `cost_block` | 비용, 결제, 보험금, 청구 관련 질문 |
| `full_doc_block` | 문서 전문, 원문 전체, 전체 검사결과 원문 요청 |
| `cannot_verify` | 제공된 문서에서 확인 불가 |

## kakaotalk_render 작성 규칙

`kakaotalk_render`는 사용자가 실제로 보게 될 본문이다.
단, 하단의 `📄 출처 ({출처 개수}건)` 섹션은 Gemini가 작성하지 않는다.

본문 작성 규칙:

- 마크다운 제목, 볼드체, 이탤릭체, 표 서식을 사용하지 않는다.
- `#`, `##`, `*`, `**`, `>` 기호를 사용하지 않는다.
- 섹션 시작은 내용에 맞는 이모지로 구분한다.
- 강조가 필요한 키워드는 `[대괄호]`를 사용한다.
- 나열식 정보는 `-` 하이픈을 사용하고 항목 사이에 줄바꿈을 적용한다.
- 문단 사이는 `\n\n`으로 구분한다.
- 단순 질문에는 짧고 직접적으로 답한다.
- 답변 본문에 출처 헤더, 출처 번호 목록, 파일명, 문서 ID를 넣지 않는다.

상태별 권장 본문:

```python
STATUS_RENDER_MESSAGES = {
    "emergency": "🚨 즉시 의료기관을 방문하시길 바랍니다.",
    "blocked": "🔒 개인정보 보호 정책에 따라 성함 이외의 세부 개인정보는 안내해 드리지 않습니다.",
    "full_doc_block": "📑 의료 보안 정책에 따라 문서 전문 출력이 제한되며, 궁금하신 특정 항목에 대해 요약해 드릴 수 있습니다.",
    "cost_block": "💳 비용 관련 정보는 해당 의료기관에 직접 문의하셔야 합니다.",
    "out_of_scope": "💬 의료 기록과 관련된 질문에만 답변을 드릴 수 있습니다.",
    "cannot_verify": "🔍 해당 내용은 제공된 진단 기록에서 확인하기 어렵습니다.",
}
```

## 백엔드 JSON schema 검증

백엔드는 Gemini 응답을 사용자에게 전송하기 전에 반드시 schema를 검증한다.
검증 대상은 JSON 구조, 허용된 status 값, evidence 존재 여부, `kakaotalk_render` 안전성, 그리고 `used_source_ids`가 현재 인증된 환자의 READY 문서인지 여부다.

권장 Pydantic 구조:

```python
from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator

Status = Literal[
    "ok",
    "emergency",
    "blocked",
    "out_of_scope",
    "cost_block",
    "full_doc_block",
    "cannot_verify",
]

STATUS_RENDER_MESSAGES = {
    "emergency": "🚨 즉시 의료기관을 방문하시길 바랍니다.",
    "blocked": "🔒 개인정보 보호 정책에 따라 성함 이외의 세부 개인정보는 안내해 드리지 않습니다.",
    "full_doc_block": "📑 의료 보안 정책에 따라 문서 전문 출력이 제한되며, 궁금하신 특정 항목에 대해 요약해 드릴 수 있습니다.",
    "cost_block": "💳 비용 관련 정보는 해당 의료기관에 직접 문의하셔야 합니다.",
    "out_of_scope": "💬 의료 기록과 관련된 질문에만 답변을 드릴 수 있습니다.",
    "cannot_verify": "🔍 해당 내용은 제공된 진단 기록에서 확인하기 어렵습니다.",
}

FORBIDDEN_RENDER_TOKENS = [
    ".pdf",
    "source_id",
    "fileSearchStores/",
    "drive_file_id",
    "chart_",
    "result_",
    "image_",
    "📄 출처",
]

FORBIDDEN_MARKDOWN_TOKENS = ["#", "##", "**", "*", ">"]

class ModelAnswer(BaseModel):
    evidence: str = Field(min_length=1, max_length=2000)
    status: Status
    kakaotalk_render: str = Field(min_length=1, max_length=1200)
    used_source_ids: list[str] = Field(default_factory=list, max_length=10)

    model_config = {"extra": "forbid"}

    @field_validator("kakaotalk_render")
    @classmethod
    def validate_kakaotalk_render(cls, value: str) -> str:
        if any(token in value for token in FORBIDDEN_RENDER_TOKENS):
            raise ValueError("kakaotalk_render must not expose source ids or source section")
        if any(token in value for token in FORBIDDEN_MARKDOWN_TOKENS):
            raise ValueError("kakaotalk_render must not contain markdown syntax")
        return value.strip()

    @field_validator("used_source_ids")
    @classmethod
    def validate_used_source_ids(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]

    @model_validator(mode="after")
    def validate_ok_sources(self):
        if self.status == "ok" and not self.used_source_ids:
            raise ValueError("ok answer must include used_source_ids")
        return self
```

## 백엔드 검증 정책: status 재판정 금지

모델이 반환한 `status`는 최종 상태값으로 사용한다.
백엔드는 질문 내용을 다시 해석하여 `emergency`, `blocked`, `cost_block`, `out_of_scope`, `full_doc_block`, `cannot_verify`, `ok` 중 하나로 재분류하지 않는다.
즉, status 판정의 책임은 Gemini 프롬프트와 모델 출력에 둔다.

백엔드는 status를 바꾸는 대신, 아래 항목만 검사한다.

```text
JSON 파싱 성공 여부
schema 유효성
status 값이 허용 enum에 포함되는지 여부
status에 따른 최종 메시지 출력 가능 여부
evidence 존재 여부
ok 답변의 used_source_ids가 비어 있지 않은지 여부
used_source_ids가 document_registry.json의 현재 인증된 환자 READY 문서에 존재하는지 여부
status == ok인 kakaotalk_render에 출처 섹션, 내부 ID, 파일명, 마크다운 금지 문법이 포함되지 않았는지 여부
```

중요:

- 출처 검증은 의도적으로 느슨하게 유지한다.
- 백엔드는 `used_source_ids`가 이번 File Search 응답의 특정 chunk와 정확히 매칭되는지까지 확인하지 않는다.
- 백엔드는 `used_source_ids`가 `document_registry.json`에서 현재 인증된 환자에게 속하고, `sync_status == "READY"`인 문서인지 여부만 확인한다.
- 백엔드는 `evidence`가 의학적으로 충분한지, 답변 문장과 근거가 의미적으로 완전히 일치하는지 재판정하지 않는다.
- 백엔드는 사용자 질문을 다시 분석해서 응급, 개인정보, 비용, 문서 전문 요청 여부를 판정하지 않는다.
- 백엔드는 모델의 `status`가 허용 enum인지 확인하고, `status != ok`이면 모델 본문 대신 백엔드 고정 메시지를 출력한다.
- 검증 실패는 status 재판정이 아니라 [모델 응답 사용 불가]로 처리한다.
- 모델 응답 사용 불가 시에는 안전한 실패 메시지를 반환하고 출처 섹션을 출력하지 않는다.

### status별 메시지 출력 처리

`status == "ok"`인 경우:

```text
- evidence가 비어 있으면 실패
- used_source_ids가 비어 있으면 실패
- used_source_ids가 document_registry.json의 현재 인증된 환자 READY 문서에 없으면 실패
- kakaotalk_render에 출처 섹션이나 내부 source_id가 포함되어 있으면 실패
```

`status != "ok"`인 경우:

```text
- 모델의 kakaotalk_render는 사용자에게 그대로 출력하지 않음
- status에 매핑된 백엔드 고정 메시지를 출력
- used_source_ids는 없어도 허용
- 출처 섹션은 출력하지 않음
- 질문 재분석으로 status를 바꾸지 않음
```

상태별 안내 문구는 프롬프트와 백엔드 고정 메시지가 동일해야 한다.
백엔드는 `status != ok`인 경우 모델의 `kakaotalk_render`를 사용하지 않고 아래 고정 메시지를 최종 사용자 메시지로 사용한다.

```python
STATUS_RENDER_MESSAGES = {
    "emergency": "🚨 즉시 의료기관을 방문하시길 바랍니다.",
    "blocked": "🔒 개인정보 보호 정책에 따라 성함 이외의 세부 개인정보는 안내해 드리지 않습니다.",
    "full_doc_block": "📑 의료 보안 정책에 따라 문서 전문 출력이 제한되며, 궁금하신 특정 항목에 대해 요약해 드릴 수 있습니다.",
    "cost_block": "💳 비용 관련 정보는 해당 의료기관에 직접 문의하셔야 합니다.",
    "out_of_scope": "💬 의료 기록과 관련된 질문에만 답변을 드릴 수 있습니다.",
    "cannot_verify": "🔍 해당 내용은 제공된 진단 기록에서 확인하기 어렵습니다.",
}
```

권장 검증 흐름:

```text
1. JSON 파싱 실패 → 모델 응답 사용 불가
2. schema 오류 → 모델 응답 사용 불가
3. status enum 오류 → 모델 응답 사용 불가
4. kakaotalk_render 금지 토큰 포함 → 모델 응답 사용 불가
5. status != ok → 모델 kakaotalk_render 미사용, status별 백엔드 고정 메시지 출력
6. status == ok 이지만 evidence가 없거나 used_source_ids가 없음 → 모델 응답 사용 불가
7. status == ok 이지만 used_source_ids가 현재 환자 READY 문서가 아님 → 모델 응답 사용 불가
8. 그 외 → 모델 응답 사용
```

모델 응답 사용 불가 시에는 아래 안전 실패 메시지를 반환한다.
이 처리는 status 재판정이 아니라 파싱, schema, evidence 존재 여부, 현재 환자 READY 문서 확인 실패에 대한 fail-closed 처리다.

```python
SAFE_FALLBACK_MESSAGE = "🔍 해당 내용은 제공된 진단 기록에서 확인하기 어렵습니다."
```

## 출처 처리 정책

모델은 출처 문장을 직접 만들지 않는다.
모델은 `used_source_ids`만 반환한다.
백엔드는 `used_source_ids`를 받아 최종 메시지 하단의 `📄 출처 ({출처 개수}건)` 섹션만 생성한다.

백엔드 처리 순서:

```text
used_source_ids 수신
→ 중복 제거
→ document_registry.json에서 현재 인증된 환자의 READY 문서에 존재하는 source_id만 유지
→ 안전한 source_map으로 문서명과 날짜 변환
→ 최종 표시 대상 출처 개수 계산
→ 검증된 kakaotalk_render 본문 아래에 📄 출처 ({출처 개수}건) 섹션 append
```

중복 제거 함수:

```python
def dedupe_preserve_order(items: list[str]) -> list[str]:
    seen = set()
    result = []

    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)

    return result
```

문서 타입 표시명:

```python
DOCUMENT_TYPE_LABELS = {
    "result": "검사결과지",
    "chart": "진료기록부",
    "image": "영상검사자료",
}
```

출처 개수 표시 규칙:

```text
출처 헤더에는 최종 표시 대상 출처 개수를 괄호로 표시한다.
출처 개수는 중복 제거와 현재 환자 READY 문서 확인을 통과한 뒤 실제로 사용자에게 표시되는 출처 수를 기준으로 한다.
ok 답변에서 검증 후 표시 가능한 출처가 0건이면 모델 응답 사용 불가로 처리하고 안전 실패 메시지를 출력한다.
status가 ok가 아닌 차단/거절 응답은 출처 섹션을 출력하지 않는다.
```

출처 렌더링 예시:

```text
📄 출처 (2건)
1. 진료기록부, 2026년 4월 21일
2. 검사결과지, 2026년 4월 21일
```

주의:

- 카카오톡 최종 메시지 하단 출처 섹션에만 문서 표시명을 사용한다.
- 카카오톡 최종 메시지에는 Google Drive ID, File Search document name, 내부 source_id를 노출하지 않는다.
- `kakaotalk_render` 안에 출처 섹션이 포함되어 있으면 schema 오류로 처리한다.

## 카카오톡 최종 메시지 조립 정책

백엔드는 카카오톡 본문 형식을 새로 렌더링하지 않는다.
백엔드는 모델 status가 `ok`이면 검증된 `kakaotalk_render`를 본문으로 사용하고, `used_source_ids`가 현재 환자의 READY 문서로 확인된 경우에만 출처 섹션을 하단에 추가한다.

조립 규칙:

```text
final_text = validated_kakaotalk_render

if model_answer.status == "ok" and validated_sources:
    final_text += "\n\n" + render_source_section(validated_sources)
```

`render_source_section`만 백엔드가 담당한다.
그 외 카카오톡 본문 가독성, 섹션 구분, 리스트 구성은 Gemini의 `kakaotalk_render`에서 생성하고 백엔드는 구조적 안전성만 검사한다.

## 개인정보 보호 원칙

반드시 지킬 것:

- 환자 정보와 의료 문서 내용을 로그에 출력하지 않는다.
- Gemini 프롬프트 전체를 로그에 출력하지 않는다.
- PDF 원문 내용을 로그에 남기지 않는다.
- Gemini 원본 JSON 응답에 개인정보가 포함될 수 있으므로 운영 로그에 원문 전체를 남기지 않는다.
- `.env`를 Git에 커밋하지 않는다.
- 서비스 계정 키를 Git에 커밋하지 않는다.
- 다른 환자의 PDF나 File Search Store를 Gemini에 전달하지 않는다.
- 관리자 동기화 로그에는 환자명 대신 `patient_id`, `drive_file_id`, 상태값 중심으로 기록한다.
- `patient_index.json` 저장 시 동시성 충돌을 막기 위해 파일 잠금 또는 원자적 쓰기를 사용한다.

이름을 제외한 아래 정보는 모두 개인정보로 간주하며 사용자에게 출력하지 않는다.

```text
주민등록번호
생년월일
나이
성별
환자번호
전화번호
이메일
주소
상세주소
우편번호
보호자명
계좌번호
카드번호
보험번호
파일 안에 포함된 모든 개인 식별 정보
```

## 카카오 응답 형식

기본 응답은 `simpleText`를 사용한다.

```json
{
  "version": "2.0",
  "template": {
    "outputs": [
      {
        "simpleText": {
          "text": "검증된 kakaotalk_render + 백엔드가 생성한 하단 출처 섹션"
        }
      }
    ]
  }
}
```

오류가 발생해도 내부 에러를 노출하지 않는다.
Gemini JSON 원문을 사용자에게 노출하지 않는다.
검증 실패 시에는 안전 실패 메시지를 반환한다.

## 카카오톡 출력 규칙

최종 출력은 `kakaotalk_render` 본문과 백엔드 출처 섹션을 결합해 만든다.
백엔드는 본문 전체를 카카오톡 형식으로 재렌더링하지 않고, 출처 하단 섹션만 생성한다.

- 일반 텍스트만 사용한다.
- 마크다운 제목, 볼드체, 이탤릭체, 표 서식을 사용하지 않는다.
- `#`, `*`, `**`, `>` 기호를 사용하지 않는다.
- 한 문장은 가급적 짧게 구성한다.
- 의미 단위로 줄바꿈을 수행한다.
- 문단 사이는 `\n\n` 기준으로 여백을 확보한다.
- 본문 섹션 시작에는 내용에 맞는 이모지를 사용할 수 있다.
- 리스트 항목 시작점에는 하이픈만 사용한다.
- 출처 섹션은 반드시 백엔드가 마지막에만 추가한다.
- `kakaotalk_render`에는 `📄 출처` 섹션을 포함하지 않는다.

## 로컬 개발

```bash
uvicorn app.main:app --reload --port 8000
```

```bash
ngrok http 8000
```

카카오 오픈빌더 Skill URL:

```text
https://{ngrok-domain}/kakao/auth
https://{ngrok-domain}/kakao/chat
```

## 배포

PoC 배포는 GCP Cloud Run을 사용한다.

```text
Cloud Run HTTPS URL → 카카오 오픈빌더 Skill URL 등록
```

등록 대상:

```text
https://{cloud-run-domain}/kakao/auth
https://{cloud-run-domain}/kakao/chat
```

## 구현 금지 사항

- `patient_index.json` 확인 없이 환자 폴더부터 검색하지 않는다.
- `patient_index.json`에 없는 환자를 인증된 환자로 처리하지 않는다.
- 이름/생년월일 확인 없이 새로운 `kakao_user_id`를 환자에 저장하지 않는다.
- 실제 파일 목록 확인 없이 `meta.json`만 믿고 최신 기록을 판단하지 않는다.
- Google Drive 수정 시간으로 최신 문서를 판단하지 않는다.
- 파일명 규칙에 맞지 않는 PDF를 Gemini에 전달하지 않는다.
- 여러 환자 폴더를 동시에 검색해 답변하지 않는다.
- 다른 환자의 문서를 사용하지 않는다.
- 의료적 판단을 단정하지 않는다.
- Gemini 자유 텍스트 응답을 허용하지 않는다.
- Gemini가 출처 섹션을 작성하지 않도록 하며, 작성된 경우 검증 실패로 처리한다.
- `status == ok`인 경우에만 Gemini의 `kakaotalk_render` 본문을 사용한다.
- Google Drive ID, File Search document name을 카카오톡 응답에 노출하지 않는다.
- 백엔드가 질문 내용을 기반으로 모델 `status`를 의미적으로 재판정하지 않는다.
- `status != ok`인 경우 모델의 `kakaotalk_render`를 사용하지 않고 백엔드 고정 메시지를 출력하며, 출처 섹션은 출력하지 않는다.