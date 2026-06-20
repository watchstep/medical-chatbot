#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-medical-chatbot-494315}"
REGION="${REGION:-asia-northeast3}"
SERVICE_NAME="${SERVICE_NAME:-medical-chatbot-test}"
FIRESTORE_DATABASE_ID="${FIRESTORE_DATABASE_ID:-medical-chatbot-test}"
DEPLOY_FIRESTORE_INDEXES="${DEPLOY_FIRESTORE_INDEXES:-false}"
ENSURE_FIRESTORE_INDEXES="${ENSURE_FIRESTORE_INDEXES:-true}"
CREATE_SCHEDULER_JOBS="${CREATE_SCHEDULER_JOBS:-true}"
SCHEDULER_TIME_ZONE="${SCHEDULER_TIME_ZONE:-Asia/Seoul}"

RUN_SA="${RUN_SA:-medical-chatbot-test-run@${PROJECT_ID}.iam.gserviceaccount.com}"
SCHEDULER_SA="${SCHEDULER_SA:-medical-chatbot-test-scheduler@${PROJECT_ID}.iam.gserviceaccount.com}"
DRIVE_CHANGES_JOB_NAME="${DRIVE_CHANGES_JOB_NAME:-medical-chatbot-test-sync-drive-changes}"
FULL_SYNC_JOB_NAME="${FULL_SYNC_JOB_NAME:-medical-chatbot-test-sync-drive-full}"
CALLBACK_JOBS_JOB_NAME="${CALLBACK_JOBS_JOB_NAME:-medical-chatbot-test-process-callback-jobs}"
GEMINI_FILES_CLEANUP_JOB_NAME="${GEMINI_FILES_CLEANUP_JOB_NAME:-medical-chatbot-test-cleanup-gemini-files}"
LEGACY_CHAT_LOG_EXPORT_JOB_NAME="${LEGACY_CHAT_LOG_EXPORT_JOB_NAME:-medical-chatbot-test-export-chat-logs}"
CALLBACK_TASKS_QUEUE_NAME="${CALLBACK_TASKS_QUEUE_NAME:-medical-chatbot-test-callback-jobs}"
PREWARM_TASKS_QUEUE_NAME="${PREWARM_TASKS_QUEUE_NAME:-medical-chatbot-test-prewarm}"
WIKI_REBUILD_TASKS_QUEUE_NAME="${WIKI_REBUILD_TASKS_QUEUE_NAME:-medical-chatbot-test-wiki-rebuild}"
CLOUD_TASKS_LOCATION="${CLOUD_TASKS_LOCATION:-${REGION}}"
CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL="${CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL:-${SCHEDULER_SA}}"
CALLBACK_TASKS_DISPATCH_DEADLINE_SECONDS="${CALLBACK_TASKS_DISPATCH_DEADLINE_SECONDS:-55}"
GEMINI_FILE_PREWARM_DISPATCH_DEADLINE_SECONDS="${GEMINI_FILE_PREWARM_DISPATCH_DEADLINE_SECONDS:-600}"
WIKI_REBUILD_TASKS_DISPATCH_DEADLINE_SECONDS="${WIKI_REBUILD_TASKS_DISPATCH_DEADLINE_SECONDS:-600}"
CLOUD_RUN_TIMEOUT_SECONDS="${CLOUD_RUN_TIMEOUT_SECONDS:-900}"
CLOUD_RUN_MEMORY="${CLOUD_RUN_MEMORY:-2Gi}"
CLOUD_RUN_CPU="${CLOUD_RUN_CPU:-2}"
CLOUD_RUN_MIN_INSTANCES="${CLOUD_RUN_MIN_INSTANCES:-1}"
CLOUD_RUN_MAX_INSTANCES="${CLOUD_RUN_MAX_INSTANCES:-8}"
CLOUD_RUN_CONCURRENCY="${CLOUD_RUN_CONCURRENCY:-20}"

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

WIKI_REBUILD_TASKS_MAX_DISPATCHES_PER_SECOND="${WIKI_REBUILD_TASKS_MAX_DISPATCHES_PER_SECOND:-0.2}"
WIKI_REBUILD_TASKS_MAX_CONCURRENT_DISPATCHES="${WIKI_REBUILD_TASKS_MAX_CONCURRENT_DISPATCHES:-1}"
WIKI_REBUILD_TASKS_MAX_ATTEMPTS="${WIKI_REBUILD_TASKS_MAX_ATTEMPTS:-4}"
WIKI_REBUILD_TASKS_MIN_BACKOFF="${WIKI_REBUILD_TASKS_MIN_BACKOFF:-60s}"
WIKI_REBUILD_TASKS_MAX_BACKOFF="${WIKI_REBUILD_TASKS_MAX_BACKOFF:-300s}"
WIKI_REBUILD_TASKS_MAX_DOUBLINGS="${WIKI_REBUILD_TASKS_MAX_DOUBLINGS:-2}"
WIKI_REBUILD_TASKS_MAX_RETRY_DURATION="${WIKI_REBUILD_TASKS_MAX_RETRY_DURATION:-1800s}"

