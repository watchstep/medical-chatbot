#!/bin/bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-medical-chatbot-494315}"
REGION="${REGION:-asia-northeast3}"
SERVICE_NAME="${SERVICE_NAME:-medical-chatbot}"
FIRESTORE_DATABASE_ID="${FIRESTORE_DATABASE_ID:-medical-chatbot}"
DEPLOY_FIRESTORE_INDEXES="${DEPLOY_FIRESTORE_INDEXES:-false}"
ENSURE_FIRESTORE_INDEXES="${ENSURE_FIRESTORE_INDEXES:-true}"
RUN_SA="${RUN_SA:-medical-chatbot-run@${PROJECT_ID}.iam.gserviceaccount.com}"
SCHEDULER_SA="${SCHEDULER_SA:-medical-chatbot-scheduler@${PROJECT_ID}.iam.gserviceaccount.com}"
DRIVE_CHANGES_JOB_NAME="${DRIVE_CHANGES_JOB_NAME:-medical-chatbot-sync-drive-changes}"
FULL_SYNC_JOB_NAME="${FULL_SYNC_JOB_NAME:-medical-chatbot-sync-drive-full}"
CALLBACK_JOBS_JOB_NAME="${CALLBACK_JOBS_JOB_NAME:-medical-chatbot-process-callback-jobs}"
GEMINI_FILES_CLEANUP_JOB_NAME="${GEMINI_FILES_CLEANUP_JOB_NAME:-medical-chatbot-cleanup-gemini-files}"
LEGACY_CHAT_LOG_EXPORT_JOB_NAME="${LEGACY_CHAT_LOG_EXPORT_JOB_NAME:-medical-chatbot-export-chat-logs}"
CALLBACK_TASKS_QUEUE_NAME="${CALLBACK_TASKS_QUEUE_NAME:-medical-chatbot-callback-jobs}"
PREWARM_TASKS_QUEUE_NAME="${PREWARM_TASKS_QUEUE_NAME:-medical-chatbot-prewarm}"
WIKI_REBUILD_TASKS_QUEUE_NAME="${WIKI_REBUILD_TASKS_QUEUE_NAME:-medical-chatbot-wiki-rebuild}"
CLOUD_TASKS_LOCATION="${CLOUD_TASKS_LOCATION:-${REGION}}"
CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL="${CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL:-${SCHEDULER_SA}}"
CALLBACK_TASKS_DISPATCH_DEADLINE_SECONDS="${CALLBACK_TASKS_DISPATCH_DEADLINE_SECONDS:-60}"
GEMINI_FILE_PREWARM_DISPATCH_DEADLINE_SECONDS="${GEMINI_FILE_PREWARM_DISPATCH_DEADLINE_SECONDS:-600}"
WIKI_REBUILD_TASKS_DISPATCH_DEADLINE_SECONDS="${WIKI_REBUILD_TASKS_DISPATCH_DEADLINE_SECONDS:-600}"
WIKI_REBUILD_WORKER_MODE="${WIKI_REBUILD_WORKER_MODE:-cloud_tasks}"
CLOUD_RUN_TIMEOUT_SECONDS="${CLOUD_RUN_TIMEOUT_SECONDS:-900}"

CALLBACK_TASKS_MAX_DISPATCHES_PER_SECOND="${CALLBACK_TASKS_MAX_DISPATCHES_PER_SECOND:-2}"
CALLBACK_TASKS_MAX_CONCURRENT_DISPATCHES="${CALLBACK_TASKS_MAX_CONCURRENT_DISPATCHES:-5}"
CALLBACK_TASKS_MAX_ATTEMPTS="${CALLBACK_TASKS_MAX_ATTEMPTS:-5}"
CALLBACK_TASKS_MIN_BACKOFF="${CALLBACK_TASKS_MIN_BACKOFF:-5s}"
CALLBACK_TASKS_MAX_BACKOFF="${CALLBACK_TASKS_MAX_BACKOFF:-10s}"
CALLBACK_TASKS_MAX_DOUBLINGS="${CALLBACK_TASKS_MAX_DOUBLINGS:-1}"
CALLBACK_TASKS_MAX_RETRY_DURATION="${CALLBACK_TASKS_MAX_RETRY_DURATION:-60s}"

