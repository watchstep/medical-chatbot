# Firestore Schema

## Firestore 데이터 모델

Firestore에서는 source registry, wiki page, wiki index, runtime 상태를 같은 `source_id`로 연결하되 별도 collection으로 분리하여 저장한다.

전체 구조:

```text
patients/{patient_id}
kakao_user_map/{kakao_user_id_hash}

drive_sync_state/{scope_id}
drive_folder_index/{drive_folder_id}
drive_file_index/{drive_file_id}

patients/{patient_id}/medical_sources/{source_id}
patients/{patient_id}/medical_wiki_pages/{page_id}
patients/{patient_id}/medical_wiki_index/main
patients/{patient_id}/medical_wiki_logs/{log_id}
patients/{patient_id}/medical_source_runtime/{source_id}

patients/{patient_id}/chat_sessions/{kakao_user_id_hash}
patients/{patient_id}/chat_logs/{log_id}
patients/{patient_id}/active_attachments/{kakao_user_id_hash}
upload_tokens/{token_id}
kakao_callback_jobs/{job_id}
gemini_file_prewarm_jobs/{job_id}
```

핵심 분리 원칙:

```text
medical_sources/{source_id}
= Google Drive 원본 파일 registry와 source lifecycle

medical_wiki_pages/{page_id}
= Router가 source를 선택하기 위한 source_summary page

medical_wiki_index/main
= Router가 먼저 읽는 index.md 역할의 compact catalog

medical_wiki_logs/{log_id}
= wiki page와 index 변경 이벤트 기록

medical_source_runtime/{source_id}
= Gemini Files API 상태, sync 상태, lock, lease, retry, 실패 정보

chat_sessions/{kakao_user_id_hash}
= 최근 대화 문맥을 유지하기 위한 짧은 세션 상태

chat_logs/{log_id}
= 인증된 사용자가 `/kakao/chat`에 입력한 원문 메시지 로그

active_attachments/{kakao_user_id_hash}
= 인증된 카카오 세션의 최근 임시 업로드 파일 1개. Google Drive source와 섞지 않음

upload_tokens/{token_id}
= `/upload/{token}` 접근을 위한 stateful one-use token 상태

kakao_callback_jobs/{job_id}
= 카카오 callback 기반 비동기 응답 작업 상태, retry, 실패 정보

gemini_file_prewarm_jobs/{job_id}
= Gemini Files API pre-warm 작업 상태, idempotency, lock, retry, skip 또는 실패 정보

drive_sync_state/{scope_id}
= Changes API pageToken, 전역 sync lock, 전체 스캔 상태 관리

drive_folder_index/{drive_folder_id}
= Drive folder_id와 patient_id 매핑

drive_file_index/{drive_file_id}
= Drive file_id와 patient_id, source_id 매핑
```

금지:

- `medical_sources/{source_id}` 안에 wiki page와 runtime을 모두 섞어 저장하지 않는다.
- `medical_wiki_pages`에 Gemini file URI, lock, retry, Drive ID를 저장하지 않는다.
- `medical_wiki_index`에 원문, OCR 전문, 검사 수치, 처방 상세, 결과값을 저장하지 않는다.
- Router에 `medical_sources.source_ref`, `medical_source_runtime.gemini_file`, lock, retry 정보를 전달하지 않는다.
- Router에 `drive_sync_state`, `drive_folder_index`, `drive_file_index`를 전달하지 않는다.

## ID 생성 정책

`source_id`, `page_id`, `log_id`, `job_id`는 순번 기반으로 생성하지 않는다.
동시성 충돌 방지를 위해 UUID 또는 Firestore auto-id를 사용한다.

권장 형식:

```text
source_id = SRC_{patient_id}_{uuid12}
page_id = PAGE_{source_id}
log_id = Firestore auto-id 또는 LOG_{uuid12}
job_id = Firestore auto-id 또는 JOB_{uuid12}
```

예시:

```text
SRC_P0001_A8F39C21D4B2
PAGE_SRC_P0001_A8F39C21D4B2
LOG_91D7A2C4F018
JOB_A42F19C7D0E3
```

주의:

- 날짜 + 순번 기반 ID는 동시 요청에서 충돌할 수 있으므로 사용하지 않는다.
- 사용자가 보는 카카오톡 응답에는 `source_id`, `page_id`, `log_id`, `job_id`를 노출하지 않는다.

## 1. 환자 기본 정보

경로:

```text
patients/{patient_id}
```

예시:

```json
{
  "patient_id": "P0001",
  "name": "손창선",
  "birth": "19461230",
  "drive_folder_id": "google-drive-folder-id",
  "drive_folder_name": "손창선_19461230",
  "status": "active",
  "created_at": "2026-05-09T12:00:00+09:00",
  "updated_at": "2026-05-09T12:00:00+09:00"
}
```

주의:

- `name`, `birth`는 인증용으로만 사용한다.
- 응답 본문에는 생년월일, 폴더명, 원본 파일명을 노출하지 않는다.
- `drive_folder_id`를 환자 폴더 접근 기준으로 사용한다.
- `status != active`인 환자는 인증과 채팅 대상에서 제외한다.

## 2. 카카오 사용자 매핑

경로:

```text
kakao_user_map/{kakao_user_id_hash}
```

예시:

```json
{
  "kakao_user_id_hash": "sha256:...",
  "patient_id": "P0001",
  "authenticated_at": "2026-05-09T12:00:00+09:00",
  "expires_at": "2026-05-10T12:00:00+09:00",
  "status": "active",
  "created_at": "2026-05-09T12:00:00+09:00",
  "updated_at": "2026-05-09T12:00:00+09:00"
}
```

중요:

- 원본 `kakao_user_id`를 평문으로 저장하지 않는다.
- 해시값을 document id로 사용한다.
- 서버 재배포 후에도 인증 상태를 유지하기 위해 Firestore를 사용한다.
- 인증 초기화 시 해당 `kakao_user_id_hash` 매핑을 비활성화하거나 삭제한다.

## 3. medical_sources

경로:

```text
patients/{patient_id}/medical_sources/{source_id}
```

역할:

- 이 source가 어느 환자의 어떤 Google Drive 원본 파일인지 식별한다.
- 원본 파일 변경 여부를 판단한다.
- source 생명주기를 관리한다.
- Router용 wiki page와 Files API runtime 상태는 저장하지 않는다.

문서화용 schema:

```jsonc
{
  // 시스템 내부에서 원본 의료 파일을 식별하는 ID.
  // Router와 medical_wiki_pages는 이 source_id를 통해 원본 파일과 연결된다.
  // 사용자 응답에는 노출하지 않는다.
  "source_id": "SRC_P0001_A8F39C21D4B2",

  // 이 원본 파일이 속한 환자 ID. 환자 격리와 권한 검증의 기준이다.
  "patient_id": "P0001",

  // 원본 source의 종류. MVP에서는 Google Drive 파일만 사용한다.
  "source_type": "google_drive_file",

  // 실제 외부 원본 파일에 대한 참조 정보.
  // 내부 운영용이며 Router와 사용자 응답에는 전달하지 않는다.
  "source_ref": {
    "drive_file_id": "internal-only",
    "drive_folder_id": "internal-only",
    "original_filename": "internal-only",
    "mime_type": "application/pdf",
    "file_size_bytes": 8420000,
    "file_hash": "sha256...",
    "drive_modified_at": "2026-05-09T10:00:00Z"
  },

  // ACTIVE, STALE, DELETED, INACTIVE, UNSUPPORTED
  "source_status": "ACTIVE",

  // Drive Sync Worker가 이 source를 마지막으로 확인하고 Firestore에 반영한 시간.
  "last_sync_at": "2026-05-09T12:00:00+09:00",

  // Firestore medical_sources 문서가 처음 생성된 시간.
  "created_at": "2026-05-09T12:00:00+09:00",

  // Firestore medical_sources 문서가 마지막으로 갱신된 시간.
  "updated_at": "2026-05-09T12:00:00+09:00"
}
```

저장용 예시:

```json
{
  "source_id": "SRC_P0001_A8F39C21D4B2",
  "patient_id": "P0001",
  "source_type": "google_drive_file",
  "source_ref": {
    "drive_file_id": "internal-only",
    "drive_folder_id": "internal-only",
    "original_filename": "internal-only",
    "mime_type": "application/pdf",
    "file_size_bytes": 8420000,
    "file_hash": "sha256...",
    "drive_modified_at": "2026-05-09T10:00:00Z"
  },
  "source_status": "ACTIVE",
  "last_sync_at": "2026-05-09T12:00:00+09:00",
  "created_at": "2026-05-09T12:00:00+09:00",
  "updated_at": "2026-05-09T12:00:00+09:00"
}
```

권장 `source_status`:

```text
ACTIVE
STALE
DELETED
INACTIVE
UNSUPPORTED
```

주의:

- `drive_modified_at`은 Google Drive 기준 파일 수정 시간이다.
- `drive_modified_at`은 의료 문서의 검사일, 진료일, 발급일이 아니다.
- `original_filename`, `drive_file_id`, `drive_folder_id`는 내부 운영용이며 사용자에게 노출하지 않는다.
- 지원하지 않는 파일 형식은 `UNSUPPORTED`로 기록하고 원본을 삭제하지 않는다.

## Temporary upload attachments

경로:

```text
upload_tokens/{token_id}
patients/{patient_id}/active_attachments/{kakao_user_id_hash}
```

`upload_tokens/{token_id}` 예시:

```json
{
  "token_id": "UPTOK_...",
  "patient_id": "P0001",
  "kakao_user_id_hash": "sha256:...",
  "status": "PENDING",
  "expires_at": "2026-06-04T16:00:00+09:00",
  "used_at": "",
  "created_at": "2026-06-04T15:45:00+09:00",
  "updated_at": "2026-06-04T15:45:00+09:00"
}
```

`patients/{patient_id}/active_attachments/{kakao_user_id_hash}` 예시:

```json
{
  "attachment_id": "ATT_...",
  "upload_token_id": "UPTOK_...",
  "patient_id": "P0001",
  "kakao_user_id_hash": "sha256:...",
  "gemini_file": {
    "file_name": "internal-only",
    "uri": "internal-only",
    "mime_type": "image/jpeg",
    "state": "ACTIVE",
    "expiration_time": "2026-06-06T15:45:00Z",
    "uploaded_at": "2026-06-04T15:45:00+09:00",
    "last_checked_at": "2026-06-04T15:45:00+09:00"
  },
  "mime_type": "image/jpeg",
  "file_size_bytes": 8420000,
  "status": "ACTIVE",
  "expires_at": "2026-06-04T16:45:00+09:00",
  "created_at": "2026-06-04T15:45:00+09:00",
  "updated_at": "2026-06-04T15:45:00+09:00"
}
```

정책:

- 업로드 token은 환자 ID와 `kakao_user_id_hash`에 바인딩하며 성공한 POST에서만 `USED` 처리한다.
- active attachment는 환자 + 카카오 세션당 최근 1개만 유지한다.
- 임시 업로드 파일은 Google Drive source of truth가 아니며 `medical_sources`, `medical_wiki_pages`, `medical_wiki_index`에 저장하지 않는다.
- 저장 필드는 업로드 token ID, Gemini Files runtime 참조, MIME, 크기, 만료시각, 상태로 제한한다.
- 이미 `USED` 처리된 token의 재전송은 active attachment의 `upload_token_id`가 같은 경우에만 완료 상태로 간주한다.
- 원본 파일명, 파일 내용, Drive ID, Gemini file URI/name은 사용자 응답과 chat log에 노출하지 않는다.

## 4. medical_wiki_pages

경로:

```text
patients/{patient_id}/medical_wiki_pages/{page_id}
```

역할:

- 원본 의료 source 하나에 대응되는 안전한 LLM Wiki `source_summary` page다.
- Router가 사용자의 질문에 맞는 source를 선택할 수 있도록 category, date, page_count, tags, description, navigation hints, quality를 제공한다.
- 의료 결과를 요약하지 않는다.
- 최종 답변 근거가 아니다.

문서화용 schema:

```jsonc
{
  // wiki page의 고유 ID. source 하나에 대응되는 page 식별자.
  "page_id": "PAGE_SRC_P0001_A8F39C21D4B2",

  // 이 wiki page가 설명하는 원본 source ID.
  "source_id": "SRC_P0001_A8F39C21D4B2",

  // 이 page가 속한 환자 ID. 환자별 격리 기준.
  "patient_id": "P0001",

  // wiki page 유형. MVP에서는 원본 source 하나를 설명하는 source_summary만 사용.
  "page_type": "source_summary",

  // page schema 구조 버전.
  "schema_version": 1,

  // 이 page 내용의 버전. page 재생성 또는 수정 시 증가 가능.
  "page_version": 1,

  // index.md의 frontmatter에 해당하는 핵심 탐색 metadata.
  "frontmatter": {
    "page_count": 8,
    "category": "health_checkup",
    "date": "2026-04-21",
    "date_source": "content",
    "date_confidence": 0.55,
    "tags": [
      "건강검진",
      "혈액검사",
      "종합소견"
    ]
  },

  // 이 page가 어떤 질문에 유용한지 설명하는 한 문장. 의료 결과 요약 금지.
  "description": "검사 수치와 건강검진 종합소견 관련 질문에서 확인할 수 있는 문서입니다.",

  // Router가 이 page를 열지 판단하는 탐색 힌트.
  "navigation": {
    "anchors": [
      "혈당",
      "콜레스테롤",
      "간수치",
      "신장기능",
      "건강검진"
    ],
    "open_when": [
      "검사 수치와 관련된 질문",
      "건강검진 결과를 요약해달라는 질문",
      "의사 종합소견을 확인하는 질문"
    ],
    "skip_when": [
      "처방약 복용법 질문",
      "영상검사 판독 질문",
      "입퇴원 기록 질문"
    ]
  },

  // LLM이 생성한 page metadata의 품질 정보.
  "quality": {
    "confidence": 0.86,
    "needs_review": false,
    "warnings": []
  },

  // 이 page가 언제, 어떤 모델에 의해 생성/갱신됐는지.
  "provenance": {
    "generated_by": "gemini-2.5-flash",
    "generated_at": "2026-05-13T16:00:00+09:00",
    "updated_at": "2026-05-13T16:00:00+09:00"
  }
}
```