APP_ENV="${APP_ENV:-test}"
APP_NAME="${APP_NAME:-medical-chatbot-test}"
GOOGLE_DRIVE_ROOT_FOLDER_ID="${GOOGLE_DRIVE_ROOT_FOLDER_ID:-}"
GOOGLE_DRIVE_ROOT_FOLDER_NAME="${GOOGLE_DRIVE_ROOT_FOLDER_NAME:-medical-chatbot-local}"
PATIENTS_FOLDER_NAME="${PATIENTS_FOLDER_NAME:-patients}"
SYSTEM_FOLDER_NAME="${SYSTEM_FOLDER_NAME:-_system}"
DRIVE_PATIENT_BOOTSTRAP_ON_FULL_SYNC="${DRIVE_PATIENT_BOOTSTRAP_ON_FULL_SYNC:-true}"
DRIVE_CHANGES_SCOPE_ID="${DRIVE_CHANGES_SCOPE_ID:-test}"

ADMIN_AUTH_MODE="${ADMIN_AUTH_MODE:-oidc}"
ADMIN_SYNC_TOKEN="${ADMIN_SYNC_TOKEN:-}"
ADMIN_EXTRA_ALLOWED_EMAILS="${ADMIN_EXTRA_ALLOWED_EMAILS:-}"

MEDICAL_WIKI_EXTRACTION_MODE="${MEDICAL_WIKI_EXTRACTION_MODE:-gemini}"
ROUTER_MODE="${ROUTER_MODE:-gemini}"
CALLBACK_WORKER_MODE="${CALLBACK_WORKER_MODE:-cloud_tasks}"
WIKI_REBUILD_WORKER_MODE="${WIKI_REBUILD_WORKER_MODE:-cloud_tasks}"

GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.5-flash}"
GEMINI_WIKI_MODEL="${GEMINI_WIKI_MODEL:-${GEMINI_MODEL}}"
GEMINI_ROUTER_MODEL="${GEMINI_ROUTER_MODEL:-${GEMINI_MODEL}}"
GEMINI_TEMPERATURE="${GEMINI_TEMPERATURE:-0.1}"
GEMINI_MAX_OUTPUT_TOKENS="${GEMINI_MAX_OUTPUT_TOKENS:-3072}"
GEMINI_THINKING_LEVEL="${GEMINI_THINKING_LEVEL:-low}"
GEMINI_WIKI_TEMPERATURE="${GEMINI_WIKI_TEMPERATURE:-0.0}"
GEMINI_WIKI_MAX_OUTPUT_TOKENS="${GEMINI_WIKI_MAX_OUTPUT_TOKENS:-3072}"
GEMINI_WIKI_THINKING_LEVEL="${GEMINI_WIKI_THINKING_LEVEL:-medium}"
GEMINI_ROUTER_TEMPERATURE="${GEMINI_ROUTER_TEMPERATURE:-0.0}"
GEMINI_ROUTER_MAX_OUTPUT_TOKENS="${GEMINI_ROUTER_MAX_OUTPUT_TOKENS:-1024}"
GEMINI_ROUTER_THINKING_LEVEL="${GEMINI_ROUTER_THINKING_LEVEL:-low}"
ROUTER_MIN_CONFIDENCE="${ROUTER_MIN_CONFIDENCE:-0.55}"
ROUTER_MAX_CATALOG_PAGES="${ROUTER_MAX_CATALOG_PAGES:-30}"

FILE_READY_WAIT_SECONDS="${FILE_READY_WAIT_SECONDS:-30}"
FILE_READY_POLL_INTERVAL_SECONDS="${FILE_READY_POLL_INTERVAL_SECONDS:-3}"
FILE_UPLOAD_LOCK_LEASE_SECONDS="${FILE_UPLOAD_LOCK_LEASE_SECONDS:-600}"
FILE_UPLOAD_MAX_RETRY_COUNT="${FILE_UPLOAD_MAX_RETRY_COUNT:-3}"
FILE_EXPIRATION_MARGIN_SECONDS="${FILE_EXPIRATION_MARGIN_SECONDS:-300}"
GEMINI_FILES_STORAGE_SOFT_LIMIT_BYTES="${GEMINI_FILES_STORAGE_SOFT_LIMIT_BYTES:-15032385536}"
GEMINI_FILES_STORAGE_TARGET_BYTES="${GEMINI_FILES_STORAGE_TARGET_BYTES:-12884901888}"
GEMINI_FILES_STORAGE_HARD_LIMIT_BYTES="${GEMINI_FILES_STORAGE_HARD_LIMIT_BYTES:-18253611008}"
GEMINI_FILES_CLEANUP_BATCH_SIZE="${GEMINI_FILES_CLEANUP_BATCH_SIZE:-20}"
GEMINI_FILE_PREWARM_ENABLED="${GEMINI_FILE_PREWARM_ENABLED:-true}"
GEMINI_FILE_PREWARM_MAX_ATTEMPTS="${GEMINI_FILE_PREWARM_MAX_ATTEMPTS:-3}"
GEMINI_FILE_PREWARM_LEASE_SECONDS="${GEMINI_FILE_PREWARM_LEASE_SECONDS:-570}"
GEMINI_FILE_PREWARM_IDEMPOTENCY_BUCKET_MINUTES="${GEMINI_FILE_PREWARM_IDEMPOTENCY_BUCKET_MINUTES:-60}"
GEMINI_FILE_PREWARM_EXPIRY_MINUTES="${GEMINI_FILE_PREWARM_EXPIRY_MINUTES:-30}"
GEMINI_FILE_PREWARM_DISPATCH_DEADLINE_SECONDS="${GEMINI_FILE_PREWARM_DISPATCH_DEADLINE_SECONDS}"

