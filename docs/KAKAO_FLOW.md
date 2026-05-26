# Kakao Flow

## 인증 정책

### `/kakao/auth` 처리 순서

```text
kakao_user_id 추출
→ kakao_user_id_hash 생성
→ 사용자 메시지 추출
→ 인증 초기화 명령인지 확인

인증 초기화 명령인 경우:
    → kakao_user_map/{kakao_user_id_hash} 삭제 또는 inactive 처리
    → patients/{patient_id}/chat_sessions/{kakao_user_id_hash} 삭제 또는 만료 처리
    → 이름 + 생년월일 입력 안내 반환

일반 인증 요청인 경우:
    → kakao_user_map/{kakao_user_id_hash} 조회
    → 매핑이 있고 expires_at이 유효하면 인증 완료 안내 반환
    → 매핑이 없거나 만료되었으면 이름 + 생년월일 입력 안내 반환

이름 + 생년월일 입력인 경우:
    → Firestore patients 컬렉션에서 name, birth로 환자 확인
    → 환자가 없으면 미등록 안내 반환
    → 환자가 있으면 kakao_user_map/{kakao_user_id_hash}에 patient_id 저장
    → chat_session 초기화
    → pre-warm job 생성과 Cloud Tasks enqueue를 best-effort로 시도
    → 인증 완료 및 /kakao/chat 사용 안내 반환
```

주의:

- 인증 실패 시 어떤 항목이 틀렸는지 구체적으로 알려주지 않는다.
- `kakao_user_id`는 원문으로 저장하지 않고 hash로 저장한다.
- 인증 전에는 Google Drive 원본 파일, Firestore medical_sources, medical_wiki_pages, medical_source_runtime, Gemini API를 조회하지 않는다.
- 인증 성공 후 pre-warm enqueue가 실패해도 인증 성공 응답은 정상 반환한다.

## 카카오 Callback 응답 정책

`/kakao/chat`에서 Gemini Router, Gemini Files API 준비, Gemini Final QA가 필요한 경우에는 카카오 OpenBuilder 5초 제한을 넘길 수 있으므로 callback 기반 비동기 응답을 기본 경로로 사용한다.

```text
/kakao/chat 요청 수신
→ 인증과 patient 상태 확인
→ log_id 생성
→ patients/{patient_id}/chat_logs/{log_id} 생성, job_id는 null 허용
→ callback_url 확인
→ job_id 생성
→ kakao_callback_jobs/{job_id} 생성, chat_log_id 연결
→ patients/{patient_id}/chat_logs/{log_id}.job_id 갱신
→ 5초 이내 processing 메시지 즉시 반환
→ 백그라운드 callback job에서 Router, Files API 준비, Final QA 수행
→ 최종 메시지를 callback_url로 전송
→ callback 전송 성공 후 assistant 답변을 role="assistant" chat log로 저장
```

즉시 응답 경로는 아래 경우에만 사용한다.

```text
인증이 없거나 만료된 경우
인증 안내 또는 인증 초기화 응답이 필요한 경우
drive_sync_state.sync_status == BOOTSTRAP_REQUIRED 인 경우
현재 환자의 medical_wiki_index 또는 usable wiki page가 아직 준비되지 않아 Router catalog를 구성할 수 없는 경우
callback_url이 없거나 callback job을 만들 수 없는 경우
의료 문서 QA를 시작하기 전에 안전하게 종료해야 하는 경우
```

Gemini 응답이 필요한 경우의 즉시 반환 메시지 예시:

```text
🩺 문서를 확인하고 있습니다.

의료 기록을 살펴본 뒤 답변드릴게요.
```

시스템 준비 중 메시지 예시:

```text
관련 문서를 찾아 준비 중입니다.\n잠시 후 다시 시도해 주세요.🙇‍♂️
```

## `/kakao/chat` 처리 흐름

### 요청 수신 및 즉시 응답 흐름

```text
kakao_user_id 추출
→ kakao_user_id_hash 생성
→ 사용자 질문 추출
→ Firestore kakao_user_map에서 인증 상태 확인
→ 인증이 없거나 만료되었으면 인증 필요 안내 즉시 반환
→ patient_id 확정
→ patients/{patient_id} 조회
→ log_id 생성
→ patients/{patient_id}/chat_logs/{log_id} 생성, job_id = null
→ 의료 기록 조회 intent이면 의료 기록 안내를 assistant/system chat log로 저장하고 즉시 반환하며 pre-warm job 생성을 best-effort로 시도
→ drive_sync_state.sync_status == BOOTSTRAP_REQUIRED 이면 시스템 준비 중 메시지 즉시 반환
→ callback_url 확인
→ job_id 생성 전후로 Router가 사용할 medical_wiki_index/main을 조회할 수 있도록 준비
→ usable page entry가 비어 있어도 차단 intent와 source 없는 OK 답변은 처리 가능
→ catalog 자체가 준비되지 않아 Router가 source 선택을 시도할 수 없으면 시스템 준비 중 메시지로 안전 처리
→ job_id 생성
→ kakao_callback_jobs/{job_id} 생성, chat_log_id = log_id
→ patients/{patient_id}/chat_logs/{log_id}.job_id 갱신
→ processing 메시지 즉시 반환
```

중요:

- `chat_logs`는 callback job 완료 후가 아니라 인증된 `/kakao/chat` 요청 수신 직후 저장한다.
- callback 전송에 성공한 최종 assistant 답변은 `ANSWER_{job_id}` chat log로 저장한다.
- callback 전송 실패 또는 retry 예정 상태에서는 assistant 답변을 전송 완료로 저장하지 않는다.
- 의료 기록 조회 안내, callback 불가 안내처럼 인증된 `/kakao/chat` 요청에 대한 즉시 응답은 assistant/system chat log로 저장한다.
- callback job을 만들 수 없는 즉시 응답 경로에서는 `chat_logs.job_id`가 `null`일 수 있다.
- Router catalog 구성 시점에는 `medical_wiki_index`와 필요한 `medical_wiki_pages`만 조회한다.
- Router에는 `medical_sources.source_ref`, `medical_source_runtime`, `drive_file_index`, `drive_folder_index`를 전달하지 않는다.
- Router는 원본 파일 내용을 보지 않고, 최종 QA에 넘겨야 할 원본 파일의 `source_id`만 선택한다.
- `/kakao/chat` 요청 스레드에서는 Gemini Final QA 완료를 기다리지 않는다.

### Callback job 내부 처리 흐름

```text
job_id 기준 callback job 시작
→ kakao_callback_jobs/{job_id}.status = PROCESSING
→ chat_sessions에서 최근 3턴 조회
→ 최근 사용자 질문 1~2개를 prior_context로 요약
→ Router에는 현재 질문, prior_context, medical_wiki_index, 필요한 source_summary page만 전달
→ Gemini Router가 intent와 selection_status, 필요한 경우 primary_source_id를 반환
→ Router intent가 EMERGENCY, PRIVACY_BLOCK, COST_BLOCK, OUT_OF_SCOPE이면 Final QA를 호출하지 않음
→ 백엔드가 intent를 status로 매핑해 고정 메시지를 callback으로 전송
→ Router intent가 OK이면 Final QA 대상으로 처리
→ OK + selected이면 백엔드가 primary_source_id가 현재 patient_id의 medical_sources에 존재하는지 검증
→ 백엔드가 source_status, runtime.sync.status, runtime.wiki_sync.status 확인
→ medical_source_runtime.gemini_file.state, expiration_time 확인
→ medical_sources.source_ref.drive_modified_at, file_hash, file_size_bytes 확인
→ ACTIVE이고 만료 전이며 원본이 변경되지 않았으면 기존 Gemini file 재사용
→ NONE, EXPIRED, FAILED이면 Firestore lock 획득 후 Google Drive 원본에서 lazy upload
→ UPLOADING 또는 PROCESSING이면 제한 시간 동안 ACTIVE 전환 확인
→ 제한 시간 내 ACTIVE가 아니면 Final QA를 호출하지 않고 문서 준비 중 메시지를 callback으로 전송
→ selected source 검증 또는 prepare_file() 실패 시 prepared_file=None으로 fallback하지 않고 기존 안전 실패 흐름을 따름
→ ACTIVE file object로 prepared_file 구성
→ answer_question(prepared_file=prepared_file, intent="OK") 호출
→ OK + insufficient이면 문서 준비 없이 answer_question(prepared_file=None, intent="OK") 호출
→ Gemini Final QA에 현재 질문, prior_context, intent="OK", prepared_file이 있는 경우 선택된 원본 파일 전체를 전달
→ Gemini가 status, kakaotalk_render, used_source_ids JSON 생성
→ 백엔드가 JSON 파싱, schema 검증, source 검증
→ Gemini가 작성한 kakaotalk_render에는 출처 섹션이 없어야 함
→ source 없이 호출된 응답은 used_source_ids가 비어 있어야 함
→ source와 함께 호출된 응답의 used_source_ids는 비어 있거나 전달 source_id만 참조해야 함
→ status == ok이면 검증된 kakaotalk_render를 전송하고, used_source_ids가 있으면 백엔드가 문서 표시명만 있는 출처 섹션 append
→ status != ok이면 백엔드 고정 메시지 또는 안전 실패 메시지 전송
→ callback_url로 최종 메시지 전송
→ 전송 성공 후 role="assistant", message_type="answer" 또는 "system" chat log 저장
→ chat_sessions 최근 5턴 갱신
→ kakao_callback_jobs/{job_id}.status = CALLBACK_SENT
→ 실패 시 kakao_callback_jobs/{job_id}.status = FAILED, last_failure 기록
```

### bootstrap 또는 catalog 미준비 처리

아래 상태에서는 Gemini를 호출하지 않는다.

```text
drive_sync_state.sync_status == BOOTSTRAP_REQUIRED
또는 현재 patient의 medical_wiki_index가 아직 준비되지 않음
또는 선택 가능한 READY source가 없음
```

사용자 응답:

```text
관련 문서를 찾아 준비 중입니다.\n잠시 후 다시 시도해 주세요.🙇‍♂️
```

### UPLOADING / PROCESSING 상태 처리

선택 source의 `gemini_file.state`가 `UPLOADING` 또는 `PROCESSING`이면, callback job은 아래 정책을 따른다.

```text
1. medical_source_runtime lock과 gemini_file 상태 확인
2. FILE_READY_WAIT_SECONDS 동안 상태를 확인한다.
3. 권장값은 30초, 확인 간격은 2~5초다.
4. 제한 시간 내 ACTIVE가 되면 Final QA를 계속 진행한다.
5. 제한 시간 내 ACTIVE가 되지 않으면 최종 QA를 호출하지 않는다.
6. callback으로 문서 준비 중 안내 메시지를 전송한다.
```
