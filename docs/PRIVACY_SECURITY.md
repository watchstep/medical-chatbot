# Privacy and Security

## 개인정보 및 안전 정책

이름을 제외한 아래 정보는 모두 개인정보로 간주하며 사용자에게 출력하지 않는다.

```text
주민등록번호
생년월일
나이
성별
환자번호
전화번호
이메일
주소
상세주소
우편번호
보호자명
계좌번호
카드번호
보험번호
파일 안에 포함된 모든 개인 식별 정보
```

Medical Wiki에 저장 금지:

```text
원문 전문
OCR 전문
검사 결과표 전체
검사 수치 전체
처방 상세
진단명 전체 목록
주민등록번호
환자번호
전화번호
주소
보험번호
Google Drive ID
Gemini file URI
원본 파일명
치료 판단
의학적 조언
```

주의:

- `medical_wiki_pages.frontmatter.title`은 새로 저장하지 않는다. 사용자 표시명은 category, date, page_count에서 백엔드가 생성한다.
- `medical_wiki_pages.description`은 문서가 어떤 질문에 유용한지 설명해야 하며, 실제 의료 결과를 요약하지 않는다.
- `medical_wiki_pages.navigation.open_keywords`에는 결과값이나 판정 결과를 넣지 않는다.
- Final QA가 의료적 판단을 단정하지 않도록 한다.
- 응급, 개인정보, 비용 관련 status는 백엔드 고정 메시지로 처리한다.

## 카카오 응답 형식

기본 응답은 `simpleText`를 사용한다.

```json
{
  "version": "2.0",
  "template": {
    "outputs": [
      {
        "simpleText": {
          "text": "검증된 kakaotalk_render + 백엔드가 생성한 하단 출처 섹션"
        }
      }
    ]
  }
}
```

오류가 발생해도 내부 에러를 노출하지 않는다.
Gemini JSON 원문을 사용자에게 노출하지 않는다.
검증 실패 시에는 안전 실패 메시지를 반환한다.

## 실패 기록 정책

Firestore의 `medical_source_runtime.sync.last_failure`, `medical_source_runtime.wiki_sync.last_failure`, `kakao_callback_jobs.last_failure`, `medical_wiki_logs`를 우선 사용한다.

실패 코드 예시:

```text
DRIVE_SYNC_FAILED
DRIVE_FILE_NOT_FOUND
DRIVE_FILE_UNSUPPORTED
DRIVE_FILE_HASH_FAILED
WIKI_PAGE_GENERATION_FAILED
WIKI_PAGE_SCHEMA_INVALID
WIKI_INDEX_COMPILE_FAILED
FILE_UPLOAD_FAILED
FILE_PROCESSING_FAILED
FILE_EXPIRED
CALLBACK_SEND_FAILED
CALLBACK_JOB_EXPIRED
CALLBACK_JOB_FAILED
MODEL_CANNOT_READ
MODEL_JSON_PARSE_FAILED
MODEL_SCHEMA_INVALID
MISSING_EVIDENCE
MISSING_EVIDENCE_REFS
SOURCE_MISMATCH
UNSAFE_RENDER
ROUTER_JSON_PARSE_FAILED
ROUTER_SCHEMA_INVALID
ROUTER_SOURCE_MISMATCH
FIRESTORE_LOCK_TIMEOUT
DRIVE_CHANGES_TOKEN_MISSING
DRIVE_CHANGES_LIST_FAILED
DRIVE_CHANGES_TOKEN_UPDATE_FAILED
DRIVE_CHANGE_FILE_GET_FAILED
DRIVE_CHANGE_PATIENT_MAPPING_FAILED
DRIVE_SYNC_GLOBAL_LOCK_TIMEOUT
```

중요:

- 실패 원인을 사용자에게 직접 노출하지 않는다.
- 실패 시 원본 파일을 삭제하지 않는다.
- 실패한 source는 document pack에서 제외하거나 안전 실패 메시지로 처리한다.
- 실패한 wiki page 생성은 retry_count 정책에 따라 재시도한다.
- 업로드 token URL, Gemini file_name, file_uri, 원본 파일명은 chat log, 카카오 응답, 관리자 대시보드에 노출하지 않는다.
- 업로드 링크 발급 assistant log에는 실제 URL 대신 고정 문구만 저장한다.

## 직접 PDF Parsing 정책

초기 MVP에서는 직접 PDF parsing 또는 Markdown 전문 변환 파일 저장을 구현하지 않는다.

현재 우선순위:

```text
1차 MVP:
Google Drive 원본 파일
+ Drive Sync Worker
+ Firestore medical_sources, medical_wiki_pages, medical_wiki_index, medical_source_runtime 분리 저장
+ Gemini Files API lazy upload
+ Medical Wiki Router 기반 source 선택
+ Whole Document QA
+ JSON 검증
+ 실패 기록

2차:
Router 선택 오류 분석
medical_wiki_pages 품질 개선
medical_wiki_index groupings 보강
medical_wiki_logs 기반 rebuild 분석
Google Drive Changes API 또는 push notification 기반 동기화 검토

3차:
필요한 경우만 parsing layer 추가
필요하면 topic page 또는 timeline page 추가
필요하면 Graphify는 런타임이 아니라 schema/설계 시각화 용도로만 사용
```

## 최종 개발 원칙

- MVP에서는 `primary_source_id` 하나만 선택한다.
- multi-document comparison은 2차 이후로 미룬다.
- topic page, timeline page, graph layer는 2차 이후로 미룬다.
- Medical LLM Wiki는 원본 문서를 선택하기 위한 지도다.
- 원본 의료 문서 전체가 최종 답변의 유일한 근거다.
- Firestore schema는 운영 안정성과 환자 격리를 우선한다.
- Router가 선택한 source_id는 백엔드가 반드시 환자 소유권, source_status, runtime readiness를 검증한다.
- intent 판단은 운영 환경에서 Gemini Router가 수행한다. keyword 기반 intent 분류는 local/test 전용으로만 사용한다.
- Gemini Final QA는 `kakaotalk_render` 하나에 본문만 작성하고, `📄 출처` 섹션은 절대 작성하지 않는다.
- 출처 섹션은 백엔드가 검증된 used_source_ids를 기준으로 category, date, page_count 기반 문서 표시명만 붙인다.
- cannot_verify는 Router intent가 아니라 Final QA answer status로만 유지한다.
- Gemini Files API 파일은 runtime cache로 취급하고, cleanup이 Google Drive 원본이나 Medical Wiki 계층을 삭제하지 않도록 한다.
- Pre-warm은 사용자 응답 지연을 줄이는 best-effort 최적화이며, 실패 시 기존 lazy upload 흐름으로 반드시 fallback한다.
- Pre-warm과 cleanup은 Cloud Tasks 또는 Cloud Scheduler 기반 관리자 경로로만 실행하고, 카카오 사용자 응답 경로를 막지 않는다.
- 채팅 로그 Google Drive CSV export는 제공하지 않는다.
- `chat_logs`에는 사용자 질문과 실제 전송된 assistant 답변 본문만 저장하고, Gemini JSON 원문, Drive/Gemini/source 내부 식별자, 원본 파일명, 생년월일, callback_url은 저장하지 않는다.
- 관리자 대시보드는 `/admin/dashboard` 이하에서만 제공하며 Basic Auth로 보호한다. 운영/test 배포의 `ADMIN_DASHBOARD_PASSWORD`는 Secret Manager에서 주입하고, `.env.example`, 배포 로그, Cloud Logging에 비밀번호 값을 남기지 않는다.
- 관리자 대시보드의 chat log 조회는 사용자 질문과 assistant 답변을 보여주되 callback_url, Drive/Gemini/source 내부 식별자, 생년월일, 원본 파일명은 노출하지 않는다.