저장용 예시:

```json
{
  "page_id": "PAGE_SRC_P0001_A8F39C21D4B2",
  "source_id": "SRC_P0001_A8F39C21D4B2",
  "patient_id": "P0001",
  "page_type": "source_summary",
  "schema_version": 1,
  "page_version": 1,
  "frontmatter": {
    "page_count": 8,
    "category": "health_checkup",
    "date": "2026-04-21",
    "date_source": "content",
    "date_confidence": 0.55,
    "tags": [
      "건강검진",
      "혈액검사",
      "종합소견"
    ]
  },
  "description": "검사 수치와 건강검진 종합소견 관련 질문에서 확인할 수 있는 문서입니다.",
  "navigation": {
    "anchors": [
      "혈당",
      "콜레스테롤",
      "간수치",
      "신장기능",
      "건강검진"
    ],
    "open_when": [
      "검사 수치와 관련된 질문",
      "건강검진 결과를 요약해달라는 질문",
      "의사 종합소견을 확인하는 질문"
    ],
    "skip_when": [
      "처방약 복용법 질문",
      "영상검사 판독 질문",
      "입퇴원 기록 질문"
    ]
  },
  "quality": {
    "confidence": 0.86,
    "needs_review": false,
    "warnings": []
  },
  "provenance": {
    "generated_by": "gemini-2.5-flash",
    "generated_at": "2026-05-13T16:00:00+09:00",
    "updated_at": "2026-05-13T16:00:00+09:00"
  }
}
```

`page_type`:

```text
source_summary
= 원본 의료 source 하나를 설명하는 wiki page. MVP에서 유일하게 사용한다.
```

향후 확장 가능한 page_type:

```text
index
topic
timeline
routing_note
lint_report
```

`category` 정의:

```text
health_checkup = 건강검진 기록. 정기 또는 종합 건강검진 결과 문서.
lab_result = 검사 결과. 혈액, 소변, 기능검사 등 검사 결과 중심 문서.
prescription = 처방 기록. 처방약, 복용 기록, 투약 내역 중심 문서.
doctor_note = 진료 기록. 외래, 진료 경과, 의사 소견 중심 문서.
diagnosis_certificate = 진단서. 진단서, 소견서, 증명서 성격의 문서.
imaging_report = 영상검사 결과. X-ray, CT, MRI, 초음파 등 영상검사 판독 문서.
discharge_summary = 퇴원 요약. 입원 경과와 퇴원 시 요약을 담은 문서.
referral = 진료 의뢰서. 타 의료기관 의뢰와 전달 정보를 담은 문서.
mixed_medical_record = 진료 및 검사 기록. 진료기록, 검사결과, 처방 등 여러 유형이 섞인 문서.
unknown = 의료 문서. 문서 유형을 안전하게 특정하기 어려운 의료 문서.
```

`date_source` 예시:

```text
content
filename
manual
drive_metadata
unknown
```

주의:

- `frontmatter.title`은 새로 저장하지 않는다. 레거시 문서에 남아 있어도 Router와 출처 렌더링은 사용하지 않는다.
- `description`은 문서의 실제 의료 결과를 요약하지 않는다.
- `navigation.open_keywords`에는 수치, 판정 결과, 개인정보를 넣지 않는다.
- `navigation.open_when`은 이 source를 열어보면 좋은 질문 상황이다.
- `navigation.skip_when`은 이 source를 우선 선택하지 말아야 하는 질문 상황이다.
- `quality.confidence`가 낮거나 `needs_review == true`이면 Router 후보에서 낮은 우선순위로 처리하거나 제외한다.
- `medical_wiki_pages`에는 Drive ID, Gemini file URI, lock 상태, retry 상태를 저장하지 않는다.

