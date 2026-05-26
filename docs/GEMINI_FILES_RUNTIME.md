# Gemini Files Runtime

## Gemini Files API runtime cache cleanup 정책

Gemini Files API에 업로드된 파일은 Google Drive 원본을 대체하는 저장소가 아니다. Files API 파일은 최종 QA 호출을 빠르게 수행하기 위한 runtime cache로만 사용한다. 원본 source of truth는 항상 Google Drive다.

cleanup의 목적은 응답 속도 향상이 아니라 Files API 저장 한도 초과와 신규 upload 실패를 예방하는 것이다. 저장 용량 사용률이 높아진다고 점진적으로 느려진다고 가정하지 않는다. 운영상 위험은 quota 초과로 새 upload 또는 pre-warm이 실패하는 것이다.

권장 설정:

```text
GEMINI_FILES_STORAGE_SOFT_LIMIT_BYTES=15032385536
GEMINI_FILES_STORAGE_TARGET_BYTES=12884901888
GEMINI_FILES_STORAGE_HARD_LIMIT_BYTES=18253611008
GEMINI_FILES_CLEANUP_BATCH_SIZE=20
```

용량 기준 의미:

```text
soft limit
= cleanup 시작 기준. 기본 14GB

target bytes
= cleanup 종료 목표. 기본 12GB

hard limit
= best-effort 작업인 pre-warm을 중단하고 cleanup을 우선해야 하는 기준. 기본 17GB

batch size
= 한 번의 cleanup에서 삭제를 시도할 최대 파일 수
```

운영 정책:

```text
usage < soft limit
→ cleanup 실행하지 않음

usage >= soft limit
→ cleanup 시작
→ 오래된 runtime cache부터 삭제
→ usage <= target bytes 또는 cleanup batch size 도달 시 중단

usage >= hard limit
→ pre-warm은 skip 또는 cleanup task enqueue
→ 실제 질문의 lazy upload는 cleanup을 먼저 시도한 뒤 필요 시 진행
```

삭제 우선순위:

```text
1. source가 없거나 비활성화된 Gemini file
2. EXPIRED 상태의 Gemini file
3. FAILED 상태의 Gemini file
4. source snapshot이 현재 Google Drive source_ref와 불일치하는 Gemini file
5. last_checked_at이 오래된 ACTIVE Gemini file
6. uploaded_at이 오래된 ACTIVE Gemini file
```

주의:

- `UPLOADING` 또는 `PROCESSING` 상태의 file은 일반 cleanup에서 삭제하지 않는다. 진행 중인 upload 또는 polling을 방해할 수 있다.
- cleanup은 `medical_source_runtime.gemini_file` cache 상태만 비운다. `medical_sources`, `medical_wiki_pages`, `medical_wiki_index`는 삭제하지 않는다.
- cleanup 후에도 `runtime.sync.status`와 `runtime.wiki_sync.status`가 READY라면 Router 후보로 남을 수 있다. 실제 질문 시 필요한 경우 기존 lazy `prepare_file()`이 다시 upload한다.
- cleanup 실패는 사용자에게 노출하지 않는다. 로그와 운영 결과 payload에만 기록한다.
- Drive 원본 파일은 cleanup 대상이 아니다.

## Gemini Files API pre-warm 정책

pre-warm은 사용자가 질문하기 전에 가장 가능성이 높은 원본 source를 Gemini Files API에 미리 준비하여 첫 질문 지연을 줄이는 best-effort 최적화다. pre-warm은 최종 QA의 필수 경로가 아니며, 실패해도 실제 질문은 기존 lazy `prepare_file()` 흐름으로 처리되어야 한다.

트리거:

```text
인증 성공
→ 인증 성공 메시지는 즉시 반환
→ latest source pre-warm job 생성
→ Cloud Tasks enqueue

이미 인증된 시작 블록 진입
→ 안내 메시지는 즉시 반환
→ latest source pre-warm job 생성
→ Cloud Tasks enqueue

의료 기록 조회
→ 의료 기록 안내 메시지는 즉시 반환
→ latest source pre-warm job 생성
→ Cloud Tasks enqueue
```

중요 원칙:

- pre-warm enqueue 실패가 인증 성공, 시작 블록 안내, 의료 기록 조회 응답을 실패시키면 안 된다.
- 사용자 응답은 즉시 반환하고, 파일 upload와 ACTIVE polling은 Cloud Tasks worker에서 처리한다.
- pre-warm worker는 새로운 upload 로직을 만들지 않고 기존 `GeminiFilesQaService.prepare_file()`을 재사용한다.
- pre-warm이 완료되면 실제 질문에서 `prepare_file()`은 ACTIVE file을 재사용한다.
- pre-warm이 실패했거나 아직 완료되지 않았으면 실제 질문에서 기존 lazy upload 흐름으로 fallback한다.

source 선택 기준:

```text
1. 현재 patient의 medical_wiki_index/main을 조회한다.
2. needs_review == false인 page만 후보로 둔다.
3. source_id가 비어 있지 않아야 한다.
4. medical_sources/{source_id}.source_status == ACTIVE 이어야 한다.
5. medical_source_runtime/{source_id}.sync.status == READY 이어야 한다.
6. medical_source_runtime/{source_id}.wiki_sync.status == READY 이어야 한다.
7. date가 있는 후보 중 가장 최신 page를 우선한다.
8. date가 없으면 confidence가 가장 높은 page를 우선한다.
9. 그래도 동률이면 안정적인 정렬 기준으로 하나만 선택한다.
```

