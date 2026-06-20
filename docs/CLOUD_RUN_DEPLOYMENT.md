# Cloud Run 배포 Runbook

이 문서는 test 환경에서 Cloud Run 서버를 배포하는 절차만 정리합니다.

Firestore reset은 Cloud Run 재배포와 함께 Medical Wiki를 새로 만들기 위해 필요한 경우에만 포함합니다. Firestore database 자체는 삭제하지 않습니다. database 안의 collection/document 데이터만 삭제합니다.

## 1. 관련 스크립트

### `scripts/deploy-test.sh`

test 환경 배포용 wrapper script입니다.

역할:

```text
- test 환경 변수 설정
- .env 또는 shell env의 ADMIN_DASHBOARD_PASSWORD를 Secret Manager에 반영
- .env 또는 shell env의 UPLOAD_TOKEN_SECRET을 Secret Manager에 반영
- repository root로 이동
- root의 ./deploy-test.sh 실행
```

일반적인 test 서버 배포는 아래 명령을 사용합니다.

```bash
./scripts/deploy-test.sh
```

`.env` 또는 shell env에 `ADMIN_DASHBOARD_PASSWORD`가 있으면 `ADMIN_DASHBOARD_PASSWORD_SECRET_NAME` secret에 새 버전으로 업로드하고 대시보드를 활성화합니다.
`.env` 또는 shell env에 `UPLOAD_TOKEN_SECRET`이 있으면 `UPLOAD_TOKEN_SECRET_NAME` secret에 새 버전으로 업로드하고 `/upload/{token}` 링크 서명 검증에 사용합니다.

### `deploy-test.sh`

실제 Cloud Run 배포 script입니다.

역할:

```text
- 필요한 GCP API enable
- Firestore composite index 보장
- service account 생성 및 IAM 권한 부여
- Cloud Tasks queue 생성 및 rate/concurrency/retry 설정
- Cloud Run service 배포
- Cloud Run 환경변수 설정
- Gemini API key Secret Manager 연동
- 관리자 대시보드 비밀번호 Secret Manager 연동
- 업로드 token secret Secret Manager 연동
- Cloud Run URL 확인 후 OIDC audience 업데이트
- Cloud Run URL 확인 후 `UPLOAD_BASE_URL` 업데이트
- Cloud Scheduler job 생성 또는 업데이트
- legacy 채팅 로그 export Scheduler job이 남아 있으면 pause
```

기본 test 환경:

```text
PROJECT_ID=medical-chatbot-494315
REGION=asia-northeast3
SERVICE_NAME=medical-chatbot-test
FIRESTORE_DATABASE_ID=medical-chatbot-test
```

## 2. 공통 변수

```bash
PROJECT_ID="medical-chatbot-494315"
REGION="asia-northeast3"
SERVICE_NAME="medical-chatbot-test"
FIRESTORE_DATABASE_ID="medical-chatbot-test"

DRIVE_CHANGES_JOB_NAME="medical-chatbot-test-sync-drive-changes"
FULL_SYNC_JOB_NAME="medical-chatbot-test-sync-drive-full"
GEMINI_FILES_CLEANUP_JOB_NAME="medical-chatbot-test-cleanup-gemini-files"
LEGACY_CHAT_LOG_EXPORT_JOB_NAME="medical-chatbot-test-export-chat-logs"

CALLBACK_TASKS_QUEUE_NAME="medical-chatbot-test-callback-jobs"
PREWARM_TASKS_QUEUE_NAME="medical-chatbot-test-prewarm"
WIKI_REBUILD_TASKS_QUEUE_NAME="medical-chatbot-test-wiki-rebuild"

SCHEDULER_SA="medical-chatbot-test-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULER_TIME_ZONE="Asia/Seoul"
ADMIN_DASHBOARD_PASSWORD_SECRET_NAME="admin-dashboard-password"
GEMINI_SECRET_NAME="gemini-api-key"
UPLOAD_TOKEN_SECRET_NAME="upload-token-secret-test"

gcloud config set project "${PROJECT_ID}"
```

Cloud Run URL은 배포 후 아래처럼 가져옵니다.

```bash
SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format='value(status.url)')"

echo "${SERVICE_URL}"
```

