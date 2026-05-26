# AGENTS.md

이 파일은 Codex가 항상 읽는 최상위 작업 지침이다. 상세 설계는 `docs/` 아래 문서를 참고한다.

## Project summary

- 의료진단기록 카카오톡 + Gemini 챗봇을 구현한다.
- Google Drive는 환자별 원본 의료 문서의 source of truth다.
- Firestore는 인증, source registry, Medical Wiki metadata, Files API runtime, session, 사용자/assistant chat log, callback job, lock, retry 상태를 관리하는 운영 DB다.
- Gemini Files API는 최종 QA에 선택된 원본 파일을 전달하기 위한 임시 runtime cache다.
- 채팅 로그 Google Drive CSV export는 제공하지 않는다.
- 관리자 대시보드는 기존 서비스와 같은 Cloud Run에서 `/admin/dashboard`로 제공하며, 브라우저 Basic Auth로 보호한다.
- 관리자 대시보드는 Firestore `chat_logs`를 최근 N개 또는 날짜 범위로 조회하고, 긴 목록은 이전/다음 페이지로 넘긴다.
- 기존 Gemini File Search Store 기반 chunk-level RAG는 사용하지 않는다.
- MVP에서는 원본 의료 문서를 Markdown 전문으로 미리 저장하지 않는다.

## Non-negotiable architecture rules

- Google Drive 원본 파일을 최종 근거로 사용한다.
- Medical LLM Wiki는 답변 근거가 아니다. Router가 원본 source를 고르기 위한 navigation layer다.
- `medical_wiki_pages`와 `medical_wiki_index`만 보고 의료 답변을 생성하지 않는다.
- Router는 의료 질문에 답변하지 않는다. 차단 여부, `OK`, `selected`, `insufficient`, `primary_source_id`만 결정한다.
- Router에는 `medical_sources.source_ref`, `medical_source_runtime.gemini_file`, Drive index, lock, retry 값을 전달하지 않는다.
- `OK + selected`이면 백엔드가 source ownership, source status, runtime readiness를 검증한 뒤 Files API 파일을 준비한다.
- `OK + insufficient`이면 문서 없이 Final QA를 호출할 수 있지만, 개인 기록 내용은 추정하지 않는다.
- selected source 검증 또는 `prepare_file()` 실패 시 `prepared_file=None`으로 fallback하지 말고 안전 실패 흐름을 따른다.
- Final QA는 단일 `answer_question()` 경로를 사용한다.
- Final QA는 `kakaotalk_render` 본문만 작성한다. 출처 섹션은 백엔드가 검증된 evidence를 기준으로만 붙인다.

## Security and privacy rules

- 인증 전에는 Google Drive, `medical_sources`, `medical_wiki_pages`, `medical_source_runtime`, Gemini API를 조회하지 않는다.
- 다른 환자의 원본 파일, source_id, Gemini Files API file_name, file_uri를 사용하지 않는다.
- 최종 Gemini 호출에는 현재 인증 환자의 선택된 원본 파일만 포함한다.
- 카카오톡 응답에 Google Drive ID, Gemini file_name, file_uri, 내부 source_id, 원본 파일명, 생년월일, 환자번호, 전화번호, 주소 등 개인정보를 노출하지 않는다.
- Cloud Logging에는 사용자 질문 원문, callback_url, Drive ID, Gemini file URI, 원본 파일명을 출력하지 않는다.
- `chat_logs`에는 인증된 사용자의 질문 원문과 사용자에게 실제 전송된 assistant 답변 본문만 저장한다.
- `chat_logs`에는 Gemini JSON 원문, Drive/Gemini/source 내부 식별자, callback_url, 원본 파일명, 생년월일 등 개인정보를 저장하지 않는다.
- 기존 채팅 로그 export Scheduler job이 남아 있으면 배포 스크립트가 pause한다. 새 export Scheduler를 만들지 않는다.
- 관리자 대시보드 Basic Auth 비밀번호는 운영/test 배포에서 Secret Manager로 주입하고, `.env.example`이나 로그에 기록하지 않는다.
- 인증 실패 시 어떤 항목이 틀렸는지 구체적으로 알려주지 않는다.
- pre-warm, cleanup 실패는 사용자에게 노출하지 않는다.

## Implementation rules for Codex

- 먼저 관련 코드를 읽고, 변경 계획과 리스크를 요약한 뒤 수정한다.
- 기능 변경은 최소 diff로 수행한다.
- public API, Firestore path, JSON schema, callback flow를 임의로 바꾸지 않는다.
- 기존 service, schema, validation, test를 우선 재사용한다.
- 새 dependency를 추가하지 않는다. 꼭 필요하면 이유와 대안을 먼저 설명한다.
- 변경한 동작은 가능한 한 테스트를 추가하거나 기존 테스트를 갱신한다.
- 테스트를 실행할 수 없으면 실행하지 못한 이유와 수동 검증 방법을 남긴다.

## Reference docs

작업 주제에 따라 아래 문서를 먼저 읽는다.

- `docs/ARCHITECTURE.md`: 전체 구조, 역할 분리, LLM Wiki 원칙
- `docs/API_ENDPOINTS.md`: 엔드포인트 책임과 보호 정책
- `docs/DRIVE_SYNC.md`: Google Drive 구조, Changes API 동기화, 변경 파일 처리
- `docs/FIRESTORE_SCHEMA.md`: Firestore collection, schema, lock, lease
- `docs/GEMINI_FILES_RUNTIME.md`: Files API runtime cache, cleanup, pre-warm, prewarm jobs
- `docs/KAKAO_FLOW.md`: `/kakao/auth`, `/kakao/chat`, callback 흐름
- `docs/ROUTER_POLICY.md`: Router 입력, 출력, intent, source 선택 정책
- `docs/FINAL_QA_POLICY.md`: Final QA JSON, status, evidence, 출처 처리
- `docs/PRIVACY_SECURITY.md`: 개인정보, 안전 정책, 로그 정책, PDF parsing 정책