CALLBACK_JOB_LEASE_SECONDS="${CALLBACK_JOB_LEASE_SECONDS:-50}"
CALLBACK_JOB_MAX_ATTEMPTS="${CALLBACK_JOB_MAX_ATTEMPTS:-5}"
CALLBACK_JOB_BATCH_SIZE="${CALLBACK_JOB_BATCH_SIZE:-10}"
CALLBACK_JOB_EXPIRY_MINUTES="${CALLBACK_JOB_EXPIRY_MINUTES:-1}"
CALLBACK_JOB_RETRY_BACKOFF_SECONDS="${CALLBACK_JOB_RETRY_BACKOFF_SECONDS:-0}"
KAKAO_CALLBACK_TIMEOUT_SECONDS="${KAKAO_CALLBACK_TIMEOUT_SECONDS:-5}"
CALLBACK_PROCESSING_BUDGET_SECONDS="${CALLBACK_PROCESSING_BUDGET_SECONDS:-45}"
ASYNC_WORKER_PROCESSING_TIMEOUT_SECONDS="${ASYNC_WORKER_PROCESSING_TIMEOUT_SECONDS:-540}"
GEMINI_HTTP_TIMEOUT_MS="${GEMINI_HTTP_TIMEOUT_MS:-540000}"

MAX_CHANGES_PER_RUN="${MAX_CHANGES_PER_RUN:-20}"
MAX_CHANGES_PAGES_PER_RUN="${MAX_CHANGES_PAGES_PER_RUN:-5}"
MAX_WIKI_PAGE_GENERATIONS_PER_RUN="${MAX_WIKI_PAGE_GENERATIONS_PER_RUN:-3}"
MAX_WIKI_BACKLOG_PER_RUN="${MAX_WIKI_BACKLOG_PER_RUN:-10}"
CHANGES_SYNC_LOCK_LEASE_MINUTES="${CHANGES_SYNC_LOCK_LEASE_MINUTES:-2}"
FULL_SYNC_LOCK_LEASE_MINUTES="${FULL_SYNC_LOCK_LEASE_MINUTES:-10}"
FOLDER_INDEX_BOOTSTRAP_ON_CHANGES="${FOLDER_INDEX_BOOTSTRAP_ON_CHANGES:-true}"
SESSION_TTL_MINUTES="${SESSION_TTL_MINUTES:-1440}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

GEMINI_SECRET_NAME="${GEMINI_SECRET_NAME:-gemini-api-key}"
UPLOAD_TOKEN_SECRET_NAME="${UPLOAD_TOKEN_SECRET_NAME:-upload-token-secret-test}"
ADMIN_DASHBOARD_ENABLED="${ADMIN_DASHBOARD_ENABLED:-false}"
ADMIN_DASHBOARD_USERNAME="${ADMIN_DASHBOARD_USERNAME:-admin}"
ADMIN_DASHBOARD_PASSWORD_SECRET_NAME="${ADMIN_DASHBOARD_PASSWORD_SECRET_NAME:-admin-dashboard-password}"

UPLOAD_TOKEN_TTL_MINUTES="${UPLOAD_TOKEN_TTL_MINUTES:-15}"
CHAT_ATTACHMENT_TTL_MINUTES="${CHAT_ATTACHMENT_TTL_MINUTES:-60}"
MAX_UPLOAD_FILE_BYTES="${MAX_UPLOAD_FILE_BYTES:-20971520}"

if [[ -z "${GOOGLE_DRIVE_ROOT_FOLDER_ID}" ]]; then
  echo "WARNING: GOOGLE_DRIVE_ROOT_FOLDER_ID is empty." >&2
  echo "The app will try to find the Drive root by GOOGLE_DRIVE_ROOT_FOLDER_NAME=${GOOGLE_DRIVE_ROOT_FOLDER_NAME}." >&2
  echo "For staging/test, using GOOGLE_DRIVE_ROOT_FOLDER_ID is safer." >&2
fi

gcloud config set project "${PROJECT_ID}"

gcloud services enable cloudtasks.googleapis.com run.googleapis.com firestore.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com --project "${PROJECT_ID}" >/dev/null


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
  if ! gcloud iam service-accounts describe "${email}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud iam service-accounts create "${name}" \
      --project "${PROJECT_ID}" \
      --display-name="${display_name}"
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

  gcloud tasks queues update "${queue_name}" \
    --project "${PROJECT_ID}" \
    --location "${CLOUD_TASKS_LOCATION}" \
    --max-dispatches-per-second="${rate}" \
    --max-concurrent-dispatches="${concurrency}" \
    --max-attempts="${attempts}" \
    --min-backoff="${min_backoff}" \
    --max-backoff="${max_backoff}" \
    --max-doublings="${doublings}" \
    --max-retry-duration="${retry_duration}" >/dev/null
}

