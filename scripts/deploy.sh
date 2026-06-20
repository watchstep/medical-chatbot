#!/usr/bin/env bash
set -euo pipefail

export PROJECT_ID="${PROJECT_ID:-medical-chatbot-498909}"
export REGION="${REGION:-asia-northeast3}"
export SERVICE_NAME="${SERVICE_NAME:-medical-chatbot}"
export FIRESTORE_DATABASE_ID="${FIRESTORE_DATABASE_ID:-medical-chatbot}"
export APP_ENV="${APP_ENV:-prod}"
export APP_NAME="${APP_NAME:-medical-chatbot}"

export GOOGLE_DRIVE_ROOT_FOLDER_NAME="${GOOGLE_DRIVE_ROOT_FOLDER_NAME:-medical-chatbot}"

export ROUTER_MODE="${ROUTER_MODE:-gemini}"
export CALLBACK_WORKER_MODE="${CALLBACK_WORKER_MODE:-cloud_tasks}"
export WIKI_REBUILD_WORKER_MODE="${WIKI_REBUILD_WORKER_MODE:-cloud_tasks}"

export GEMINI_SECRET_NAME="${GEMINI_SECRET_NAME:-gemini-api-key}"
export UPLOAD_TOKEN_SECRET_NAME="${UPLOAD_TOKEN_SECRET_NAME:-upload-token-secret}"
export ADMIN_DASHBOARD_PASSWORD_SECRET_NAME="${ADMIN_DASHBOARD_PASSWORD_SECRET_NAME:-admin-dashboard-password}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${ROOT_DIR}"

echo "[deploy-wrapper] target: ${PROJECT_ID} / ${REGION} / ${SERVICE_NAME}"
echo "[deploy-wrapper] firestore database: ${FIRESTORE_DATABASE_ID}"

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

is_placeholder_secret() {
  local value="$1"
  case "${value}" in
    "" | replace-with-* | "replace_me" | "changeme" | "CHANGE_ME")
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

upsert_secret_value_if_present() {
  local secret_name="$1"
  local secret_value="$2"
  local label="$3"

  if is_placeholder_secret "${secret_value}"; then
    return
  fi

  gcloud services enable secretmanager.googleapis.com --project "${PROJECT_ID}" >/dev/null
  if ! gcloud secrets describe "${secret_name}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    gcloud secrets create "${secret_name}" \
      --project "${PROJECT_ID}" \
      --replication-policy="automatic" >/dev/null
  fi
  printf "%s" "${secret_value}" | gcloud secrets versions add "${secret_name}" \
    --project "${PROJECT_ID}" \
    --data-file=- >/dev/null
  echo "Updated ${label} secret: ${secret_name}"
}

load_optional_env_from_dotenv "GOOGLE_DRIVE_ROOT_FOLDER_ID"
load_optional_env_from_dotenv "GOOGLE_DRIVE_ROOT_FOLDER_NAME"
load_optional_env_from_dotenv "PATIENTS_FOLDER_NAME"
load_optional_env_from_dotenv "SYSTEM_FOLDER_NAME"
load_optional_env_from_dotenv "ADMIN_DASHBOARD_ENABLED"
load_optional_env_from_dotenv "ADMIN_DASHBOARD_USERNAME"
load_optional_env_from_dotenv "ADMIN_DASHBOARD_PASSWORD_SECRET_NAME"
load_optional_env_from_dotenv "GEMINI_SECRET_NAME"
load_optional_env_from_dotenv "UPLOAD_TOKEN_SECRET_NAME"
load_optional_env_from_dotenv "UPLOAD_TOKEN_TTL_MINUTES"
load_optional_env_from_dotenv "CHAT_ATTACHMENT_TTL_MINUTES"
load_optional_env_from_dotenv "MAX_UPLOAD_FILE_BYTES"
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

echo "[deploy-wrapper] updating Secret Manager values if provided"

GEMINI_API_KEY_VALUE="${GEMINI_API_KEY:-}"
if [[ -z "${GEMINI_API_KEY_VALUE}" ]]; then
  GEMINI_API_KEY_VALUE="$(read_dotenv_value "GEMINI_API_KEY")"
fi
upsert_secret_value_if_present "${GEMINI_SECRET_NAME}" "${GEMINI_API_KEY_VALUE}" "Gemini API key"

UPLOAD_TOKEN_SECRET_VALUE="${UPLOAD_TOKEN_SECRET:-}"
if [[ -z "${UPLOAD_TOKEN_SECRET_VALUE}" ]]; then
  UPLOAD_TOKEN_SECRET_VALUE="$(read_dotenv_value "UPLOAD_TOKEN_SECRET")"
fi
upsert_secret_value_if_present "${UPLOAD_TOKEN_SECRET_NAME}" "${UPLOAD_TOKEN_SECRET_VALUE}" "upload token"

ADMIN_DASHBOARD_PASSWORD_VALUE="${ADMIN_DASHBOARD_PASSWORD:-}"
if [[ -z "${ADMIN_DASHBOARD_PASSWORD_VALUE}" ]]; then
  ADMIN_DASHBOARD_PASSWORD_VALUE="$(read_dotenv_value "ADMIN_DASHBOARD_PASSWORD")"
fi

if ! is_placeholder_secret "${ADMIN_DASHBOARD_PASSWORD_VALUE}"; then
  export ADMIN_DASHBOARD_ENABLED="true"
  upsert_secret_value_if_present \
    "${ADMIN_DASHBOARD_PASSWORD_SECRET_NAME}" \
    "${ADMIN_DASHBOARD_PASSWORD_VALUE}" \
    "admin dashboard password"
fi

if [[ -z "${GOOGLE_DRIVE_ROOT_FOLDER_ID:-}" ]]; then
  echo "WARNING: GOOGLE_DRIVE_ROOT_FOLDER_ID is empty." >&2
  echo "The app will try to find the Drive root by GOOGLE_DRIVE_ROOT_FOLDER_NAME=${GOOGLE_DRIVE_ROOT_FOLDER_NAME}." >&2
  echo "For production, setting GOOGLE_DRIVE_ROOT_FOLDER_ID is safer." >&2
else
  echo "[deploy-wrapper] Google Drive root folder ID: <set>"
fi

echo "[deploy-wrapper] running ./deploy.sh"

exec ./deploy.sh