## 3. 평상시 코드 수정 후 서버만 배포

아래 경우에는 Firestore reset 없이 Cloud Run만 새로 배포합니다.

```text
- 일반 코드 수정
- prompt 문구 수정
- validation/rendering 수정
- bug fix
- 환경변수 변경
- Medical Wiki 재생성이 필요 없는 수정
```

### 3.1 배포

관리자 대시보드를 test 서버에서 사용하려면 `.env`에 아래 값을 둡니다. 비밀번호 값은 Secret Manager로 업로드되고 배포 로그에는 출력하지 않습니다.

```env
ADMIN_DASHBOARD_USERNAME=admin
ADMIN_DASHBOARD_PASSWORD=replace-with-password
ADMIN_DASHBOARD_PASSWORD_SECRET_NAME=admin-dashboard-password
```

파일 업로드 링크를 사용하려면 `.env`에 아래 값을 둡니다. `UPLOAD_TOKEN_SECRET`은 실제 운영값으로 교체해야 하며, test wrapper가 Secret Manager에 업로드합니다.

```env
# Example only. Replace with a long random value before deploy.
UPLOAD_TOKEN_SECRET=replace-with-long-random-secret
UPLOAD_TOKEN_SECRET_NAME=upload-token-secret-test
UPLOAD_TOKEN_TTL_MINUTES=15
CHAT_ATTACHMENT_TTL_MINUTES=60
MAX_UPLOAD_FILE_BYTES=20971520
```

카카오 즉시 응답 경로가 Drive sync, Wiki rebuild, prewarm 작업과 같은 Cloud Run service를 공유하므로 운영 배포 기본값은 아래처럼 사용자 요청 지연을 줄이는 쪽으로 잡습니다. 필요하면 `.env` 또는 shell env로 override할 수 있습니다.

```env
CLOUD_RUN_CPU=2
CLOUD_RUN_MEMORY=2Gi
CLOUD_RUN_MIN_INSTANCES=2
CLOUD_RUN_MAX_INSTANCES=12
CLOUD_RUN_CONCURRENCY=20
WIKI_REBUILD_TASKS_MAX_DISPATCHES_PER_SECOND=0.2
WIKI_REBUILD_TASKS_MAX_CONCURRENT_DISPATCHES=1
```

```bash
./scripts/deploy-test.sh
```

Cloud Scheduler job은 `SCHEDULER_TIME_ZONE` 기준으로 생성 또는 갱신됩니다. 기본값은 `Asia/Seoul`이며, full sync는 `0 3 * * *` 스케줄이므로 기본 설정에서는 매일 한국 시간 03:00에 `/admin/sync-drive`가 실행됩니다.

운영 `deploy.sh`에서 관리자 대시보드를 활성화하려면 `ADMIN_DASHBOARD_ENABLED=true`와 함께 `ADMIN_DASHBOARD_PASSWORD_SECRET_NAME`에 해당하는 Secret Manager secret이 미리 존재해야 합니다. 운영 배포 스크립트는 비밀번호 값을 `.env`에서 읽어 secret을 생성하지 않습니다.

`LEGACY_CHAT_LOG_EXPORT_JOB_NAME`은 이전 채팅 로그 Drive export Scheduler가 남아 있는 환경에서 pause할 대상 이름입니다. 현재 코드는 Drive CSV export endpoint와 worker를 제공하지 않으므로, 이 변수로 새 export job을 만들지 않습니다.

정식 운영 배포에서 새 Firestore database이거나 `patients`, `drive_file_index`, `medical_sources`가 비어 있는 상태라면 변경분 sync만으로 기존 Drive 파일 전체가 생성되지 않습니다. 이 경우 배포 직후 초기 전체 연동까지 실행합니다.

```bash
RUN_FULL_SYNC_AFTER_DEPLOY=true ./deploy.sh
```

평상시 코드 수정 배포에서는 기존처럼 실행합니다.

```bash
./deploy.sh
```

`deploy.sh`의 기본 Scheduler 설정:

```text
SCHEDULER_TIME_ZONE=Asia/Seoul
DRIVE_CHANGES_JOB_NAME: * * * * * → /admin/sync-drive-changes
FULL_SYNC_JOB_NAME: 0 3 * * * → /admin/sync-drive
GEMINI_FILES_CLEANUP_JOB_NAME: */30 * * * * → /admin/cleanup-gemini-files
```