grant_secret_access() {
  local secret_name="$1"
  if [[ -z "${secret_name}" ]]; then
    return
  fi
  gcloud secrets add-iam-policy-binding "${secret_name}" \
    --project "${PROJECT_ID}" \
    --member="serviceAccount:${RUN_SA}" \
    --role="roles/secretmanager.secretAccessor" >/dev/null
}

require_secret_exists() {
  local secret_name="$1"
  if ! gcloud secrets describe "${secret_name}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    echo "Required Secret Manager secret not found: ${secret_name}" >&2
    echo "Create it or run scripts/deploy-test.sh with UPLOAD_TOKEN_SECRET set." >&2
    exit 1
  fi
}

upsert_secret_value_if_present() {
  local secret_name="$1"
  local secret_value="$2"
  if [[ -z "${secret_value}" ]]; then
    return
  fi
  if ! gcloud secrets describe "${secret_name}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud secrets create "${secret_name}" \
      --project "${PROJECT_ID}" \
      --replication-policy="automatic" >/dev/null
  fi
  printf "%s" "${secret_value}" | gcloud secrets versions add "${secret_name}" \
    --project "${PROJECT_ID}" \
    --data-file=- >/dev/null
  echo "Updated upload token secret: ${secret_name}"
}

pause_scheduler_job_if_exists() {
  local job_name="$1"
  if gcloud scheduler jobs describe "${job_name}" --project "${PROJECT_ID}" --location "${REGION}" >/dev/null 2>&1; then
    gcloud scheduler jobs pause "${job_name}" \
      --project "${PROJECT_ID}" \
      --location "${REGION}" >/dev/null || true
    echo "Paused legacy scheduler job: ${job_name}"
  fi
}

if [[ "${ENSURE_FIRESTORE_INDEXES}" == "true" ]]; then
  ensure_firestore_composite_indexes
fi

ensure_service_account "${RUN_SA}" "Medical Chatbot Test Cloud Run Runtime"
ensure_service_account "${SCHEDULER_SA}" "Medical Chatbot Test Scheduler Invoker"
if [[ "${CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL}" != "${SCHEDULER_SA}" ]]; then
  ensure_service_account "${CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL}" "Medical Chatbot Test Cloud Tasks Invoker"
fi


gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${RUN_SA}" \
  --role="roles/datastore.user" >/dev/null

upsert_secret_value_if_present "${UPLOAD_TOKEN_SECRET_NAME}" "${UPLOAD_TOKEN_SECRET:-}"

grant_secret_access "${GEMINI_SECRET_NAME}"
require_secret_exists "${UPLOAD_TOKEN_SECRET_NAME}"
grant_secret_access "${UPLOAD_TOKEN_SECRET_NAME}"
if [[ "${ADMIN_DASHBOARD_ENABLED}" == "true" ]]; then
  grant_secret_access "${ADMIN_DASHBOARD_PASSWORD_SECRET_NAME}"
fi

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${RUN_SA}" \
  --role="roles/cloudtasks.enqueuer" >/dev/null

# Required when Cloud Tasks uses CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL for OIDC.
# Without this, create_task fails with iam.serviceAccounts.actAs PERMISSION_DENIED.
gcloud iam service-accounts add-iam-policy-binding "${CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL}" \
  --project "${PROJECT_ID}" \
  --member="serviceAccount:${RUN_SA}" \
  --role="roles/iam.serviceAccountUser" >/dev/null

ALLOWED_EMAILS="${SCHEDULER_SA}"
if [[ "${CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL}" != "${SCHEDULER_SA}" ]]; then
  ALLOWED_EMAILS="${ALLOWED_EMAILS},${CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL}"
fi
if [[ -n "${ADMIN_EXTRA_ALLOWED_EMAILS}" ]]; then
  ALLOWED_EMAILS="${ALLOWED_EMAILS},${ADMIN_EXTRA_ALLOWED_EMAILS}"
fi

if ! gcloud tasks queues describe "${CALLBACK_TASKS_QUEUE_NAME}" \
  --project "${PROJECT_ID}" \
  --location "${CLOUD_TASKS_LOCATION}" >/dev/null 2>&1; then
  gcloud tasks queues create "${CALLBACK_TASKS_QUEUE_NAME}" \
    --project "${PROJECT_ID}" \
    --location "${CLOUD_TASKS_LOCATION}" >/dev/null
fi

if ! gcloud tasks queues describe "${PREWARM_TASKS_QUEUE_NAME}" \
  --project "${PROJECT_ID}" \
  --location "${CLOUD_TASKS_LOCATION}" >/dev/null 2>&1; then
  gcloud tasks queues create "${PREWARM_TASKS_QUEUE_NAME}" \
    --project "${PROJECT_ID}" \
    --location "${CLOUD_TASKS_LOCATION}" >/dev/null
fi

if ! gcloud tasks queues describe "${WIKI_REBUILD_TASKS_QUEUE_NAME}" \
  --project "${PROJECT_ID}" \
  --location "${CLOUD_TASKS_LOCATION}" >/dev/null 2>&1; then
  gcloud tasks queues create "${WIKI_REBUILD_TASKS_QUEUE_NAME}" \
    --project "${PROJECT_ID}" \
    --location "${CLOUD_TASKS_LOCATION}" >/dev/null