PREWARM_TASKS_MAX_DISPATCHES_PER_SECOND="${PREWARM_TASKS_MAX_DISPATCHES_PER_SECOND:-0.2}"
PREWARM_TASKS_MAX_CONCURRENT_DISPATCHES="${PREWARM_TASKS_MAX_CONCURRENT_DISPATCHES:-1}"
PREWARM_TASKS_MAX_ATTEMPTS="${PREWARM_TASKS_MAX_ATTEMPTS:-3}"
PREWARM_TASKS_MIN_BACKOFF="${PREWARM_TASKS_MIN_BACKOFF:-60s}"
PREWARM_TASKS_MAX_BACKOFF="${PREWARM_TASKS_MAX_BACKOFF:-300s}"
PREWARM_TASKS_MAX_DOUBLINGS="${PREWARM_TASKS_MAX_DOUBLINGS:-2}"
PREWARM_TASKS_MAX_RETRY_DURATION="${PREWARM_TASKS_MAX_RETRY_DURATION:-1200s}"

WIKI_REBUILD_TASKS_MAX_DISPATCHES_PER_SECOND="${WIKI_REBUILD_TASKS_MAX_DISPATCHES_PER_SECOND:-0.5}"
WIKI_REBUILD_TASKS_MAX_CONCURRENT_DISPATCHES="${WIKI_REBUILD_TASKS_MAX_CONCURRENT_DISPATCHES:-2}"
WIKI_REBUILD_TASKS_MAX_ATTEMPTS="${WIKI_REBUILD_TASKS_MAX_ATTEMPTS:-4}"
WIKI_REBUILD_TASKS_MIN_BACKOFF="${WIKI_REBUILD_TASKS_MIN_BACKOFF:-60s}"
WIKI_REBUILD_TASKS_MAX_BACKOFF="${WIKI_REBUILD_TASKS_MAX_BACKOFF:-300s}"
WIKI_REBUILD_TASKS_MAX_DOUBLINGS="${WIKI_REBUILD_TASKS_MAX_DOUBLINGS:-2}"
WIKI_REBUILD_TASKS_MAX_RETRY_DURATION="${WIKI_REBUILD_TASKS_MAX_RETRY_DURATION:-1800s}"
GEMINI_SECRET_NAME="${GEMINI_SECRET_NAME:-gemini-api-key}"
UPLOAD_TOKEN_SECRET_NAME="${UPLOAD_TOKEN_SECRET_NAME:-upload-token-secret}"
ADMIN_DASHBOARD_ENABLED="${ADMIN_DASHBOARD_ENABLED:-false}"
ADMIN_DASHBOARD_USERNAME="${ADMIN_DASHBOARD_USERNAME:-admin}"
ADMIN_DASHBOARD_PASSWORD_SECRET_NAME="${ADMIN_DASHBOARD_PASSWORD_SECRET_NAME:-admin-dashboard-password}"

gcloud config set project "$PROJECT_ID"

gcloud services enable cloudtasks.googleapis.com run.googleapis.com firestore.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com --project "$PROJECT_ID" >/dev/null


