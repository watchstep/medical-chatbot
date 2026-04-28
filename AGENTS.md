## 프로젝트 개요
- 의료진단기록 카카오톡 + Gemini 챗봇 PoC 구현
- 환자가 카카오채널 챗봇으로 질문하면, 서버는 인증된 환자의 Gemini File Search Store를 사용해 문서 기반 답변을 생성한 뒤 카카오 챗봇 응답으로 반환한다. Google Drive는 원본 의료 PDF의 기준 저장소이며, 환자 인덱스 생성과 File Search Store 동기화 작업에서만 조회한다.

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
  →  READY 상태의 환자별 File Search Store만 Gemini 요청에 전달
  → READY 상태의 환자별 File Search Store만 Gemini 요청에 전달
  → 카카오 simpleText 응답 반환
```

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
인증 유효 시간: 7일
7일 이내 재접속: 인증 유지
7일 초과 재접속: 재인증
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
→ 문서 근거 답변 생성
→ 카카오 simpleText 응답 반환
```

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

## Gemini 답변 원칙

Gemini는 반드시 제공된 환자 PDF 문서에 근거해서만 답변한다.

금지:

* 문서에 없는 내용 추측
* 진단 확정
* 처방 변경 제안
* 약 복용 중단 권유
* 응급 여부 단정
* “괜찮습니다”, “문제 없습니다”처럼 단정적으로 안심시키기

답변에는 가능하면 아래 내용을 포함한다.

* 핵심 답변
* 근거 문서명
* 문서 날짜
* 관련 검사명 또는 진료기록
* 주의할 점
* 의료진에게 확인하면 좋은 질문

## 개인정보 보호 원칙

반드시 지킬 것:

- 환자 정보와 의료 문서 내용을 로그에 출력하지 않는다.
- Gemini 프롬프트 전체를 로그에 출력하지 않는다.
- PDF 원문 내용을 로그에 남기지 않는다.
- `.env`를 Git에 커밋하지 않는다.
- 서비스 계정 키를 Git에 커밋하지 않는다.
- 다른 환자의 PDF나 File Search Store를 Gemini에 전달하지 않는다.
- 관리자 동기화 로그에는 환자명 대신 `patient_id`, `drive_file_id`, 상태값 중심으로 기록한다.
- `patient_index.json` 저장 시 동시성 충돌을 막기 위해 파일 잠금 또는 원자적 쓰기를 사용한다.

## 카카오 응답 형식

기본 응답은 `simpleText`를 사용한다.

```json
{
  "version": "2.0",
  "template": {
    "outputs": [
      {
        "simpleText": {
          "text": "답변 내용"
        }
      }
    ]
  }
}
```

오류가 발생해도 내부 에러를 노출하지 않는다.

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

* `patient_index.json` 확인 없이 환자 폴더부터 검색하지 않는다.
* `patient_index.json`에 없는 환자를 인증된 환자로 처리하지 않는다.
* 이름/생년월일 확인 없이 새로운 `kakao_user_id`를 환자에 저장하지 않는다.
* 실제 파일 목록 확인 없이 `meta.json`만 믿고 최신 기록을 판단하지 않는다.
* Google Drive 수정 시간으로 최신 문서를 판단하지 않는다.
* 파일명 규칙에 맞지 않는 PDF를 Gemini에 전달하지 않는다.
* 여러 환자 폴더를 동시에 검색해 답변하지 않는다.
* 다른 환자의 문서를 사용하지 않는다.
* 의료적 판단을 단정하지 않는다.