fi

configure_tasks_queue "${CALLBACK_TASKS_QUEUE_NAME}" \
  "${CALLBACK_TASKS_MAX_DISPATCHES_PER_SECOND}" \
  "${CALLBACK_TASKS_MAX_CONCURRENT_DISPATCHES}" \
  "${CALLBACK_TASKS_MAX_ATTEMPTS}" \
  "${CALLBACK_TASKS_MIN_BACKOFF}" \
  "${CALLBACK_TASKS_MAX_BACKOFF}" \
  "${CALLBACK_TASKS_MAX_DOUBLINGS}" \
  "${CALLBACK_TASKS_MAX_RETRY_DURATION}"

configure_tasks_queue "${PREWARM_TASKS_QUEUE_NAME}" \
  "${PREWARM_TASKS_MAX_DISPATCHES_PER_SECOND}" \
  "${PREWARM_TASKS_MAX_CONCURRENT_DISPATCHES}" \
  "${PREWARM_TASKS_MAX_ATTEMPTS}" \
  "${PREWARM_TASKS_MIN_BACKOFF}" \
  "${PREWARM_TASKS_MAX_BACKOFF}" \
  "${PREWARM_TASKS_MAX_DOUBLINGS}" \
  "${PREWARM_TASKS_MAX_RETRY_DURATION}"

configure_tasks_queue "${WIKI_REBUILD_TASKS_QUEUE_NAME}" \
  "${WIKI_REBUILD_TASKS_MAX_DISPATCHES_PER_SECOND}" \
  "${WIKI_REBUILD_TASKS_MAX_CONCURRENT_DISPATCHES}" \
  "${WIKI_REBUILD_TASKS_MAX_ATTEMPTS}" \
  "${WIKI_REBUILD_TASKS_MIN_BACKOFF}" \
  "${WIKI_REBUILD_TASKS_MAX_BACKOFF}" \
  "${WIKI_REBUILD_TASKS_MAX_DOUBLINGS}" \
  "${WIKI_REBUILD_TASKS_MAX_RETRY_DURATION}"

ENV_VARS_FILE="$(mktemp)"
cleanup() {
  rm -f "${ENV_VARS_FILE}"
}
trap cleanup EXIT

