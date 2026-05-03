#!/bin/bash
set -e

PROJECT_ID="medical-chatbot-494315"
REGION="asia-northeast3"
SERVICE_NAME="medical-chatbot"
RUN_SA="medical-chatbot-run@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"

gcloud run deploy "$SERVICE_NAME" \
  --quiet \
  --source . \
  --region "$REGION" \
  --allow-unauthenticated \
  --service-account "$RUN_SA" \
  --timeout 300 \
  --memory 1Gi \
  --cpu 1 \
  --set-env-vars GOOGLE_SERVICE_ACCOUNT_PATH=/credentials/google-service-account.json \
  --set-env-vars DRIVE_ROOT_FOLDER_NAME=medical-chatbot \
  --set-env-vars DRIVE_SYSTEM_FOLDER_NAME=_system \
  --set-env-vars PATIENT_INDEX_FILE_NAME=patient_index.json \
  --set-env-vars DOCUMENT_REGISTRY_FILE_NAME=document_registry.json \
  --set-env-vars GEMINI_MODEL=gemini-3-flash-preview \
  --set-env-vars GEMINI_TEMPERATURE=0.1 \
  --set-env-vars GEMINI_MAX_OUTPUT_TOKENS=2048 \
  --set-env-vars GEMINI_FILE_SEARCH_TOP_K=7 \
  --set-env-vars GEMINI_THINKING_LEVEL=low \
  --set-env-vars GEMINI_FILE_SEARCH_LOG_RETRIEVAL=false \
  --set-env-vars LOG_LEVEL=INFO\
  --set-secrets GEMINI_API_KEY=gemini-api-key:latest,/credentials/google-service-account.json=google-service-account-json:latest