ensure_firestore_composite_indexes() {
  echo "Ensuring Firestore composite indexes for ${FIRESTORE_DATABASE_ID}..."

  # Required by /admin/process-callback-jobs fallback polling:
  # where(runnable == true) + where(next_run_at <= now) + order_by(next_run_at).
  gcloud firestore indexes composite create \
    --project "${PROJECT_ID}" \
    --database "${FIRESTORE_DATABASE_ID}" \
    --collection-group "kakao_callback_jobs" \
    --query-scope "COLLECTION" \
    --field-config "field-path=runnable,order=ascending" \
    --field-config "field-path=next_run_at,order=ascending" \
    --async >/dev/null 2>&1 || true

  # Useful for operational cleanup or expiration scans by status and expires_at.
  gcloud firestore indexes composite create \
    --project "${PROJECT_ID}" \
    --database "${FIRESTORE_DATABASE_ID}" \
    --collection-group "kakao_callback_jobs" \
    --query-scope "COLLECTION" \
    --field-config "field-path=status,order=ascending" \
    --field-config "field-path=expires_at,order=ascending" \
    --async >/dev/null 2>&1 || true

  # Required by temporary upload attachment cleanup:
  # collection_group(active_attachments) where(status == ACTIVE)
  # + where(expires_at <= now) + order_by(expires_at).
  gcloud firestore indexes composite create \
    --project "${PROJECT_ID}" \
    --database "${FIRESTORE_DATABASE_ID}" \
    --collection-group "active_attachments" \
    --query-scope "COLLECTION_GROUP" \
    --field-config "field-path=status,order=ascending" \
    --field-config "field-path=expires_at,order=ascending" \
    --async >/dev/null 2>&1 || true
}

ensure_service_account() {
  local email="$1"
  local name="${email%@*}"
  local display_name="$2"
  if ! gcloud iam service-accounts describe "$email" --project "$PROJECT_ID" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$name" \
      --project "$PROJECT_ID" \
      --display-name="$display_name"
  fi
}

configure_tasks_queue() {
  local queue_name="$1"
  local rate="$2"
  local concurrency="$3"
  local attempts="$4"
  local min_backoff="$5"
  local max_backoff="$6"
  local doublings="$7"
  local retry_duration="$8"

  gcloud tasks queues update "$queue_name" \
    --project "$PROJECT_ID" \
    --location "$CLOUD_TASKS_LOCATION" \
    --max-dispatches-per-second="$rate" \
    --max-concurrent-dispatches="$concurrency" \
    --max-attempts="$attempts" \
    --min-backoff="$min_backoff" \
    --max-backoff="$max_backoff" \
    --max-doublings="$doublings" \
    --max-retry-duration="$retry_duration" >/dev/null
}

grant_secret_access() {
  local secret_name="$1"
  if [[ -z "$secret_name" ]]; then
    return
  fi
  gcloud secrets add-iam-policy-binding "$secret_name" \
    --project "$PROJECT_ID" \
    --member="serviceAccount:${RUN_SA}" \
    --role="roles/secretmanager.secretAccessor" >/dev/null
}

require_secret_exists() {
  local secret_name="$1"
  if ! gcloud secrets describe "$secret_name" --project "$PROJECT_ID" >/dev/null 2>&1; then
    echo "Required Secret Manager secret not found: ${secret_name}" >&2
    echo "Create it before deploying with ADMIN_DASHBOARD_ENABLED=true." >&2
    exit 1
  fi
}

pause_scheduler_job_if_exists() {
  local job_name="$1"
  if gcloud scheduler jobs describe "$job_name" --project "$PROJECT_ID" --location "$REGION" >/dev/null 2>&1; then
    gcloud scheduler jobs pause "$job_name" \
      --project "$PROJECT_ID" \
      --location "$REGION" >/dev/null || true
    echo "Paused legacy scheduler job: ${job_name}"
  fi
}

if [[ "${ENSURE_FIRESTORE_INDEXES}" == "true" ]]; then
  ensure_firestore_composite_indexes
fi

ensure_service_account "$RUN_SA" "Medical Chatbot Cloud Run Runtime"
ensure_service_account "$SCHEDULER_SA" "Medical Chatbot Cloud Scheduler Invoker"
if [[ "$CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL" != "$SCHEDULER_SA" ]]; then
  ensure_service_account "$CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL" "Medical Chatbot Cloud Tasks Invoker"
fi

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUN_SA}" \
  --role="roles/cloudtasks.enqueuer" >/dev/null

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUN_SA}" \
  --role="roles/datastore.user" >/dev/null

