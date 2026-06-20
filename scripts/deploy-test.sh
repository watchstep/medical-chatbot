#!/usr/bin/env bash
set -euo pipefail

export PROJECT_ID="medical-chatbot-494315"
export REGION="asia-northeast3"
export SERVICE_NAME="medical-chatbot-test"
export FIRESTORE_DATABASE_ID="medical-chatbot-test"

export GOOGLE_DRIVE_ROOT_FOLDER_NAME="medical-chatbot-local"

export CREATE_SCHEDULER_JOBS="true"

export ROUTER_MODE="gemini"
export CALLBACK_WORKER_MODE="cloud_tasks"
export WIKI_REBUILD_WORKER_MODE="cloud_tasks"

export GEMINI_SECRET_NAME="gemini-api-key"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${ROOT_DIR}"

read_dotenv_value() {
  local key="$1"
  if [[ ! -f ".env" ]]; then
    return
  fi
  awk -v key="${key}" '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    {
      line = $0
      sub(/^[[:space:]]*/, "", line)
      split(line, parts, "=")
      env_key = parts[1]
      sub(/[[:space:]]*$/, "", env_key)
      if (env_key == key) {
        sub(/^[^=]*=/, "", line)
        sub(/^[[:space:]]*/, "", line)
        sub(/[[:space:]]*$/, "", line)
        if ((substr(line, 1, 1) == "\"" && substr(line, length(line), 1) == "\"") ||
            (substr(line, 1, 1) == "'"'"'" && substr(line, length(line), 1) == "'"'"'")) {
          line = substr(line, 2, length(line) - 2)
        }
        print line
        exit
      }
    }
  ' .env
}

load_optional_env_from_dotenv() {
  local key="$1"
  if [[ -n "${!key:-}" ]]; then
    return
  fi
  local value
  value="$(read_dotenv_value "${key}")"
  if [[ -n "${value}" ]]; then
    export "${key}=${value}"
  fi
}

load_optional_env_from_dotenv "ADMIN_DASHBOARD_ENABLED"
load_optional_env_from_dotenv "ADMIN_DASHBOARD_USERNAME"
load_optional_env_from_dotenv "ADMIN_DASHBOARD_PASSWORD_SECRET_NAME"
load_optional_env_from_dotenv "UPLOAD_TOKEN_SECRET_NAME"
load_optional_env_from_dotenv "UPLOAD_TOKEN_TTL_MINUTES"
load_optional_env_from_dotenv "CHAT_ATTACHMENT_TTL_MINUTES"
load_optional_env_from_dotenv "MAX_UPLOAD_FILE_BYTES"
load_optional_env_from_dotenv "SCHEDULER_TIME_ZONE"
load_optional_env_from_dotenv "CLOUD_RUN_TIMEOUT_SECONDS"
load_optional_env_from_dotenv "CLOUD_RUN_MEMORY"
load_optional_env_from_dotenv "CLOUD_RUN_CPU"
load_optional_env_from_dotenv "CLOUD_RUN_MIN_INSTANCES"
load_optional_env_from_dotenv "CLOUD_RUN_MAX_INSTANCES"
load_optional_env_from_dotenv "CLOUD_RUN_CONCURRENCY"
load_optional_env_from_dotenv "CALLBACK_TASKS_MAX_DISPATCHES_PER_SECOND"
load_optional_env_from_dotenv "CALLBACK_TASKS_MAX_CONCURRENT_DISPATCHES"
load_optional_env_from_dotenv "PREWARM_TASKS_MAX_DISPATCHES_PER_SECOND"
load_optional_env_from_dotenv "PREWARM_TASKS_MAX_CONCURRENT_DISPATCHES"
load_optional_env_from_dotenv "WIKI_REBUILD_TASKS_MAX_DISPATCHES_PER_SECOND"
load_optional_env_from_dotenv "WIKI_REBUILD_TASKS_MAX_CONCURRENT_DISPATCHES"

ADMIN_DASHBOARD_PASSWORD_VALUE="${ADMIN_DASHBOARD_PASSWORD:-}"
if [[ -z "${ADMIN_DASHBOARD_PASSWORD_VALUE}" ]]; then
  ADMIN_DASHBOARD_PASSWORD_VALUE="$(read_dotenv_value "ADMIN_DASHBOARD_PASSWORD")"
fi

export ADMIN_DASHBOARD_PASSWORD_SECRET_NAME="${ADMIN_DASHBOARD_PASSWORD_SECRET_NAME:-admin-dashboard-password}"
if [[ -n "${ADMIN_DASHBOARD_PASSWORD_VALUE}" ]]; then
  export ADMIN_DASHBOARD_ENABLED="true"
  gcloud services enable secretmanager.googleapis.com --project "${PROJECT_ID}" >/dev/null
  if ! gcloud secrets describe "${ADMIN_DASHBOARD_PASSWORD_SECRET_NAME}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud secrets create "${ADMIN_DASHBOARD_PASSWORD_SECRET_NAME}" \
      --project "${PROJECT_ID}" \
      --replication-policy="automatic" >/dev/null
  fi
  printf "%s" "${ADMIN_DASHBOARD_PASSWORD_VALUE}" | gcloud secrets versions add "${ADMIN_DASHBOARD_PASSWORD_SECRET_NAME}" \
    --project "${PROJECT_ID}" \
    --data-file=- >/dev/null
  echo "Updated admin dashboard password secret: ${ADMIN_DASHBOARD_PASSWORD_SECRET_NAME}"
fi

UPLOAD_TOKEN_SECRET_VALUE="${UPLOAD_TOKEN_SECRET:-}"
if [[ -z "${UPLOAD_TOKEN_SECRET_VALUE}" ]]; then
  UPLOAD_TOKEN_SECRET_VALUE="$(read_dotenv_value "UPLOAD_TOKEN_SECRET")"
fi

export UPLOAD_TOKEN_SECRET_NAME="${UPLOAD_TOKEN_SECRET_NAME:-upload-token-secret-test}"
if [[ -n "${UPLOAD_TOKEN_SECRET_VALUE}" ]]; then
  gcloud services enable secretmanager.googleapis.com --project "${PROJECT_ID}" >/dev/null
  if ! gcloud secrets describe "${UPLOAD_TOKEN_SECRET_NAME}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud secrets create "${UPLOAD_TOKEN_SECRET_NAME}" \
      --project "${PROJECT_ID}" \
      --replication-policy="automatic" >/dev/null
  fi
  printf "%s" "${UPLOAD_TOKEN_SECRET_VALUE}" | gcloud secrets versions add "${UPLOAD_TOKEN_SECRET_NAME}" \
    --project "${PROJECT_ID}" \
    --data-file=- >/dev/null
  echo "Updated upload token secret: ${UPLOAD_TOKEN_SECRET_NAME}"
fi

exec ./deploy-test.sh
