# Architecture

## 프로젝트 개요

- 의료진단기록 카카오톡 + Gemini 챗봇을 구현한다.
- 환자가 카카오채널 챗봇으로 질문하면, 서버는 현재 인증된 환자의 Google Drive 원본 의료 자료를 기준으로 필요한 원본 파일을 Gemini Files API에 준비하고, 선택된 원본 파일 전체 또는 문서 없는 질문 context를 Gemini에 전달하여 답변 JSON을 생성한다.
- 기존 Gemini File Search Store 기반 chunk-level RAG는 사용하지 않는다.
- MVP에서는 원본 의료 문서를 Markdown 전문으로 파싱한 파일을 미리 저장하지 않는다.
- Google Drive는 환자별 원본 의료 문서의 source of truth로 사용한다.
- Firestore는 환자 인증, 원본 source registry, Medical LLM Wiki page, Medical LLM Wiki index, Gemini Files API runtime 상태, 세션, 사용자 질문/assistant 답변 chat log, 카카오 callback job 상태, lock, retry 상태를 관리하는 운영 DB로 사용한다.
- Gemini Files API는 선택된 원본 파일을 Gemini 최종 QA 호출에 전달하기 위한 임시 파일 참조 계층으로 사용한다.
- Gemini Files API에 업로드된 파일은 원본 저장소가 아니라 runtime cache로 취급하며, 용량 한도와 만료 정책을 고려해 주기적으로 cleanup한다.
- 카카오 사용자가 직접 올린 PDF/이미지는 Google Drive source가 아니며, 인증된 카카오 세션의 최근 temporary attachment 1개로만 관리한다.
- 인증 성공, 이미 인증된 시작 블록 진입, 의료 기록 조회 시점에는 사용자 응답을 지연시키지 않고 Cloud Tasks 기반 pre-warm job을 생성해 가장 가능성이 높은 source를 미리 Files API에 준비한다.
- Gemini Router는 현재 질문이 차단 대상인지 판단하고, 차단 대상이 아니면 `OK`로 처리한 뒤 현재 인증 환자의 `medical_wiki_index`와 `medical_wiki_pages` catalog를 보고 `selected` 또는 `insufficient`와 `primary_source_id`를 결정한다.
- 최종 QA의 기준 근거는 `medical_wiki_pages`, `medical_wiki_index`, Markdown 파싱본이 아니라, Gemini Files API로 전달된 Google Drive 원본 파일 전체다.
- 관리자가 Google Drive 환자 폴더에 파일을 업로드하면 Drive Sync Worker가 Google Drive Changes API 기반 checkpoint polling으로 변경된 파일만 감지하고, Firestore의 `medical_sources`, `medical_wiki_pages`, `medical_wiki_index`, `medical_source_runtime`을 동기화한다.
- 관리자 대시보드는 Basic Auth 보호 아래 Firestore `chat_logs`를 최근 N개 또는 날짜 범위로 조회한다. 채팅 로그를 Google Drive CSV로 export하지 않는다.
- Pre-warm이 실패하거나 아직 완료되지 않은 경우에도 실제 질문 처리에서는 기존 lazy `prepare_file()` 흐름으로 fallback한다.

## 주요 기술

- Python
- FastAPI
- Uvicorn
- Kakao OpenBuilder Skill
- ngrok
- Google Drive API
- Google Drive Changes API
- Google Cloud Firestore
- Gemini API
- Gemini Files API
- GCP Cloud Run
- Cloud Scheduler
- Cloud Tasks
- Pydantic

## 핵심 아키텍처 방향

### 기존 구조

```text
Google Drive PDF
→ Gemini File Search Store
→ chunk 기반 RAG
→ 답변
```

### 변경 구조