grant_secret_access "$GEMINI_SECRET_NAME"
require_secret_exists "$UPLOAD_TOKEN_SECRET_NAME"
grant_secret_access "$UPLOAD_TOKEN_SECRET_NAME"
if [[ "$ADMIN_DASHBOARD_ENABLED" == "true" ]]; then
  require_secret_exists "$ADMIN_DASHBOARD_PASSWORD_SECRET_NAME"
  grant_secret_access "$ADMIN_DASHBOARD_PASSWORD_SECRET_NAME"
fi

# Required when Cloud Tasks uses CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL for OIDC.
# Without this, create_task fails with iam.serviceAccounts.actAs PERMISSION_DENIED.
gcloud iam service-accounts add-iam-policy-binding "$CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL" \
  --project "$PROJECT_ID" \
  --member="serviceAccount:${RUN_SA}" \
  --role="roles/iam.serviceAccountUser" >/dev/null

if ! gcloud tasks queues describe "$CALLBACK_TASKS_QUEUE_NAME" --location "$CLOUD_TASKS_LOCATION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud tasks queues create "$CALLBACK_TASKS_QUEUE_NAME" --location "$CLOUD_TASKS_LOCATION" --project "$PROJECT_ID" >/dev/null
fi

if ! gcloud tasks queues describe "$PREWARM_TASKS_QUEUE_NAME" --location "$CLOUD_TASKS_LOCATION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud tasks queues create "$PREWARM_TASKS_QUEUE_NAME" --location "$CLOUD_TASKS_LOCATION" --project "$PROJECT_ID" >/dev/null
fi

if ! gcloud tasks queues describe "$WIKI_REBUILD_TASKS_QUEUE_NAME" --location "$CLOUD_TASKS_LOCATION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud tasks queues create "$WIKI_REBUILD_TASKS_QUEUE_NAME" --location "$CLOUD_TASKS_LOCATION" --project "$PROJECT_ID" >/dev/null
fi

configure_tasks_queue "$CALLBACK_TASKS_QUEUE_NAME" \
  "$CALLBACK_TASKS_MAX_DISPATCHES_PER_SECOND" \
  "$CALLBACK_TASKS_MAX_CONCURRENT_DISPATCHES" \
  "$CALLBACK_TASKS_MAX_ATTEMPTS" \
  "$CALLBACK_TASKS_MIN_BACKOFF" \
  "$CALLBACK_TASKS_MAX_BACKOFF" \
  "$CALLBACK_TASKS_MAX_DOUBLINGS" \
  "$CALLBACK_TASKS_MAX_RETRY_DURATION"

configure_tasks_queue "$PREWARM_TASKS_QUEUE_NAME" \
  "$PREWARM_TASKS_MAX_DISPATCHES_PER_SECOND" \
  "$PREWARM_TASKS_MAX_CONCURRENT_DISPATCHES" \
  "$PREWARM_TASKS_MAX_ATTEMPTS" \
  "$PREWARM_TASKS_MIN_BACKOFF" \
  "$PREWARM_TASKS_MAX_BACKOFF" \
  "$PREWARM_TASKS_MAX_DOUBLINGS" \
  "$PREWARM_TASKS_MAX_RETRY_DURATION"

configure_tasks_queue "$WIKI_REBUILD_TASKS_QUEUE_NAME" \
  "$WIKI_REBUILD_TASKS_MAX_DISPATCHES_PER_SECOND" \
  "$WIKI_REBUILD_TASKS_MAX_CONCURRENT_DISPATCHES" \
  "$WIKI_REBUILD_TASKS_MAX_ATTEMPTS" \
  "$WIKI_REBUILD_TASKS_MIN_BACKOFF" \
  "$WIKI_REBUILD_TASKS_MAX_BACKOFF" \
  "$WIKI_REBUILD_TASKS_MAX_DOUBLINGS" \
  "$WIKI_REBUILD_TASKS_MAX_RETRY_DURATION"

ALLOWED_EMAILS="$SCHEDULER_SA"
if [[ "$CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL" != "$SCHEDULER_SA" ]]; then
  ALLOWED_EMAILS="${ALLOWED_EMAILS},${CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL}"