MVP에서는 환자별 pre-warm source를 1개로 제한한다. 다중 source QA 또는 비교 질문은 2차 이후에 별도 설계한다.

Cloud Tasks worker 흐름:

```text
/admin/prewarm-gemini-file 호출
→ admin auth 또는 Cloud Tasks OIDC 검증
→ prewarm_job_id로 gemini_file_prewarm_jobs/{job_id} 조회
→ Firestore lock 획득
→ job 만료, 완료, 중복 처리 여부 확인
→ latest source 또는 지정 source 확인
→ source 소유권, source_status, runtime readiness 확인
→ ACTIVE이고 만료 여유가 있으며 source snapshot이 일치하면 SKIPPED 처리
→ Files API usage가 hard limit 이상이면 cleanup 우선 또는 SKIPPED 처리
→ 필요한 경우 cleanup_if_needed 실행
→ GeminiFilesQaService.prepare_file(patient_id, source_id) 실행
→ 성공 시 DONE 기록
→ 실패 시 retry 가능하면 FAILED 상태로 runnable 유지
→ 최대 재시도 초과 또는 만료 시 FAILED 최종 처리
```

skip 조건:

```text
runtime.gemini_file.state == ACTIVE
+ expiration_time이 file_expiration_margin_seconds 이상 남아 있음
+ source_drive_modified_at, source_file_hash, source_file_size_bytes가 현재 source_ref와 일치함

→ upload하지 않고 SKIPPED 처리
```

idempotency 정책:

```text
idempotency_key = prewarm:{patient_id}:{source_id}:{hour_bucket}
```

동일 key의 `PENDING`, `PROCESSING`, `DONE`, `SKIPPED` job이 최근에 있으면 새 job을 만들지 않는다. `FAILED` job은 retry 정책과 만료 시간을 고려해 재생성할 수 있다. Cloud Tasks task name도 동일 key 기반으로 deterministic하게 생성하여 중복 enqueue를 줄인다.

실패 처리:

- pre-warm 실패는 사용자에게 노출하지 않는다.
- 실패 원인은 `gemini_file_prewarm_jobs.last_failure`와 Cloud Logging에 최소 정보로 기록한다.
- callback job이나 chat log를 실패시키지 않는다.
- 실제 질문 시 기존 lazy `prepare_file()`이 다시 파일 준비를 시도한다.

보안 주의:

- pre-warm job payload에는 가능하면 `prewarm_job_id`만 담는다.
- 카카오 응답에는 pre-warm 여부, source_id, Drive ID, Gemini file_name, file_uri를 노출하지 않는다.
- Cloud Logging에는 원본 파일명, Drive ID, Gemini file URI, 사용자 질문 원문을 출력하지 않는다.

## 8. gemini_file_prewarm_jobs

경로:

```text
gemini_file_prewarm_jobs/{job_id}
```

목적:

- 인증 성공, 이미 인증된 시작 블록, 의료 기록 조회 이후 실행되는 Gemini Files API pre-warm 작업 상태를 관리한다.
- Cloud Tasks enqueue와 worker 실행을 추적한다.
- 중복 pre-warm을 idempotency key로 제어한다.
- lock, lease, retry, skip, 실패 정보를 저장한다.
- 사용자 응답 본문, 의료 문서 내용, source_ref, Drive ID, Gemini file URI는 저장하지 않는다.

예시:

```json
{
  "job_id": "PREWARM_A42F19C7D0E3",
  "patient_id": "P0001",
  "source_id": "SRC_P0001_A8F39C21D4B2",
  "reason": "auth_success",
  "idempotency_key": "prewarm:P0001:SRC_P0001_A8F39C21D4B2:2026051321",
  "status": "PENDING",
  "runnable": true,
  "retry_count": 0,
  "max_attempts": 3,
  "next_run_at": "",
  "lock_owner": null,
  "lease_expires_at": null,
  "skip_reason": "",
  "last_failure": null,
  "created_at": "2026-05-13T21:00:00+09:00",
  "updated_at": "2026-05-13T21:00:00+09:00",
  "started_at": "",
  "finished_at": ""
}
```

권장 `status`:

```text
PENDING
PROCESSING
DONE
SKIPPED
FAILED
EXPIRED
```

권장 `reason`:

```text
auth_success
authenticated_start
medical_record_lookup
manual
```

주의:

- pre-warm job은 사용자에게 직접 노출하지 않는다.
- `source_id`는 내부 추적용으로만 저장한다. 카카오톡 응답에는 노출하지 않는다.
- `SKIPPED`는 실패가 아니다. 이미 ACTIVE cache가 충분히 유효하거나 hard limit 방어 정책으로 pre-warm을 하지 않은 상태다.
- pre-warm이 `FAILED`여도 실제 질문 처리는 기존 lazy upload로 진행한다.
- job TTL 또는 주기적 삭제 정책을 둬서 오래된 pre-warm job을 정리한다.