cat > "${ENV_VARS_FILE}" <<YAML
APP_ENV: "${APP_ENV}"
APP_NAME: "${APP_NAME}"
FIRESTORE_PROJECT_ID: "${PROJECT_ID}"
FIRESTORE_DATABASE_ID: "${FIRESTORE_DATABASE_ID}"
GOOGLE_DRIVE_ROOT_FOLDER_ID: "${GOOGLE_DRIVE_ROOT_FOLDER_ID}"
GOOGLE_DRIVE_ROOT_FOLDER_NAME: "${GOOGLE_DRIVE_ROOT_FOLDER_NAME}"
PATIENTS_FOLDER_NAME: "${PATIENTS_FOLDER_NAME}"
SYSTEM_FOLDER_NAME: "${SYSTEM_FOLDER_NAME}"
DRIVE_PATIENT_BOOTSTRAP_ON_FULL_SYNC: "${DRIVE_PATIENT_BOOTSTRAP_ON_FULL_SYNC}"
DRIVE_CHANGES_SCOPE_ID: "${DRIVE_CHANGES_SCOPE_ID}"
ADMIN_AUTH_MODE: "${ADMIN_AUTH_MODE}"
ADMIN_SYNC_TOKEN: "${ADMIN_SYNC_TOKEN}"
ADMIN_OIDC_ALLOWED_EMAILS: "${ALLOWED_EMAILS}"
ADMIN_OIDC_AUDIENCE: ""
ADMIN_DASHBOARD_ENABLED: "${ADMIN_DASHBOARD_ENABLED}"
ADMIN_DASHBOARD_USERNAME: "${ADMIN_DASHBOARD_USERNAME}"
MEDICAL_WIKI_EXTRACTION_MODE: "${MEDICAL_WIKI_EXTRACTION_MODE}"
ROUTER_MODE: "${ROUTER_MODE}"
CALLBACK_WORKER_MODE: "${CALLBACK_WORKER_MODE}"
CLOUD_TASKS_PROJECT_ID: "${PROJECT_ID}"
CLOUD_TASKS_LOCATION: "${CLOUD_TASKS_LOCATION}"
CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL: "${CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL}"
CLOUD_TASKS_BASE_URL: ""
CLOUD_TASKS_AUDIENCE: ""
UPLOAD_TOKEN_TTL_MINUTES: "${UPLOAD_TOKEN_TTL_MINUTES}"
CHAT_ATTACHMENT_TTL_MINUTES: "${CHAT_ATTACHMENT_TTL_MINUTES}"
MAX_UPLOAD_FILE_BYTES: "${MAX_UPLOAD_FILE_BYTES}"
UPLOAD_BASE_URL: ""
CALLBACK_TASKS_QUEUE_NAME: "${CALLBACK_TASKS_QUEUE_NAME}"
CALLBACK_TASKS_DISPATCH_DEADLINE_SECONDS: "${CALLBACK_TASKS_DISPATCH_DEADLINE_SECONDS}"
PREWARM_TASKS_QUEUE_NAME: "${PREWARM_TASKS_QUEUE_NAME}"
WIKI_REBUILD_WORKER_MODE: "${WIKI_REBUILD_WORKER_MODE}"
WIKI_REBUILD_TASKS_QUEUE_NAME: "${WIKI_REBUILD_TASKS_QUEUE_NAME}"
WIKI_REBUILD_TASKS_DISPATCH_DEADLINE_SECONDS: "${WIKI_REBUILD_TASKS_DISPATCH_DEADLINE_SECONDS}"
ASYNC_WORKER_PROCESSING_TIMEOUT_SECONDS: "${ASYNC_WORKER_PROCESSING_TIMEOUT_SECONDS}"
GEMINI_FILE_PREWARM_ENABLED: "${GEMINI_FILE_PREWARM_ENABLED}"
GEMINI_FILE_PREWARM_MAX_ATTEMPTS: "${GEMINI_FILE_PREWARM_MAX_ATTEMPTS}"
GEMINI_FILE_PREWARM_LEASE_SECONDS: "${GEMINI_FILE_PREWARM_LEASE_SECONDS}"
GEMINI_FILE_PREWARM_IDEMPOTENCY_BUCKET_MINUTES: "${GEMINI_FILE_PREWARM_IDEMPOTENCY_BUCKET_MINUTES}"
GEMINI_FILE_PREWARM_EXPIRY_MINUTES: "${GEMINI_FILE_PREWARM_EXPIRY_MINUTES}"
GEMINI_FILE_PREWARM_DISPATCH_DEADLINE_SECONDS: "${GEMINI_FILE_PREWARM_DISPATCH_DEADLINE_SECONDS}"
GEMINI_MODEL: "${GEMINI_MODEL}"
GEMINI_WIKI_MODEL: "${GEMINI_WIKI_MODEL}"
GEMINI_ROUTER_MODEL: "${GEMINI_ROUTER_MODEL}"
GEMINI_TEMPERATURE: "${GEMINI_TEMPERATURE}"
GEMINI_MAX_OUTPUT_TOKENS: "${GEMINI_MAX_OUTPUT_TOKENS}"
GEMINI_THINKING_LEVEL: "${GEMINI_THINKING_LEVEL}"
GEMINI_WIKI_TEMPERATURE: "${GEMINI_WIKI_TEMPERATURE}"
GEMINI_WIKI_MAX_OUTPUT_TOKENS: "${GEMINI_WIKI_MAX_OUTPUT_TOKENS}"
GEMINI_WIKI_THINKING_LEVEL: "${GEMINI_WIKI_THINKING_LEVEL}"
GEMINI_ROUTER_TEMPERATURE: "${GEMINI_ROUTER_TEMPERATURE}"
GEMINI_ROUTER_MAX_OUTPUT_TOKENS: "${GEMINI_ROUTER_MAX_OUTPUT_TOKENS}"
GEMINI_ROUTER_THINKING_LEVEL: "${GEMINI_ROUTER_THINKING_LEVEL}"
ROUTER_MIN_CONFIDENCE: "${ROUTER_MIN_CONFIDENCE}"
ROUTER_MAX_CATALOG_PAGES: "${ROUTER_MAX_CATALOG_PAGES}"
FILE_READY_WAIT_SECONDS: "${FILE_READY_WAIT_SECONDS}"
FILE_READY_POLL_INTERVAL_SECONDS: "${FILE_READY_POLL_INTERVAL_SECONDS}"
FILE_UPLOAD_LOCK_LEASE_SECONDS: "${FILE_UPLOAD_LOCK_LEASE_SECONDS}"
FILE_UPLOAD_MAX_RETRY_COUNT: "${FILE_UPLOAD_MAX_RETRY_COUNT}"
FILE_EXPIRATION_MARGIN_SECONDS: "${FILE_EXPIRATION_MARGIN_SECONDS}"
GEMINI_FILES_STORAGE_SOFT_LIMIT_BYTES: "${GEMINI_FILES_STORAGE_SOFT_LIMIT_BYTES}"
GEMINI_FILES_STORAGE_TARGET_BYTES: "${GEMINI_FILES_STORAGE_TARGET_BYTES}"
GEMINI_FILES_STORAGE_HARD_LIMIT_BYTES: "${GEMINI_FILES_STORAGE_HARD_LIMIT_BYTES}"
GEMINI_FILES_CLEANUP_BATCH_SIZE: "${GEMINI_FILES_CLEANUP_BATCH_SIZE}"
CALLBACK_JOB_LEASE_SECONDS: "${CALLBACK_JOB_LEASE_SECONDS}"
CALLBACK_JOB_MAX_ATTEMPTS: "${CALLBACK_JOB_MAX_ATTEMPTS}"
CALLBACK_JOB_BATCH_SIZE: "${CALLBACK_JOB_BATCH_SIZE}"
CALLBACK_JOB_EXPIRY_MINUTES: "${CALLBACK_JOB_EXPIRY_MINUTES}"
CALLBACK_JOB_RETRY_BACKOFF_SECONDS: "${CALLBACK_JOB_RETRY_BACKOFF_SECONDS}"
KAKAO_CALLBACK_TIMEOUT_SECONDS: "${KAKAO_CALLBACK_TIMEOUT_SECONDS}"
CALLBACK_PROCESSING_BUDGET_SECONDS: "${CALLBACK_PROCESSING_BUDGET_SECONDS}"
GEMINI_HTTP_TIMEOUT_MS: "${GEMINI_HTTP_TIMEOUT_MS}"
MAX_CHANGES_PER_RUN: "${MAX_CHANGES_PER_RUN}"
MAX_CHANGES_PAGES_PER_RUN: "${MAX_CHANGES_PAGES_PER_RUN}"
MAX_WIKI_PAGE_GENERATIONS_PER_RUN: "${MAX_WIKI_PAGE_GENERATIONS_PER_RUN}"
MAX_WIKI_BACKLOG_PER_RUN: "${MAX_WIKI_BACKLOG_PER_RUN}"
CHANGES_SYNC_LOCK_LEASE_MINUTES: "${CHANGES_SYNC_LOCK_LEASE_MINUTES}"
FULL_SYNC_LOCK_LEASE_MINUTES: "${FULL_SYNC_LOCK_LEASE_MINUTES}"
FOLDER_INDEX_BOOTSTRAP_ON_CHANGES: "${FOLDER_INDEX_BOOTSTRAP_ON_CHANGES}"
SESSION_TTL_MINUTES: "${SESSION_TTL_MINUTES}"
LOG_LEVEL: "${LOG_LEVEL}"
YAML