```text
Google Drive 원본 파일
→ Drive Sync Worker가 Google Drive Changes API로 변경된 file_id 감지
→ Firestore medical_sources에 원본 source registry 저장
→ Firestore medical_wiki_pages에 source_summary page 저장
→ Firestore medical_wiki_index/main에 환자별 wiki index 저장 또는 재컴파일
→ Firestore medical_source_runtime에 Files API 상태, sync 상태, lock, retry 저장
→ 인증 성공 또는 의료 기록 조회 시 Cloud Tasks로 latest source pre-warm job 생성
→ Pre-warm Worker가 필요한 경우 선택 source를 Gemini Files API에 미리 준비
→ 사용자 질문 발생
→ 질문이 최근 업로드 파일을 가리키면 Router source 선택 전에 active attachment 기준으로 Final QA 처리
→ 현재 인증 환자의 medical_wiki_index를 Router가 먼저 읽음
→ 필요 시 관련 medical_wiki_pages를 참고
→ Gemini Router가 차단 intent 또는 OK를 판단하고, OK이면 selected 또는 insufficient를 결정
→ 차단 intent이면 백엔드가 즉시 고정 메시지 생성
→ OK + selected이면 백엔드가 선택된 source_id가 현재 환자 source인지 검증
→ 검증 성공 시 백엔드가 medical_source_runtime에서 Files API 상태 확인
→ 선택된 원본 파일만 Gemini Files API에 pre-warm 결과 재사용 또는 lazy upload
→ 준비된 원본 파일 전체와 질문을 단일 answer_question() 경로로 Gemini Final QA에 전달
→ OK + insufficient이면 문서 준비 없이 동일한 answer_question(prepared_file=None) 경로로 Gemini Final QA 호출
→ Gemini Final QA가 문서 전달 여부와 질문 내용을 기준으로 ok 또는 cannot_verify 판단
→ JSON 검증
→ 카카오톡 응답
→ 실제 전송된 assistant 답변을 patients/{patient_id}/chat_logs/{ANSWER_*}에 저장
```

## LLM Wiki 적용 원칙

Medical LLM Wiki는 기존 LLM Wiki의 `raw sources`, `wiki pages`, `index.md`, `log.md` 개념을 의료 문서 Router에 맞게 제한 적용한다.

```text
raw sources
└── patients/{patient_id}/medical_sources/{source_id}

wiki pages
└── patients/{patient_id}/medical_wiki_pages/{page_id}

index.md
└── patients/{patient_id}/medical_wiki_index/main

log.md
└── patients/{patient_id}/medical_wiki_logs/{log_id}

runtime
└── patients/{patient_id}/medical_source_runtime/{source_id}

prewarm jobs
└── gemini_file_prewarm_jobs/{job_id}
```

중요:

- Medical LLM Wiki는 답변 근거가 아니다.
- Medical LLM Wiki는 Router가 어떤 원본 source를 열어볼지 고르기 위한 navigation layer다.
- `medical_wiki_pages`는 원본 의료 문서 하나에 대응되는 안전한 `source_summary` page다.
- `medical_wiki_index`는 현재 환자의 wiki page 목록을 압축한 catalog다.
- `medical_wiki_logs`는 wiki page, index 생성과 갱신 이벤트를 남기는 append-only 운영 로그다.
- 문서 기반 최종 답변은 선택된 `source_id`가 가리키는 Google Drive 원본 파일 전체를 Gemini Files API로 전달해서 생성한다.
- source가 선택되지 않은 OK + insufficient 흐름에서는 원본 문서 없이 Final QA를 호출할 수 있으나, 개인 기록 내용은 추정하지 않는다.
- 관리자 대시보드는 기존 Cloud Run 서비스의 `/admin/dashboard`에서 제공하며 Basic Auth로 보호한다.
- 대시보드는 Firestore `chat_logs`를 최근 N개 또는 날짜 범위로 조회하고, 이전/다음 페이지로 넘겨 본다.
- 대시보드 action은 기존 sync/cleanup service를 재사용하고, 비밀번호는 운영/test 배포에서 Secret Manager로 주입한다.

## 핵심 금지 사항