fi

ENV_VARS="FIRESTORE_PROJECT_ID=${PROJECT_ID}"
ENV_VARS+=",FIRESTORE_DATABASE_ID=${FIRESTORE_DATABASE_ID}"
ENV_VARS+=",DRIVE_CHANGES_SCOPE_ID=main"
if [[ -n "${GOOGLE_DRIVE_ROOT_FOLDER_ID:-}" ]]; then
  ENV_VARS+=",GOOGLE_DRIVE_ROOT_FOLDER_ID=${GOOGLE_DRIVE_ROOT_FOLDER_ID}"
fi
ENV_VARS+=",GOOGLE_DRIVE_ROOT_FOLDER_NAME=${GOOGLE_DRIVE_ROOT_FOLDER_NAME:-medical-chatbot}"
ENV_VARS+=",PATIENTS_FOLDER_NAME=${PATIENTS_FOLDER_NAME:-patients}"
ENV_VARS+=",SYSTEM_FOLDER_NAME=${SYSTEM_FOLDER_NAME:-_system}"
ENV_VARS+=",DRIVE_PATIENT_BOOTSTRAP_ON_FULL_SYNC=true"
ENV_VARS+=",MAX_CHANGES_PER_RUN=20"
ENV_VARS+=",MAX_CHANGES_PAGES_PER_RUN=5"
ENV_VARS+=",MAX_WIKI_PAGE_GENERATIONS_PER_RUN=3"
ENV_VARS+=",MAX_WIKI_BACKLOG_PER_RUN=10"
ENV_VARS+=",CHANGES_SYNC_LOCK_LEASE_MINUTES=2"
ENV_VARS+=",FULL_SYNC_LOCK_LEASE_MINUTES=10"
ENV_VARS+=",FOLDER_INDEX_BOOTSTRAP_ON_CHANGES=true"
ENV_VARS+=",FILE_READY_WAIT_SECONDS=30"
ENV_VARS+=",FILE_UPLOAD_LOCK_LEASE_SECONDS=${FILE_UPLOAD_LOCK_LEASE_SECONDS:-600}"
ENV_VARS+=",UPLOAD_TOKEN_TTL_MINUTES=${UPLOAD_TOKEN_TTL_MINUTES:-15}"
ENV_VARS+=",CHAT_ATTACHMENT_TTL_MINUTES=${CHAT_ATTACHMENT_TTL_MINUTES:-60}"
ENV_VARS+=",MAX_UPLOAD_FILE_BYTES=${MAX_UPLOAD_FILE_BYTES:-20971520}"
ENV_VARS+=",UPLOAD_BASE_URL="
ENV_VARS+=",GEMINI_FILES_STORAGE_SOFT_LIMIT_BYTES=${GEMINI_FILES_STORAGE_SOFT_LIMIT_BYTES:-15032385536}"
ENV_VARS+=",GEMINI_FILES_STORAGE_TARGET_BYTES=${GEMINI_FILES_STORAGE_TARGET_BYTES:-12884901888}"
ENV_VARS+=",GEMINI_FILES_STORAGE_HARD_LIMIT_BYTES=${GEMINI_FILES_STORAGE_HARD_LIMIT_BYTES:-18253611008}"
ENV_VARS+=",GEMINI_FILES_CLEANUP_BATCH_SIZE=${GEMINI_FILES_CLEANUP_BATCH_SIZE:-20}"
ENV_VARS+=",GEMINI_FILE_PREWARM_ENABLED=${GEMINI_FILE_PREWARM_ENABLED:-true}"
ENV_VARS+=",GEMINI_FILE_PREWARM_MAX_ATTEMPTS=${GEMINI_FILE_PREWARM_MAX_ATTEMPTS:-3}"
ENV_VARS+=",GEMINI_FILE_PREWARM_LEASE_SECONDS=${GEMINI_FILE_PREWARM_LEASE_SECONDS:-570}"
ENV_VARS+=",GEMINI_FILE_PREWARM_IDEMPOTENCY_BUCKET_MINUTES=${GEMINI_FILE_PREWARM_IDEMPOTENCY_BUCKET_MINUTES:-60}"
ENV_VARS+=",GEMINI_FILE_PREWARM_EXPIRY_MINUTES=${GEMINI_FILE_PREWARM_EXPIRY_MINUTES:-30}"
ENV_VARS+=",GEMINI_FILE_PREWARM_DISPATCH_DEADLINE_SECONDS=${GEMINI_FILE_PREWARM_DISPATCH_DEADLINE_SECONDS}"
ENV_VARS+=",ASYNC_WORKER_PROCESSING_TIMEOUT_SECONDS=${ASYNC_WORKER_PROCESSING_TIMEOUT_SECONDS:-540}"
ENV_VARS+=",SESSION_TTL_MINUTES=1440"
ENV_VARS+=",CALLBACK_WORKER_MODE=${CALLBACK_WORKER_MODE:-cloud_tasks}"
ENV_VARS+=",CLOUD_TASKS_PROJECT_ID=${PROJECT_ID}"
ENV_VARS+=",CLOUD_TASKS_LOCATION=${CLOUD_TASKS_LOCATION}"
ENV_VARS+=",CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL=${CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL}"
ENV_VARS+=",CLOUD_TASKS_BASE_URL="
ENV_VARS+=",CLOUD_TASKS_AUDIENCE="
ENV_VARS+=",CALLBACK_TASKS_QUEUE_NAME=${CALLBACK_TASKS_QUEUE_NAME}"
ENV_VARS+=",CALLBACK_TASKS_DISPATCH_DEADLINE_SECONDS=${CALLBACK_TASKS_DISPATCH_DEADLINE_SECONDS}"
ENV_VARS+=",PREWARM_TASKS_QUEUE_NAME=${PREWARM_TASKS_QUEUE_NAME}"
ENV_VARS+=",WIKI_REBUILD_WORKER_MODE=${WIKI_REBUILD_WORKER_MODE}"
ENV_VARS+=",WIKI_REBUILD_TASKS_QUEUE_NAME=${WIKI_REBUILD_TASKS_QUEUE_NAME}"
ENV_VARS+=",WIKI_REBUILD_TASKS_DISPATCH_DEADLINE_SECONDS=${WIKI_REBUILD_TASKS_DISPATCH_DEADLINE_SECONDS}"
ENV_VARS+=",CALLBACK_JOB_LEASE_SECONDS=${CALLBACK_JOB_LEASE_SECONDS:-55}"
ENV_VARS+=",CALLBACK_JOB_MAX_ATTEMPTS=${CALLBACK_JOB_MAX_ATTEMPTS:-5}"
ENV_VARS+=",CALLBACK_JOB_BATCH_SIZE=10"
ENV_VARS+=",CALLBACK_JOB_EXPIRY_MINUTES=${CALLBACK_JOB_EXPIRY_MINUTES:-1}"
ENV_VARS+=",CALLBACK_JOB_RETRY_BACKOFF_SECONDS=${CALLBACK_JOB_RETRY_BACKOFF_SECONDS:-0}"
ENV_VARS+=",KAKAO_CALLBACK_TIMEOUT_SECONDS=${KAKAO_CALLBACK_TIMEOUT_SECONDS:-5}"
ENV_VARS+=",CALLBACK_PROCESSING_BUDGET_SECONDS=${CALLBACK_PROCESSING_BUDGET_SECONDS:-50}"
ENV_VARS+=",GEMINI_HTTP_TIMEOUT_MS=${GEMINI_HTTP_TIMEOUT_MS:-540000}"
ENV_VARS+=",ADMIN_AUTH_MODE=oidc"
ENV_VARS+=",ADMIN_OIDC_ALLOWED_EMAILS=${ALLOWED_EMAILS}"
ENV_VARS+=",ADMIN_DASHBOARD_ENABLED=${ADMIN_DASHBOARD_ENABLED}"
ENV_VARS+=",ADMIN_DASHBOARD_USERNAME=${ADMIN_DASHBOARD_USERNAME}"
ENV_VARS+=",GEMINI_MODEL=gemini-3.5-flash"
ENV_VARS+=",GEMINI_TEMPERATURE=0.1"
ENV_VARS+=",GEMINI_MAX_OUTPUT_TOKENS=3072"
ENV_VARS+=",GEMINI_THINKING_LEVEL=low"
ENV_VARS+=",GEMINI_WIKI_MODEL=gemini-3.5-flash"
ENV_VARS+=",GEMINI_WIKI_THINKING_LEVEL=medium"
ENV_VARS+=",ROUTER_MODE=gemini"
ENV_VARS+=",GEMINI_ROUTER_MODEL=gemini-3.5-flash"
ENV_VARS+=",GEMINI_ROUTER_TEMPERATURE=0.0"
ENV_VARS+=",GEMINI_ROUTER_MAX_OUTPUT_TOKENS=1024"
ENV_VARS+=",GEMINI_ROUTER_THINKING_LEVEL=low"
ENV_VARS+=",ROUTER_MIN_CONFIDENCE=0.55"
ENV_VARS+=",ROUTER_MAX_CATALOG_PAGES=30"
ENV_VARS+=",GEMINI_PARSING_MODEL=gemini-3-flash-preview"
ENV_VARS+=",GEMINI_PARSING_MAX_OUTPUT_TOKENS=32768"
ENV_VARS+=",GEMINI_PARSING_TEMPERATURE=0.0"
ENV_VARS+=",GEMINI_PARSING_TOP_K=1"
ENV_VARS+=",GEMINI_PARSING_THINKING_LEVEL=minimal"
ENV_VARS+=",LOG_LEVEL=INFO"