## 5. medical_wiki_index

경로:

```text
patients/{patient_id}/medical_wiki_index/main
```

역할:

- 기존 LLM Wiki의 `index.md`에 해당한다.
- 현재 환자의 `medical_wiki_pages` 목록을 압축한 compact catalog다.
- Router는 이 index를 먼저 읽고 관련 page 또는 source_id를 선택한다.
- index는 `medical_wiki_pages`에서 재생성 가능한 derived cache다.

예시:

```json
{
  "index_id": "main",
  "patient_id": "P0001",
  "index_type": "medical_wiki_index",
  "schema_version": 1,
  "index_version": 1,
  "description": "현재 환자의 medical_wiki_pages를 요약한 문서 catalog입니다. Router는 이 index를 먼저 읽고 관련 source_summary page 또는 source_id를 선택합니다.",
  "pages": [
    {
      "page_id": "PAGE_SRC_P0001_A8F39C21D4B2",
      "source_id": "SRC_P0001_A8F39C21D4B2",
      "page_type": "source_summary",
      "category": "health_checkup",
      "date": "2026-04-21",
      "date_source": "content",
      "date_confidence": 0.55,
      "description": "검사 수치와 건강검진 종합소견 관련 질문에서 확인할 수 있는 문서입니다.",
      "tags": [
        "건강검진",
        "혈액검사"
      ],
      "anchors": [
        "건강검진",
        "검사 결과",
        "혈액검사",
        "종합소견"
      ],
      "open_when": [
        "검사 수치와 관련된 질문",
        "건강검진 결과 요약 질문"
      ],
      "confidence": 0.86,
      "needs_review": false,
      "page_version": 1
    }
  ],
  "groupings": {
    "by_category": {
      "health_checkup": [
        "PAGE_SRC_P0001_A8F39C21D4B2"
      ]
    },
    "by_tag": {
      "건강검진": [
        "PAGE_SRC_P0001_A8F39C21D4B2"
      ],
      "혈액검사": [
        "PAGE_SRC_P0001_A8F39C21D4B2"
      ]
    },
    "by_date": {
      "2026-04": [
        "PAGE_SRC_P0001_A8F39C21D4B2"
      ]
    }
  },
  "updated_at": "2026-05-13T16:00:00+09:00"
}
```

주의:

- `medical_wiki_index.pages`에는 compact entry만 저장한다.
- `skip_when`은 index에 꼭 넣지 않아도 된다. 세부 회피 조건은 `medical_wiki_pages`에서 확인한다.
- `groupings`는 page를 category, tag, month 기준으로 묶은 보조 catalog다.
- `by_date`는 `YYYY-MM` 형식을 사용한다.
- index는 derived cache이므로 손상되면 `medical_wiki_pages`에서 재컴파일할 수 있어야 한다.

## 6. medical_wiki_logs

경로:

```text
patients/{patient_id}/medical_wiki_logs/{log_id}
```

역할:

- 기존 LLM Wiki의 `log.md`에 해당한다.
- wiki page, wiki index가 언제, 왜, 어떻게 바뀌었는지 남기는 append-only 변경 일지다.
- Router가 직접 읽는 데이터가 아니라 운영, 디버깅, 재생성 추적용이다.

MVP 권장 event_type:

```text
page_generated
page_rebuilt
index_compiled
page_failed
source_deleted
```

예시:

```json
{
  "log_id": "LOG_20260513_001",
  "patient_id": "P0001",
  "event_type": "page_generated",
  "title": "source summary page generated",
  "source_id": "SRC_P0001_A8F39C21D4B2",
  "page_ids": [
    "PAGE_SRC_P0001_A8F39C21D4B2"
  ],
  "index_updated": true,
  "status": "success",
  "message": "source_summary page generated and medical_wiki_index updated",
  "created_at": "2026-05-13T16:00:00+09:00"
}
```

주의:

- 로그에는 원본 의료 문서 내용, 검사 수치, OCR 전문, 사용자 질문 전문, Drive ID, Gemini file URI, 원본 파일명을 저장하지 않는다.
- Firestore의 `updated_at`은 마지막 변경 시각만 알려준다.
- `medical_wiki_logs`는 왜, 무엇이, 어떻게 바뀌었는지 알려준다.
- MVP에서는 모든 변경을 기록하지 말고 page 생성, 재생성, 실패, index compile만 기록한다.

## 7. medical_source_runtime

경로:

```text
patients/{patient_id}/medical_source_runtime/{source_id}
```

역할:

- Gemini Files API 업로드 상태를 관리한다.
- Files API 만료 여부를 관리한다.
- Google Drive 동기화 상태를 관리한다.
- wiki page 생성 상태를 관리한다.
- Firestore lock, lease, retry, 실패 정보를 관리한다.

예시:

```json
{
  "source_id": "SRC_P0001_A8F39C21D4B2",
  "patient_id": "P0001",
  "gemini_file": {
    "file_name": "files/abc123",
    "uri": "https://generativelanguage.googleapis.com/...",
    "mime_type": "application/pdf",
    "state": "ACTIVE",
    "expiration_time": "2026-05-11T12:00:00Z",
    "uploaded_at": "2026-05-09T12:00:00+09:00",
    "last_checked_at": "2026-05-09T12:01:00+09:00",
    "error": null
  },
  "sync": {
    "status": "READY",
    "lock_owner": null,
    "lease_expires_at": null,
    "retry_count": 0,
    "last_failure": null,
    "last_synced_at": "2026-05-09T12:02:00+09:00"
  },
  "wiki_sync": {
    "status": "READY",
    "lock_owner": null,
    "lease_expires_at": null,
    "retry_count": 0,
    "last_failure": null,
    "last_generated_at": "2026-05-09T12:02:00+09:00"
  },
  "updated_at": "2026-05-09T12:02:00+09:00"
}
```

권장 `sync.status`:

```text
DISCOVERED
WIKI_PENDING
WIKI_PROCESSING
READY
UPLOADING
PROCESSING
STALE
EXPIRED
FAILED
```

권장 `gemini_file.state`:

```text
NONE
UPLOADING
PROCESSING
ACTIVE
EXPIRED
FAILED
```

주의:

- `medical_source_runtime`에는 category, tags, description, navigation hints를 저장하지 않는다.
- Gemini Files API의 `file_name` 또는 `uri`만 보고 환자 소유 여부를 판단하지 않는다.
- Files API 사용 전에는 반드시 `medical_sources/{source_id}.patient_id`와 현재 인증 환자의 `patient_id`를 비교한다.

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

## 9. 최근 대화 세션

경로:

```text
patients/{patient_id}/chat_sessions/{kakao_user_id_hash}
```

목적:

- 최근 5턴 정도의 문맥 유지
- 서버 재배포 후에도 세션 유지
- 장기 기억이 아니라 단기 대화 문맥 저장

예시:

```json
{
  "patient_id": "P0001",
  "kakao_user_id_hash": "sha256:...",
  "recent_messages": [
    {
      "role": "user",
      "text": "혈당 수치가 어떻게 나왔나요?",
      "created_at": "2026-05-09T12:00:00+09:00"
    },
    {
      "role": "assistant",
      "text": "혈당 관련 답변을 제공함",
      "status": "ok",
      "created_at": "2026-05-09T12:00:10+09:00"
    }
  ],
  "updated_at": "2026-05-09T12:00:10+09:00",
  "expires_at": "2026-05-10T12:00:10+09:00"
}
```

주의:

- 최근 메시지는 최대 5턴으로 제한한다.
- `chat_sessions.recent_messages`의 assistant 메시지는 최종 답변 전문이 아니라 후속 질문 해석에 필요한 짧은 요약 또는 상태만 저장한다.
- 최종 assistant 답변 본문은 별도로 `chat_logs`에 `role="assistant"` row로 저장한다.
- 원문 질문도 민감정보일 수 있으므로 접근 권한과 보관 기간을 제한한다.
- Cloud Logging에는 질문 원문을 출력하지 않는다.
- 최근 대화는 문서 근거가 아니라 후속 질문 해석 보조 정보다.

## 10. 환자별 채팅 메시지 로그

경로:

```text
patients/{patient_id}/chat_logs/{log_id}
```

목적:

- 인증된 사용자가 `/kakao/chat`에 입력한 원문 메시지를 저장한다.
- 사용자에게 실제 전송된 assistant 답변 본문을 `role="assistant"` 메시지로 저장한다.
- Gemini JSON 원문, evidence quote 전문, 내부 source 식별자는 저장하지 않는다.
- callback job 생성 여부와 관계없이, 인증된 환자의 사용자 메시지는 가능한 한 요청 수신 직후 저장한다.
- 사용자 질문/답변 이력 확인, callback job 연결, 장애 상황 추적, 최소 운영 분석에 사용한다.

예시:

```json
{
  "log_id": "LOG_91D7A2C4F018",
  "patient_id": "P0001",
  "kakao_user_id_hash": "sha256:...",

  "role": "user",
  "message": "혈당 수치가 어떻게 나왔나요?",
  "message_type": "question",

  "job_id": "JOB_A8F39C21D4B2",

  "created_at": "2026-05-09T12:00:00+09:00"
}
```