- File Search Store 기반 RAG를 사용하지 않는다.
- 문서를 chunk 검색하지 않는다.
- Markdown 전문 파싱본을 최종 QA context로 사용하지 않는다.
- `medical_wiki_pages` 또는 `medical_wiki_index`만 보고 의료 답변을 생성하지 않는다.
- `medical_wiki_pages`에 원문 전문, OCR 전문, 검사 결과표 전체, 검사 수치 전체, 처방 상세, 진단명 전체 목록을 저장하지 않는다.
- `medical_wiki_index`에 `medical_wiki_pages`의 모든 내용을 복사하지 않는다. index에는 compact entry만 저장한다.
- Router에 `medical_sources.source_ref`, `medical_source_runtime.gemini_file`, lock, retry, Drive index 값을 전달하지 않는다.
- Router 결과는 항상 백엔드에서 현재 인증 환자 source인지 검증한다.
- 최종 Gemini 호출에는 현재 인증 환자의 선택된 원본 파일만 `contents`에 포함한다.
- 임시 업로드 질문에서는 현재 인증 환자와 카카오 세션에 바인딩된 active attachment만 `contents`에 포함한다.
- 다른 환자의 원본 파일, source_id, Gemini Files API file_name, file_uri를 사용하지 않는다.
- Google Drive ID, Gemini file_name, 내부 source_id, 원본 파일명을 카카오톡 응답에 노출하지 않는다.

## 역할 분리

| 구성 요소 | 역할 |
|---|---|
| Google Drive | 환자별 원본 의료 문서 저장소. source of truth |
| Drive Sync Worker | Google Drive Changes API checkpoint polling으로 업로드, 수정, 삭제를 감지하고 Firestore wiki 계층과 runtime을 동기화 |
| Firestore `patients` | 환자 인증 기준 정보와 Google Drive folder_id 관리 |
| Firestore `medical_sources` | Google Drive 원본 파일 registry와 source lifecycle 관리 |
| Firestore `medical_wiki_pages` | Router가 원본 source를 선택하기 위한 source_summary wiki page 관리 |
| Firestore `medical_wiki_index` | 환자별 wiki page catalog. Router가 먼저 읽는 index.md 역할 |
| Firestore `medical_wiki_logs` | wiki ingest, rebuild, index compile, 실패 이벤트 기록 |
| Firestore `medical_source_runtime` | Gemini Files API 상태, sync 상태, lock, lease, retry, 실패 정보 관리 |
| Firestore `active_attachments` | 인증된 카카오 세션의 최근 임시 업로드 파일 1개 관리. Google Drive source와 분리 |
| Firestore `upload_tokens` | `/upload/{token}` 접근을 위한 one-use token 상태 관리 |
| Firestore `gemini_file_prewarm_jobs` | pre-warm 작업 상태, idempotency, lock, retry, skip 또는 실패 정보 관리 |
| Gemini Files API | 선택된 원본 파일을 Gemini가 읽을 수 있게 하는 임시 파일 참조 계층 |
| Gemini Files Cleanup Worker | Files API runtime cache 사용량을 계산하고 soft, target, hard limit 기준으로 오래된 cache를 정리 |
| Gemini File Pre-warm Worker | 인증 또는 의료 기록 조회 이후 latest source를 미리 Files API에 준비 |
| Cloud Tasks | pre-warm 같은 비동기 최적화 작업을 durable HTTP task로 실행 |
| Gemini Router | 차단 intent 또는 OK를 판단하고, OK일 때 현재 환자 Medical Wiki catalog를 보고 selected 또는 insufficient와 primary_source_id를 결정 |
| Gemini Final QA | 단일 OK 흐름에서 문서 전달 여부와 질문 내용을 기준으로 일반 설명, 문서 기반 답변, cannot_verify JSON 생성. 출처 섹션은 작성하지 않음 |
| 백엔드 | 인증, 권한 필터링, Router 결과 검증, 환자 source 검증, Files API 준비, 단일 Final QA 호출, schema 검증, 출처 검증 |
