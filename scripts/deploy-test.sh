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
exec ./deploy-test.sh