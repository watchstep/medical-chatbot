# API Endpoints

## API 엔드포인트 구분

| 엔드포인트 | 역할 |
|---|---|
| `/kakao/auth` | 시작 인사, 인증 상태 확인, 이름/생년월일 인증, 인증 초기화 |
| `/kakao/chat` | 인증된 환자의 Gemini Files API 기반 의료 문서 질의응답 |
| `/admin/sync-drive-changes` | Cloud Scheduler 1분 주기로 Google Drive Changes API 변경분 동기화 |
| `/admin/sync-drive` | 초기 bootstrap, 하루 1회 정합성 검사, 장애 복구용 전체 스캔 |
| `/admin/sync-drive/patient/{patient_id}` | 특정 환자 폴더만 수동 동기화 |
| `/admin/rebuild-wiki-page/{patient_id}/{source_id}` | 특정 source의 medical_wiki_page 재생성 |
| `/admin/recompile-wiki-index/{patient_id}` | 특정 환자의 medical_wiki_index 재컴파일 |
| `/admin/cleanup-gemini-files` | Gemini Files API runtime cache 용량 사용량을 확인하고 필요 시 오래된 cache 삭제 |
| `/admin/prewarm-gemini-file` | Cloud Tasks가 호출하는 pre-warm worker endpoint. 사용자 요청 경로에서 직접 호출하지 않음 |
| `/admin/dashboard` | 관리자 대시보드 HTML. Basic Auth로 보호하며 운영 상태 확인과 수동 관리 작업을 제공 |
| `/admin/dashboard/status` | 대시보드 시스템 상태 조회 |
| `/admin/dashboard/chat-logs` | 최근 또는 날짜 범위 chat log를 질문/답변 pair로 조회. `mode`, `limit`, `page`, `start_at`, `end_at` query 지원 |
| `/admin/dashboard/callback-jobs` | callback job status count와 최근 실패 job 조회 |
| `/admin/dashboard/logs-link` | Cloud Logging 실패 로그 링크 생성 |
| `/admin/dashboard/actions/sync-drive-changes` | 대시보드에서 변경분 Drive sync를 수동 실행 |
| `/admin/dashboard/actions/sync-drive-full` | 대시보드에서 전체 Drive sync를 수동 실행 |
| `/admin/dashboard/actions/cleanup-gemini-files` | 대시보드에서 Gemini Files runtime cache cleanup을 수동 실행 |

중요:

- `/kakao/auth`는 인증 및 환자 식별만 담당한다.
- `/kakao/chat`은 반드시 인증된 사용자만 처리한다.
- `/kakao/chat`은 인증되지 않은 사용자에게 Google Drive 원본 파일, Firestore source, Gemini Files API 요청을 수행하지 않는다.
- `/admin/*` 엔드포인트는 카카오 사용자 요청으로 접근하지 못하게 보호한다.
- `/admin/*` 엔드포인트는 별도 관리자 인증 또는 Cloud Scheduler OIDC 인증을 사용한다.
- `/admin/prewarm-gemini-file`은 Cloud Tasks OIDC 인증으로만 호출되도록 운영한다.
- `/admin/cleanup-gemini-files`는 Cloud Scheduler 또는 관리자 수동 호출로만 실행한다.
- `/admin/dashboard/*`는 기존 Scheduler/Tasks용 `/admin/*` 인증과 분리된 Basic Auth를 사용한다. 비밀번호는 `ADMIN_DASHBOARD_PASSWORD` 환경변수로만 읽고, Cloud Run에서는 Secret Manager로 주입한다.
- `/admin/dashboard/chat-logs`는 Firestore `chat_logs`의 `user`/`assistant` row를 대시보드 표시용 질문/답변 pair로 묶어 조회하며 Google Drive export를 수행하지 않는다.
- `/admin/dashboard/chat-logs` 응답은 `items`, `mode`, `limit`, `page`, `has_prev`, `has_next`, `start_at`, `end_at`을 반환한다.
- `/admin/export-chat-logs`와 `/admin/dashboard/chat-log-export*` endpoint는 제공하지 않는다.