assistant 답변 row 예시:

```json
{
  "log_id": "ANSWER_JOB_A8F39C21D4B2",
  "patient_id": "P0001",
  "kakao_user_id_hash": "sha256:...",
  "role": "assistant",
  "message": "검증된 kakaotalk_render 본문과 백엔드가 붙인 출처 섹션",
  "message_type": "answer",
  "job_id": "JOB_A8F39C21D4B2",
  "created_at": "2026-05-09T12:00:10+09:00"
}
```

`job_id`가 아직 없거나 callback job을 만들지 않는 즉시 응답 경로에서는 `job_id`를 `null`로 저장할 수 있다.
callback job이 생성되면 `chat_logs/{log_id}.job_id`를 생성된 `job_id`로 갱신한다.
callback으로 실제 전송된 assistant 답변은 `ANSWER_{job_id}` 형식의 deterministic `log_id`로 저장하여 retry 중복 저장을 막는다.
즉시 응답 경로에서 저장되는 assistant/system 메시지는 `ANSWER_{related_log_id}` 또는 `ANSWER_{job_id}` 형식을 사용한다.
관리자 대시보드는 이 저장 스키마를 바꾸지 않고 `job_id` 또는 `ANSWER_{related_log_id}` 규칙으로 질문/답변을 표시용 pair로 묶는다.

필드 설명:

```text
log_id
= 사용자 메시지 로그의 고유 ID

patient_id
= 현재 인증된 환자 ID

kakao_user_id_hash
= 원본 카카오 사용자 ID를 해시 처리한 값

role
= 메시지 작성자. `user` 또는 `assistant`.

message
= 사용자가 입력한 원문 메시지 또는 사용자에게 실제 전송된 assistant 답변

message_type
= 메시지 유형. 사용자 질문은 `question`, assistant 답변은 `answer` 또는 `system`을 사용한다.

job_id
= 이 메시지로 생성된 callback job ID. callback job이 없거나 생성 전이면 null일 수 있다.

created_at
= 사용자 메시지를 서버가 수신한 시각 또는 assistant 답변을 저장한 시각
```

권장 `message_type`:

```text
question
answer
auth_related
reset_request
system_command
system
unknown
```

주의:

- `chat_logs`에는 Gemini JSON 원문을 저장하지 않는다.
- `chat_logs`에는 Drive/Gemini/source 내부 식별자, 원본 파일명, 생년월일, callback_url을 저장하지 않는다.
- `chat_logs`에는 routing 결과, used_source_ids, runtime latency, safety 검증 결과를 기본 저장하지 않는다.
- assistant 답변은 최종 Kakao render 본문만 저장하며 Gemini 원본 JSON이나 evidence quote 전문은 저장하지 않는다.
- 운영 분석이 필요한 경우 `job_id`를 통해 `kakao_callback_jobs/{job_id}` 또는 별도 운영 로그를 참조한다.
- 사용자 메시지와 assistant 답변은 민감정보를 포함할 수 있으므로 접근 권한과 보관 기간을 제한한다.
- Cloud Logging에는 `message` 원문을 출력하지 않는다.

## 11. kakao_callback_jobs

경로:

```text
kakao_callback_jobs/{job_id}
```

목적:

- 카카오 callback 기반 비동기 응답 작업 상태를 관리한다.
- 사용자 메시지 로그와 callback 처리 상태를 연결한다.
- callback 실패, timeout, retry, 중복 실행 방지에 사용한다.
- 답변 본문은 `chat_logs`의 assistant 메시지로 저장하고, callback job에는 본문을 저장하지 않는다.
- Gemini JSON 원문, evidence quote 전문은 저장하지 않는다.

예시:

```json
{
  "job_id": "JOB_A8F39C21D4B2",
  "patient_id": "P0001",
  "kakao_user_id_hash": "sha256:...",
  "chat_log_id": "LOG_91D7A2C4F018",

  "status": "PENDING",
  "callback_url": "internal-temporary-callback-url",

  "retry_count": 0,
  "last_failure": null,

  "created_at": "2026-05-09T12:00:00+09:00",
  "updated_at": "2026-05-09T12:00:00+09:00",
  "expires_at": "2026-05-09T12:10:00+09:00"
}
```

권장 `status`:

```text
PENDING
PROCESSING
CALLBACK_SENT
FAILED
EXPIRED
```

주의:

- `callback_url`은 callback 전송을 위한 임시 값으로만 사용한다.
- `callback_url`은 Cloud Logging에 출력하지 않는다.
- `expires_at` 기반 TTL 삭제를 권장한다.
- 답변 본문, Gemini JSON 원문, evidence quote, 내부 source_id 목록은 callback job에 저장하지 않는다.
- 실패 원인은 `last_failure`에 내부 코드와 최소 진단 정보만 저장하고 사용자에게 직접 노출하지 않는다.

## 12. Google Drive 변경 추적 상태

경로:

```text
drive_sync_state/{scope_id}
```

목적:

- Google Drive Changes API의 checkpoint pageToken 관리
- `/admin/sync-drive-changes` 전역 lock, lease 관리
- 변경분 동기화 실패, retry, 마지막 성공 시각 관리
- 전체 스캔 정합성 검사 시각 관리

예시:

```json
{
  "scope_id": "main",
  "scope_type": "my_drive",
  "saved_page_token": "123456",
  "last_success_page_token": "123456",
  "sync_status": "READY",
  "lock_owner": null,
  "lease_expires_at": null,
  "retry_count": 0,
  "last_failure": null,
  "last_synced_at": "2026-05-09T12:02:00+09:00",
  "last_full_scan_at": "2026-05-09T03:00:00+09:00",
  "updated_at": "2026-05-09T12:02:00+09:00"
}
```

권장 `sync_status`:

```text
READY
PROCESSING
BOOTSTRAP_REQUIRED
FAILED
```

주의:

- `saved_page_token`은 모든 변경사항 처리가 성공한 뒤에만 갱신한다.
- 전역 lock이 유효하면 다음 1분 주기 호출은 즉시 종료한다.
- 이 collection은 내부 운영용이며 Router와 최종 QA에 전달하지 않는다.

## 13. Google Drive 환자 폴더 index

경로:

```text
drive_folder_index/{drive_folder_id}
```

예시:

```json
{
  "drive_folder_id": "google-drive-folder-id",
  "patient_id": "P0001",
  "status": "active",
  "created_at": "2026-05-09T12:00:00+09:00",
  "updated_at": "2026-05-09T12:00:00+09:00"
}
```

주의:

- `drive_folder_index`는 인증 기준이 아니라 동기화 편의를 위한 index다.
- 실제 환자 활성 여부는 `patients/{patient_id}.status`도 함께 확인한다.
- 카카오톡 응답에 drive_folder_id를 노출하지 않는다.

## 14. Google Drive 파일 index

경로:

```text
drive_file_index/{drive_file_id}
```

예시:

```json
{
  "drive_file_id": "google-drive-file-id",
  "patient_id": "P0001",
  "source_id": "SRC_P0001_A8F39C21D4B2",
  "drive_folder_id": "google-drive-folder-id",
  "status": "ACTIVE",
  "created_at": "2026-05-09T12:00:00+09:00",
  "updated_at": "2026-05-09T12:02:00+09:00"
}
```

권장 `status`:

```text
ACTIVE
INACTIVE
DELETED
UNSUPPORTED
FAILED
```

주의:

- `drive_file_index`는 내부 동기화용 index다.
- 환자 소유권의 최종 검증은 `patients/{patient_id}/medical_sources/{source_id}`의 patient_id와 source_status로 다시 확인한다.
- Router와 최종 QA에는 drive_file_id를 전달하지 않는다.

## Firestore transaction, lock, lease 정책

Files API 재업로드, wiki page 생성, Drive 변경분 동기화처럼 중복 실행되면 안 되는 작업은 Firestore transaction으로 제어한다.

lock은 두 층으로 나눈다.

```text
전역 sync lock
= `/admin/sync-drive-changes`가 동시에 여러 번 실행되는 것을 방지
= drive_sync_state/{scope_id}에 저장

source 단위 lock
= 같은 source_id에 대한 wiki page 생성, Files API 업로드, runtime 갱신 중복 방지
= patients/{patient_id}/medical_source_runtime/{source_id}에 저장
```

중요:

- 이전 1분 주기 실행이 아직 끝나지 않았으면 다음 실행은 전역 lock 때문에 중복 처리하지 않는다.
- lock 해제 실패에 대비해 lease 만료 기반 복구를 반드시 허용한다.
- 동시에 여러 요청이 같은 Drive 원본 파일을 Files API에 중복 업로드하지 않도록 한다.
- 동시에 여러 sync worker가 같은 source의 wiki page를 중복 생성하지 않도록 한다.
- retry_count가 일정 횟수를 초과하면 `FAILED`로 처리하고 안전 실패 메시지를 반환한다.