SET_SECRETS="GEMINI_API_KEY=${GEMINI_SECRET_NAME}:latest,UPLOAD_TOKEN_SECRET=${UPLOAD_TOKEN_SECRET_NAME}:latest"
if [[ "$ADMIN_DASHBOARD_ENABLED" == "true" ]]; then
  SET_SECRETS="${SET_SECRETS},ADMIN_DASHBOARD_PASSWORD=${ADMIN_DASHBOARD_PASSWORD_SECRET_NAME}:latest"
fi

gcloud run deploy "$SERVICE_NAME" \
  --quiet \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --service-account "$RUN_SA" \
  --timeout "$CLOUD_RUN_TIMEOUT_SECONDS" \
  --memory 1Gi \
  --cpu 1 \
  --min-instances 1 \
  --set-env-vars "$ENV_VARS" \
  --set-secrets "$SET_SECRETS"

SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" \
  --region "$REGION" \
  --format='value(status.url)')

# The app verifies the OIDC aud claim against this exact value.
gcloud run services update "$SERVICE_NAME" \
  --quiet \
  --region "$REGION" \
  --update-env-vars "ADMIN_OIDC_AUDIENCE=${SERVICE_URL},CLOUD_TASKS_BASE_URL=${SERVICE_URL},CLOUD_TASKS_AUDIENCE=${SERVICE_URL},UPLOAD_BASE_URL=${SERVICE_URL}"

gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
  --quiet \
  --region "$REGION" \
  --member="serviceAccount:${SCHEDULER_SA}" \
  --role="roles/run.invoker" >/dev/null

gcloud run services add-iam-policy-binding "$SERVICE_NAME" \
  --quiet \
  --region "$REGION" \
  --member="serviceAccount:${CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL}" \
  --role="roles/run.invoker" >/dev/null

upsert_scheduler_job() {
  local job_name="$1"
  local uri="$2"
  local schedule="$3"

  if gcloud scheduler jobs describe "$job_name" --location "$REGION" >/dev/null 2>&1; then
    gcloud scheduler jobs update http "$job_name" \
      --location "$REGION" \
      --schedule "$schedule" \
      --uri "$uri" \
      --http-method POST \
      --oidc-service-account-email "$SCHEDULER_SA" \
      --oidc-token-audience "$SERVICE_URL"
  else
    gcloud scheduler jobs create http "$job_name" \
      --location "$REGION" \
      --schedule "$schedule" \
      --uri "$uri" \
      --http-method POST \
      --oidc-service-account-email "$SCHEDULER_SA" \
      --oidc-token-audience "$SERVICE_URL"
  fi
}

upsert_scheduler_job \
  "$DRIVE_CHANGES_JOB_NAME" \
  "${SERVICE_URL}/admin/sync-drive-changes" \
  "* * * * *"

