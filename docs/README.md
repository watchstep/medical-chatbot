# Documentation Index

이 폴더는 기존의 긴 `AGENTS.md` 내용을 기능별 상세 문서로 분리한 것이다.

## Files

- `ARCHITECTURE.md`: 전체 구조, 주요 기술, LLM Wiki 적용 원칙, 역할 분리
- `API_ENDPOINTS.md`: Kakao 및 admin endpoint 책임과 접근 제어
- `CLOUD_RUN_DEPLOYMENT.md`: test Cloud Run 배포, Secret Manager, 관리자 대시보드, legacy chat log export Scheduler 정리 절차
- `DRIVE_SYNC.md`: Google Drive 폴더 구조, Changes API 동기화, 신규/수정/삭제 처리
- `FIRESTORE_SCHEMA.md`: Firestore collection 구조, 문서 schema, ID 정책, lock/lease 정책
- `GEMINI_FILES_RUNTIME.md`: Gemini Files API runtime cache, cleanup, pre-warm, prewarm job
- `KAKAO_FLOW.md`: 인증, callback, `/kakao/chat` 요청 및 callback job 처리 흐름
- `ROUTER_POLICY.md`: Gemini Router의 역할, 입력, 출력, intent, selection policy
- `FINAL_QA_POLICY.md`: Final QA 호출 원칙, status 처리, evidence 및 출처 검증
- `PRIVACY_SECURITY.md`: 개인정보 보호, 응답 형식, 실패 기록, PDF parsing, 최종 개발 원칙

## How to use with Codex

1. Codex는 먼저 루트 `AGENTS.md`를 읽는다.
2. 수정하려는 기능과 관련된 문서만 추가로 읽는다.
3. 상세 문서와 코드가 충돌하면, 구현 코드와 테스트를 확인한 뒤 변경 계획을 제시한다.
4. 아키텍처, Firestore schema, Router/Final QA 계약을 바꿀 때는 관련 문서를 함께 갱신한다.