### 3.2 Health check

```bash
SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format='value(status.url)')"

curl "${SERVICE_URL}/health"
```

### 3.3 admin endpoint 수동 호출이 필요할 때

`ADMIN_AUTH_MODE=oidc` 기준으로 scheduler service account를 impersonate해서 호출합니다.

```bash
TOKEN="$(gcloud auth print-identity-token \
  --impersonate-service-account="${SCHEDULER_SA}" \
  --audiences="${SERVICE_URL}" \
  --include-email)"

curl -X POST "${SERVICE_URL}/admin/sync-drive-changes" \
  -H "Authorization: Bearer ${TOKEN}"
```

`--include-email`은 앱 내부에서 `ADMIN_OIDC_ALLOWED_EMAILS`를 검증하므로 유지합니다.

### 3.4 관리자 대시보드

배포 후 아래 URL로 접속합니다.

```text
${SERVICE_URL}/admin/dashboard
```

브라우저 기본 로그인창에는 `ADMIN_DASHBOARD_USERNAME`과 Secret Manager에서 주입된 `ADMIN_DASHBOARD_PASSWORD`를 사용합니다. 대시보드에서는 Firestore/Drive 상태, 최근 또는 날짜 범위 chat log, callback job 실패/집계, Drive sync/full sync/Gemini files cleanup 실행 버튼, Cloud Logging 실패 로그 링크를 제공합니다.

## 4. Firestore 데이터 삭제 후 Medical Wiki 재생성 배포

아래 경우에만 사용합니다.

```text
- medical_wiki_pages schema 변경
- Medical Wiki 생성 prompt 변경
- category/display_topics/frontmatter 구조 변경
- Router catalog 구조 변경
- Final QA 출력 계약 변경
- used_source_ids/evidence_refs 구조 변경
- 기존 Firestore 데이터를 새 코드 기준으로 다시 만들고 싶은 경우
```

중요:

```text
Firestore database를 삭제하지 않습니다.
medical-chatbot-test database는 유지합니다.
database 안의 collection/document 데이터만 삭제합니다.
```

핵심 순서:

```text
Scheduler pause
→ Tasks pause/purge
→ 기존 Gemini Files cleanup
→ 새 코드 배포
→ Scheduler 다시 pause
→ Firestore 데이터 삭제
→ Tasks resume
→ /admin/sync-drive 실행
→ wiki rebuild task 처리 확인
→ Scheduler resume
```

### 4.1 Scheduler pause

```bash
gcloud scheduler jobs pause "${DRIVE_CHANGES_JOB_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" || true

gcloud scheduler jobs pause "${FULL_SYNC_JOB_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" || true

gcloud scheduler jobs pause "${GEMINI_FILES_CLEANUP_JOB_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" || true
```

### 4.2 Cloud Tasks queue pause/purge

```bash
gcloud tasks queues pause "${CALLBACK_TASKS_QUEUE_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" || true

gcloud tasks queues pause "${PREWARM_TASKS_QUEUE_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" || true

gcloud tasks queues pause "${WIKI_REBUILD_TASKS_QUEUE_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" || true
```

test 환경에서 기존 task를 버려도 되면 purge합니다.

```bash
gcloud tasks queues purge "${CALLBACK_TASKS_QUEUE_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --quiet || true

gcloud tasks queues purge "${PREWARM_TASKS_QUEUE_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --quiet || true

gcloud tasks queues purge "${WIKI_REBUILD_TASKS_QUEUE_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --quiet || true
```

### 4.3 기존 Gemini Files cleanup

Firestore runtime 참조가 남아 있을 때 먼저 실행합니다.

```bash
SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format='value(status.url)')"

TOKEN="$(gcloud auth print-identity-token \
  --impersonate-service-account="${SCHEDULER_SA}" \
  --audiences="${SERVICE_URL}" \
  --include-email)"

curl -X POST "${SERVICE_URL}/admin/cleanup-gemini-files" \
  -H "Authorization: Bearer ${TOKEN}"
```

### 4.4 새 코드 배포

Firestore 데이터를 삭제하기 전에 새 코드를 먼저 배포합니다.