upsert_scheduler_job \
  "$FULL_SYNC_JOB_NAME" \
  "${SERVICE_URL}/admin/sync-drive" \
  "0 3 * * *"

upsert_scheduler_job \
  "$CALLBACK_JOBS_JOB_NAME" \
  "${SERVICE_URL}/admin/process-callback-jobs" \
  "* * * * *"

upsert_scheduler_job \
  "$GEMINI_FILES_CLEANUP_JOB_NAME" \
  "${SERVICE_URL}/admin/cleanup-gemini-files" \
  "*/30 * * * *"

pause_scheduler_job_if_exists "$LEGACY_CHAT_LOG_EXPORT_JOB_NAME"

if [[ "$DEPLOY_FIRESTORE_INDEXES" == "true" ]]; then
  if command -v firebase >/dev/null 2>&1; then
    firebase deploy --only firestore:indexes --project "$PROJECT_ID"
  else
    echo "DEPLOY_FIRESTORE_INDEXES=true but firebase CLI was not found. Skipping index deploy." >&2
  fi
fi

echo "Deployed ${SERVICE_NAME}: ${SERVICE_URL}"
echo "Firestore database: ${FIRESTORE_DATABASE_ID}"
echo "Firestore indexes ensured: ${ENSURE_FIRESTORE_INDEXES}"
echo "Admin OIDC audience: ${SERVICE_URL}"
echo "Allowed admin caller: ${ALLOWED_EMAILS}"
echo "Cloud Run timeout seconds: ${CLOUD_RUN_TIMEOUT_SECONDS}"
echo "Upload base URL: ${SERVICE_URL}"
echo "Upload token secret: ${UPLOAD_TOKEN_SECRET_NAME}"
echo "Callback Cloud Tasks queue: ${CALLBACK_TASKS_QUEUE_NAME}"
echo "Callback queue rate/concurrency/attempts: ${CALLBACK_TASKS_MAX_DISPATCHES_PER_SECOND}/${CALLBACK_TASKS_MAX_CONCURRENT_DISPATCHES}/${CALLBACK_TASKS_MAX_ATTEMPTS}"
echo "Prewarm Cloud Tasks queue: ${PREWARM_TASKS_QUEUE_NAME}"
echo "Prewarm queue rate/concurrency/attempts: ${PREWARM_TASKS_MAX_DISPATCHES_PER_SECOND}/${PREWARM_TASKS_MAX_CONCURRENT_DISPATCHES}/${PREWARM_TASKS_MAX_ATTEMPTS}"
echo "Admin dashboard enabled: ${ADMIN_DASHBOARD_ENABLED}"
echo "Legacy chat log export scheduler paused if present: ${LEGACY_CHAT_LOG_EXPORT_JOB_NAME}"
echo "Wiki rebuild worker mode: ${WIKI_REBUILD_WORKER_MODE}"
echo "Wiki rebuild Cloud Tasks queue: ${WIKI_REBUILD_TASKS_QUEUE_NAME}"
echo "Wiki rebuild queue rate/concurrency/attempts: ${WIKI_REBUILD_TASKS_MAX_DISPATCHES_PER_SECOND}/${WIKI_REBUILD_TASKS_MAX_CONCURRENT_DISPATCHES}/${WIKI_REBUILD_TASKS_MAX_ATTEMPTS}"
echo "Cloud Tasks service account: ${CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL}"