SET_SECRETS="GEMINI_API_KEY=${GEMINI_SECRET_NAME}:latest,UPLOAD_TOKEN_SECRET=${UPLOAD_TOKEN_SECRET_NAME}:latest"
if [[ "${ADMIN_DASHBOARD_ENABLED}" == "true" ]]; then
  SET_SECRETS="${SET_SECRETS},ADMIN_DASHBOARD_PASSWORD=${ADMIN_DASHBOARD_PASSWORD_SECRET_NAME}:latest"
fi

# Deploy test Cloud Run service. Use env-vars-file to safely support comma-separated values.
gcloud run deploy "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --source . \
  --service-account "${RUN_SA}" \
  --timeout "${CLOUD_RUN_TIMEOUT_SECONDS}" \
  --memory "${CLOUD_RUN_MEMORY}" \
  --cpu "${CLOUD_RUN_CPU}" \
  --min-instances "${CLOUD_RUN_MIN_INSTANCES}" \
  --max-instances "${CLOUD_RUN_MAX_INSTANCES}" \
  --concurrency "${CLOUD_RUN_CONCURRENCY}" \
  --allow-unauthenticated \
  --env-vars-file "${ENV_VARS_FILE}" \
  --set-secrets "${SET_SECRETS}"

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --format='value(status.url)')"

# Pin the expected OIDC audience after the URL exists.
gcloud run services update "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --update-env-vars "ADMIN_OIDC_AUDIENCE=${SERVICE_URL},CLOUD_TASKS_BASE_URL=${SERVICE_URL},CLOUD_TASKS_AUDIENCE=${SERVICE_URL},UPLOAD_BASE_URL=${SERVICE_URL}" >/dev/null

gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --member="serviceAccount:${SCHEDULER_SA}" \
  --role="roles/run.invoker" >/dev/null

if [[ "${CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL}" != "${SCHEDULER_SA}" ]]; then
  gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
    --project "${PROJECT_ID}" \
    --region "${REGION}" \
    --member="serviceAccount:${CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL}" \
    --role="roles/run.invoker" >/dev/null
fi

if [[ "${CREATE_SCHEDULER_JOBS}" == "true" ]]; then
  create_or_update_job() {
    local job_name="$1"
    local schedule="$2"
    local path="$3"
    if gcloud scheduler jobs describe "${job_name}" --project "${PROJECT_ID}" --location "${REGION}" >/dev/null 2>&1; then
      gcloud scheduler jobs update http "${job_name}" \
        --project "${PROJECT_ID}" \
        --location "${REGION}" \
        --schedule="${schedule}" \
        --time-zone="${SCHEDULER_TIME_ZONE}" \
        --uri="${SERVICE_URL}${path}" \
        --http-method=POST \
        --oidc-service-account-email="${SCHEDULER_SA}" \
        --oidc-token-audience="${SERVICE_URL}"
    else
      gcloud scheduler jobs create http "${job_name}" \
        --project "${PROJECT_ID}" \
        --location "${REGION}" \
        --schedule="${schedule}" \
        --time-zone="${SCHEDULER_TIME_ZONE}" \
        --uri="${SERVICE_URL}${path}" \
        --http-method=POST \
        --oidc-service-account-email="${SCHEDULER_SA}" \
        --oidc-token-audience="${SERVICE_URL}"
    fi
    gcloud scheduler jobs resume "${job_name}" \
      --project "${PROJECT_ID}" \
      --location "${REGION}" >/dev/null || true
  }

  create_or_update_job "${DRIVE_CHANGES_JOB_NAME}" "*/1 * * * *" "/admin/sync-drive-changes"
  create_or_update_job "${FULL_SYNC_JOB_NAME}" "0 3 * * *" "/admin/sync-drive"
  create_or_update_job "${GEMINI_FILES_CLEANUP_JOB_NAME}" "*/30 * * * *" "/admin/cleanup-gemini-files"
  pause_scheduler_job_if_exists "${LEGACY_CHAT_LOG_EXPORT_JOB_NAME}"

  if [[ "${CALLBACK_WORKER_MODE}" == "cloud_tasks" ]]; then
    if gcloud scheduler jobs describe "${CALLBACK_JOBS_JOB_NAME}" --project "${PROJECT_ID}" --location "${REGION}" >/dev/null 2>&1; then
      gcloud scheduler jobs pause "${CALLBACK_JOBS_JOB_NAME}"         --project "${PROJECT_ID}"         --location "${REGION}" >/dev/null || true
      echo "Paused ${CALLBACK_JOBS_JOB_NAME} because CALLBACK_WORKER_MODE=cloud_tasks."
    fi
  else
    create_or_update_job "${CALLBACK_JOBS_JOB_NAME}" "*/1 * * * *" "/admin/process-callback-jobs"
  fi
