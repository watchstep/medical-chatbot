# medical-chatbot

카카오톡 챗봇에서 `kakao_user_id`를 받아 환자를 식별하고, Google Drive의 진단기록을 조회하는 PoC입니다.

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

- `credentials/google-service-account.json`이 존재해야 합니다.
- `.env` 또는 실행 환경에 `GEMINI_API_KEY`를 설정해야 Gemini File Search Store 기반 질의응답이 동작합니다.
- 카카오 AI 챗봇 callback 기능을 사용할 수 있어야 자유 질문이 5초 제한 안에서 동작합니다.
- Google Drive에 `medical-chatbot/_system/patient_index.json`이 있어야 합니다.
- 테스트 대상 환자는 `patient_index.json`에 `name`, `birth`, `folder_name`이 정확히 있어야 합니다.
- 최초 매핑 검증 전에는 테스트 대상 카카오 계정의 `kakao_user_id`가 해당 환자 `kakao_user_ids`에 없어야 합니다.

## Patient Index Sync

Google Drive `patients/` 아래에 환자 폴더는 있지만 `_system/patient_index.json`에 누락된 경우 아래 명령으로 보정할 수 있습니다.

자동 보정으로 새로 추가되는 환자 엔트리에는 `phone_last4`가 기본적으로 빈 문자열 `""`로 들어갑니다.

```bash
python -m app.tools.sync_patient_index
```

기본은 `dry-run`이며 변경 요약만 출력합니다. 실제로 Google Drive의 `patient_index.json`을 갱신하려면 아래처럼 실행합니다.

```bash
python -m app.tools.sync_patient_index --apply
```

## Document Registry Sync

Google Drive 환자 폴더의 PDF를 기준으로 `_system/document_registry.json`과 환자별 Gemini File Search Store를 동기화하려면 아래 명령을 사용합니다.

```bash
python -m app.tools.sync_document_registry
```

기본은 `dry-run`이며 결과만 출력합니다. 실제로 `document_registry.json`을 갱신하려면 아래처럼 실행합니다.

```bash
python -m app.tools.sync_document_registry --apply
```

## Sync All

관리자가 `patient_index.json` sync 후 바로 Gemini File Search Store/document registry sync까지 한 번에 실행하려면 아래 명령을 사용합니다.

```bash
python -m app.tools.sync_all --apply
```

## Kakao Verification Flow

1. 카카오톡 시작 블록 또는 인증하기 블록을 `/kakao/auth`에 연결합니다.
2. 최신 기록 메뉴 또는 기록 기반 질문 블록을 `/kakao/chat`에 연결합니다.
3. 카카오톡에서 챗봇에 일반 메시지(예: `안녕하세요`)를 보내 인증 안내가 오는지 확인합니다.
4. 같은 카카오 계정으로 `인증 {이름} {생년월일}` 형식의 메시지를 보내 인증 성공 응답이 오는지 확인합니다.
5. Google Drive의 `_system/patient_index.json`을 열어 해당 환자 `kakao_user_ids`에 실제 카카오 `userRequest.user.id`가 추가되었는지 확인합니다.
6. 같은 카카오 계정으로 `/kakao/chat` 경로에 연결된 최신 기록 메뉴 또는 `최신 기록 보여줘` 요청을 보내 재인증 없이 최신 기록 안내가 오는지 확인합니다.
7. 자유 질문은 `/kakao/chat`으로 들어오며, callbackUrl이 포함된 요청이어야 최종 Gemini 답변이 callback으로 전달됩니다.
8. 환자를 바꾸거나 재인증하려면 `인증 초기화`, `다른 환자 인증`, `환자 변경`, `재인증` 중 하나를 입력합니다.

## Logging

- `/kakao/auth`, `/kakao/chat` 진입
- patient index cache hit/miss
- patient record cache hit/miss/warm
- Gemini File Search Store sync / query
- callback enqueue / callback send success/failure

위 로그를 보면 기본 동작 여부와 Gemini 호출 경로를 빠르게 확인할 수 있습니다.

## Re-identification Check

- 로컬 서버를 재시작하거나 24시간 세션 만료 후 다시 `/kakao/auth` 경로로 진입합니다.
- 저장된 `kakao_user_id`가 있더라도 활성 세션이 없으면 재인증 안내가 나오는 것이 정상입니다.
- 재인증 후 `/kakao/chat`에서 다시 최신 기록을 조회할 수 있어야 합니다.