```bash
./scripts/deploy-test.sh
```

배포 후 URL과 health를 다시 확인합니다.

```bash
SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format='value(status.url)')"

curl "${SERVICE_URL}/health"
```

### 4.5 배포 후 Scheduler 다시 pause

배포 script가 Scheduler job을 생성 또는 업데이트할 수 있으므로 다시 멈춥니다.

```bash
gcloud scheduler jobs pause "${DRIVE_CHANGES_JOB_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" || true

gcloud scheduler jobs pause "${FULL_SYNC_JOB_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" || true

gcloud scheduler jobs pause "${GEMINI_FILES_CLEANUP_JOB_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" || true
```

### 4.6 Firestore 데이터만 삭제

Firebase CLI로 database 안의 collection/document 데이터를 삭제합니다. database 자체를 삭제하지 않습니다.

```bash
firebase firestore:delete \
  --all-collections \
  --project "${PROJECT_ID}" \
  --database "${FIRESTORE_DATABASE_ID}" \
  --force
```

주의:

```text
아래 명령은 사용하지 않습니다.

gcloud firestore databases delete ...
```

### 4.7 Cloud Tasks queue resume

`/admin/sync-drive` 실행 전에 반드시 wiki rebuild queue가 RUNNING 상태여야 합니다.

```bash
gcloud tasks queues resume "${CALLBACK_TASKS_QUEUE_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" || true

gcloud tasks queues resume "${PREWARM_TASKS_QUEUE_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" || true

gcloud tasks queues resume "${WIKI_REBUILD_TASKS_QUEUE_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" || true
```

상태 확인:

```bash
gcloud tasks queues describe "${WIKI_REBUILD_TASKS_QUEUE_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --format="value(state)"
```

반드시 `RUNNING`이어야 합니다.

### 4.8 full sync 실행

```bash
TOKEN="$(gcloud auth print-identity-token \
  --impersonate-service-account="${SCHEDULER_SA}" \
  --audiences="${SERVICE_URL}" \
  --include-email)"

curl -X POST "${SERVICE_URL}/admin/sync-drive" \
  -H "Authorization: Bearer ${TOKEN}"
```

### 4.9 wiki rebuild task 확인

```bash
gcloud tasks list \
  --queue="${WIKI_REBUILD_TASKS_QUEUE_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --limit=20
```

정상 상태:

```text
- task가 목록에서 사라짐
- 또는 DISPATCH_ATTEMPTS가 1 이상으로 증가
```

`DISPATCH_ATTEMPTS=0` 상태로 계속 남아 있으면 queue 상태와 rate limit을 확인합니다.

```bash
gcloud tasks queues describe "${WIKI_REBUILD_TASKS_QUEUE_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --format="yaml(state,rateLimits,retryConfig)"
```

### 4.10 Firestore 생성 결과 확인

Firestore Console에서 아래 데이터가 생성되었는지 확인합니다.

```text
patients/{patient_id}
patients/{patient_id}/medical_sources
patients/{patient_id}/medical_wiki_pages
patients/{patient_id}/medical_wiki_index/main
patients/{patient_id}/medical_source_runtime
drive_file_index
drive_folder_index
```

### 4.11 Scheduler resume

확인 후 Scheduler를 다시 켭니다.

```bash
gcloud scheduler jobs resume "${DRIVE_CHANGES_JOB_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" || true

gcloud scheduler jobs resume "${FULL_SYNC_JOB_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" || true

gcloud scheduler jobs resume "${GEMINI_FILES_CLEANUP_JOB_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" || true
```

## 5. 최소 체크리스트

### 일반 배포

```text
[ ] ./scripts/deploy-test.sh 실행
[ ] /health 확인
```

### Firestore 데이터 삭제 포함 배포

```text
[ ] Scheduler pause
[ ] Tasks pause/purge
[ ] cleanup-gemini-files 실행
[ ] ./scripts/deploy-test.sh 실행
[ ] Scheduler 다시 pause
[ ] firebase firestore:delete --all-collections 실행
[ ] Tasks resume
[ ] wiki rebuild queue RUNNING 확인
[ ] /admin/sync-drive 실행
[ ] medical_wiki_pages 생성 확인
[ ] Scheduler resume
```