else
  echo "CREATE_SCHEDULER_JOBS=false, skipping test Cloud Scheduler jobs."
fi

if [[ "${DEPLOY_FIRESTORE_INDEXES}" == "true" ]]; then
  if command -v firebase >/dev/null 2>&1; then
    firebase deploy --only firestore:indexes --project "${PROJECT_ID}"
  else
    echo "WARNING: firebase CLI not found. Skipping firestore index deploy." >&2
  fi
fi

cat <<SUMMARY

Deployed TEST service: ${SERVICE_NAME}
URL: ${SERVICE_URL}
Firestore project: ${PROJECT_ID}
Firestore database: ${FIRESTORE_DATABASE_ID}
Drive root folder ID: ${GOOGLE_DRIVE_ROOT_FOLDER_ID:-<empty, using name lookup>}
Drive root folder name: ${GOOGLE_DRIVE_ROOT_FOLDER_NAME}
Run service account: ${RUN_SA}
Scheduler service account: ${SCHEDULER_SA}
Scheduler jobs created: ${CREATE_SCHEDULER_JOBS}
Scheduler time zone: ${SCHEDULER_TIME_ZONE}
Firestore indexes ensured: ${ENSURE_FIRESTORE_INDEXES}
Cloud Run timeout seconds: ${CLOUD_RUN_TIMEOUT_SECONDS}
Cloud Run resources: cpu=${CLOUD_RUN_CPU}, memory=${CLOUD_RUN_MEMORY}, min=${CLOUD_RUN_MIN_INSTANCES}, max=${CLOUD_RUN_MAX_INSTANCES}, concurrency=${CLOUD_RUN_CONCURRENCY}
Upload base URL: ${SERVICE_URL}
Upload token secret: ${UPLOAD_TOKEN_SECRET_NAME}
Callback worker mode: ${CALLBACK_WORKER_MODE}
Callback Cloud Tasks queue: ${CALLBACK_TASKS_QUEUE_NAME}
Callback queue rate/concurrency/attempts: ${CALLBACK_TASKS_MAX_DISPATCHES_PER_SECOND}/${CALLBACK_TASKS_MAX_CONCURRENT_DISPATCHES}/${CALLBACK_TASKS_MAX_ATTEMPTS}
Prewarm Cloud Tasks queue: ${PREWARM_TASKS_QUEUE_NAME}
Prewarm queue rate/concurrency/attempts: ${PREWARM_TASKS_MAX_DISPATCHES_PER_SECOND}/${PREWARM_TASKS_MAX_CONCURRENT_DISPATCHES}/${PREWARM_TASKS_MAX_ATTEMPTS}
Admin dashboard enabled: ${ADMIN_DASHBOARD_ENABLED}
Legacy chat log export scheduler paused if present: ${LEGACY_CHAT_LOG_EXPORT_JOB_NAME}
Wiki rebuild worker mode: ${WIKI_REBUILD_WORKER_MODE}
Wiki rebuild Cloud Tasks queue: ${WIKI_REBUILD_TASKS_QUEUE_NAME}
Wiki rebuild queue rate/concurrency/attempts: ${WIKI_REBUILD_TASKS_MAX_DISPATCHES_PER_SECOND}/${WIKI_REBUILD_TASKS_MAX_CONCURRENT_DISPATCHES}/${WIKI_REBUILD_TASKS_MAX_ATTEMPTS}
Cloud Tasks service account: ${CLOUD_TASKS_SERVICE_ACCOUNT_EMAIL}

Next checks:
1. curl "${SERVICE_URL}/health"
2. curl -X POST "${SERVICE_URL}/admin/sync-drive" -H "Authorization: Bearer \$(gcloud auth print-identity-token --audiences=${SERVICE_URL} --include-email)"
3. Check Firestore database: ${FIRESTORE_DATABASE_ID}
SUMMARY
