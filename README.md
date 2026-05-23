# medical-chatbot

카카오톡 챗봇에서 `kakao_user_id`를 해시 처리해 환자를 인증하고, Firestore Medical Wiki catalog로 Google Drive 원본 의료 문서를 선택한 뒤 Gemini Files API whole-document QA를 수행하는 PoC입니다.

## Run Locally

```bash
uvicorn app.main:app --reload --port 8000
```

```bash
ngrok http 8000
```

카카오 오픈빌더 Skill URL:

```text
https://{ngrok-domain}/kakao/auth
https://{ngrok-domain}/kakao/chat
```

## Required Setup

- Cloud Run에서는 배포된 런타임 서비스 계정에 Google Drive 폴더 접근 권한을 공유해야 합니다.
- Cloud Run 런타임 서비스 계정에 Firestore 접근 권한을 부여해야 합니다.
- 로컬에서 서비스 계정 JSON 파일을 직접 쓰려면 `.env`에 `GOOGLE_SERVICE_ACCOUNT_PATH=credentials/google-service-account.json`를 설정합니다. 미설정 시 Application Default Credentials를 사용합니다.
- `.env` 또는 실행 환경에 `GEMINI_API_KEY`를 설정해야 Gemini Files API 기반 질의응답이 동작합니다.
- `/admin/*` 엔드포인트를 쓰려면 `ADMIN_SYNC_TOKEN`을 설정하고 요청에 `X-Admin-Token` 헤더를 넣어야 합니다. 토큰 미설정 시 admin endpoint는 닫힙니다.
- 카카오 AI 챗봇 callback 기능을 사용할 수 있어야 자유 질문이 5초 제한 안에서 동작합니다.
- Firestore `patients/{patient_id}` 문서에 `name`, `birth`, `drive_folder_id`, `status=active`가 있어야 인증과 sync가 동작합니다.
- Google Drive는 환자별 원본 파일 저장소로만 사용하며, 런타임 운영 DB로 `_system/patient_index.json` 또는 `document_registry.json`을 사용하지 않습니다.

## Firestore Runtime

런타임은 Firestore 기반 Medical Wiki + Gemini Files API 경로만 사용합니다.

Firestore 주요 경로:

```text
patients/{patient_id}
kakao_user_map/{kakao_user_id_hash}
drive_sync_state/{scope_id}
drive_folder_index/{drive_folder_id}
drive_file_index/{drive_file_id}
patients/{patient_id}/medical_sources/{source_id}
patients/{patient_id}/medical_wiki_pages/{page_id}
patients/{patient_id}/medical_wiki_index/main
patients/{patient_id}/medical_source_runtime/{source_id}
patients/{patient_id}/chat_sessions/{kakao_user_id_hash}
patients/{patient_id}/chat_logs/{log_id}
kakao_callback_jobs/{job_id}
```

Admin sync endpoints:

```text
POST /admin/sync-drive-changes
POST /admin/sync-drive
POST /admin/sync-drive/patient/{patient_id}
POST /admin/rebuild-wiki-page/{patient_id}/{source_id}
POST /admin/recompile-wiki-index/{patient_id}
```

## Markdown Parsing PoC

Firestore 전환 전에 Google Drive 원본 PDF를 Gemini로 직접 파싱해 `parsed.md` 품질을 확인할 수 있습니다.

```bash
python -m app.tools.parse_drive_document \
  --drive-file-id {google-drive-file-id-or-url} \
  --out artifacts/parsing/{google-drive-file-id}/parsed.md
```

`--out`을 생략하면 `artifacts/parsing/{drive_file_id}/parsed.md`에 저장됩니다. 출력 요약에는 글자 수, Markdown table 행 수, 페이지 마커 수가 포함됩니다.

```bash
python -m app.tools.parse_drive_document \
  --drive-file-id {google-drive-file-id-or-url} \
  --json
```

파싱 결과는 의료 원문을 포함할 수 있으므로 `artifacts/`는 git에 포함하지 않습니다.

## Kakao Verification Flow

1. 카카오톡 시작 블록 또는 인증하기 블록을 `/kakao/auth`에 연결합니다.
2. 최신 기록 메뉴 또는 기록 기반 질문 블록을 `/kakao/chat`에 연결합니다.
3. 카카오톡에서 챗봇에 일반 메시지(예: `안녕하세요`)를 보내 인증 안내가 오는지 확인합니다.
4. 같은 카카오 계정으로 `인증 {이름} {생년월일}` 형식의 메시지를 보내 인증 성공 응답이 오는지 확인합니다.
5. Firestore `kakao_user_map/{sha256...}`에 인증 매핑이 생성되었는지 확인합니다.
6. 같은 카카오 계정으로 `/kakao/chat` 경로에 연결된 최신 기록 메뉴 또는 `최신 기록 보여줘` 요청을 보내 재인증 없이 최신 기록 안내가 오는지 확인합니다.
7. 자유 질문은 `/kakao/chat`으로 들어오며, callbackUrl이 포함된 요청이어야 최종 Gemini 답변이 callback으로 전달됩니다.
8. 환자를 바꾸거나 재인증하려면 `인증 초기화`, `다른 환자 인증`, `환자 변경`, `재인증` 중 하나를 입력합니다.

## Logging

- `/kakao/auth`, `/kakao/chat` 진입
- Firestore auth/session/chat log/callback job 처리
- Drive Changes API sync
- Medical Wiki page/index 생성
- Gemini Files API upload/reuse 및 whole-document QA
- callback enqueue / callback send success/failure

위 로그를 보면 기본 동작 여부와 Gemini 호출 경로를 빠르게 확인할 수 있습니다.

## Re-identification Check

- 로컬 서버를 재시작하거나 24시간 세션 만료 후 다시 `/kakao/auth` 경로로 진입합니다.
- 저장된 `kakao_user_id`가 있더라도 활성 세션이 없으면 재인증 안내가 나오는 것이 정상입니다.
- 재인증 후 `/kakao/chat`에서 다시 최신 기록을 조회할 수 있어야 합니다.
