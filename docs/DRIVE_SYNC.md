# Drive Sync

## Google Drive 구조

Google Drive는 원본 의료 문서 저장소로만 사용한다.
Firestore 도입 이후 Google Drive에 `_document_registry.json`, `_document_metadata.json`, `patient_index.json`을 운영 DB처럼 저장하지 않는다.

권장 구조:

```text
medical-chatbot/
└── patients/
    ├── 손창선_19461230/
    │   ├── 건강검진.pdf
    │   ├── 아버지검사결과.pdf
    │   └── IMG_3021.jpg
    │
    └── 홍길동_19800515/
        ├── 서울대병원자료.pdf
        └── scan001.jpg
```

중요:

- 환자 폴더명은 관리자 편의를 위한 표시명일 뿐이다.
- 시스템 내부 기준은 `patient_id`와 `drive_folder_id`다.
- 가능하면 Google Drive 접근은 `folder_name`보다 `drive_folder_id`를 우선 사용한다.
- 파일명은 자유롭게 허용한다.
- 파일명은 문서 유형과 날짜 판단의 힌트일 뿐 확정 근거가 아니다.
- 파일명이 규칙에 맞지 않아도 의료 자료일 수 있으므로 자동 제외하지 않는다.
- 카카오톡 사용자 응답에는 Google Drive ID, Gemini file name, 내부 source_id, 원본 파일명을 노출하지 않는다.

## Google Drive 업로드 감지 및 Firestore 자동 동기화 정책

MVP의 평상시 자동 동기화는 Cloud Scheduler가 1분마다 `/admin/sync-drive-changes`를 호출하고, Drive Sync Worker가 Google Drive Changes API의 변경 로그를 조회하는 checkpoint polling 방식을 기본으로 한다.
전체 환자 폴더 스캔은 초기 bootstrap, 하루 1회 정합성 검사, 장애 복구용으로만 사용한다.
push notification은 MVP 범위에서 제외하며, 향후 필요하면 Change Sync Worker를 더 빨리 깨우는 trigger로만 추가한다.
실제 변경 목록 처리는 push notification을 사용하더라도 반드시 Changes API의 `changes.list` 흐름으로 수행한다.

### 평상시 변경분 동기화

```text
Cloud Scheduler, 1분 주기
→ Cloud Run `/admin/sync-drive-changes` 호출
→ drive_sync_state/{scope_id}에서 saved_page_token 조회
→ Google Drive Changes API `changes.list(saved_page_token)` 호출
→ 변경된 file_id만 처리
→ 신규, 수정, 삭제 또는 접근 불가 상태를 Firestore에 반영
→ 필요한 경우 medical_wiki_pages 생성 또는 재생성
→ 필요한 경우 medical_wiki_index/main 재컴파일
→ 모든 변경 처리 성공 후 newStartPageToken 저장
```

### 복구용 전체 동기화

```text
Cloud Scheduler, 하루 1회 새벽
→ Cloud Run `/admin/sync-drive` 호출
→ active patients 목록 조회
→ 각 patient의 drive_folder_id로 Google Drive 파일 목록 조회
→ Firestore medical_sources, drive_file_index와 비교
→ 누락된 신규 파일 등록
→ 수정된 파일 STALE 처리
→ 삭제 또는 접근 불가 파일 DELETED 또는 INACTIVE 처리
→ 누락되거나 실패한 medical_wiki_pages 복구
→ medical_wiki_index 재컴파일
→ drive_sync_state.last_full_scan_at 갱신
```

중요:

- 1분 주기로 전체 환자 폴더를 스캔하지 않는다.
- 1분 주기는 `changes.list` 기반 변경 로그 확인에만 사용한다.
- 변경사항이 없으면 빠르게 종료하고 Firestore write를 최소화한다.
- `newStartPageToken`은 모든 변경사항 처리가 성공한 뒤에만 저장한다.
- 중간에 실패하면 saved_page_token을 갱신하지 않고 다음 실행에서 같은 변경분을 다시 처리한다.
- 같은 file_id 변경이 여러 번 처리되어도 결과가 깨지지 않도록 idempotent하게 구현한다.

권장 제한값:

```text
Cloud Scheduler 주기: 1분
Cloud Run timeout: 60초 또는 120초
전역 sync lock lease: 2분 또는 3분
MAX_CHANGES_PER_RUN: 20
MAX_WIKI_PAGE_GENERATIONS_PER_RUN: 3~5
```

## Drive file_id와 patient_id 매핑 정책

Firestore에 아래 내부 index를 유지한다.

```text
drive_folder_index/{drive_folder_id}
drive_file_index/{drive_file_id}
```

역할:

```text
drive_folder_index
= Drive folder_id가 어느 patient_id에 속하는지 찾기 위한 index

drive_file_index
= Drive file_id가 어느 patient_id, source_id에 속하는지 찾기 위한 index
```

신규 파일은 `files.get(file_id)`로 `parents`를 확인한 뒤 `drive_folder_index`에서 patient_id를 찾는다.
기존 파일 수정 또는 삭제는 `drive_file_index`에서 patient_id와 source_id를 찾는다.

주의:

- `drive_folder_index`와 `drive_file_index`는 내부 동기화용 index다.
- Router에 이 index를 전달하지 않는다.
- 카카오톡 응답에 이 index의 값, Drive ID, source_id를 노출하지 않는다.
- 최종 QA 전에는 `medical_sources/{source_id}.patient_id`와 현재 인증 환자의 patient_id를 다시 비교한다.

## 변경 파일 처리 흐름

### 신규 파일 처리

```text
drive_file_index에 drive_file_id 없음
→ files.get 결과의 parents에서 drive_folder_index 매칭
→ active patient_id 확인
→ 지원 파일 형식인지 확인
→ source_id 생성
→ patients/{patient_id}/medical_sources/{source_id} 생성
→ patients/{patient_id}/medical_source_runtime/{source_id} 생성
→ drive_file_index/{drive_file_id} 생성
→ medical_source_runtime.sync.status = DISCOVERED 또는 WIKI_PENDING
→ Google Drive 원본 파일을 wiki page 생성용으로 Gemini Files API에 임시 업로드하거나 직접 전달 가능한 방식으로 준비
→ Gemini wiki page extraction 수행
→ patients/{patient_id}/medical_wiki_pages/{page_id} 저장
→ patients/{patient_id}/medical_wiki_index/main 재컴파일
→ medical_source_runtime.wiki_sync.status = READY
→ medical_source_runtime.sync.status = READY
→ Router catalog에 포함
→ medical_wiki_logs에 page_generated 또는 index_compiled 기록
```

### 수정 파일 처리

```text
drive_file_index에 drive_file_id 있음
→ patient_id, source_id 확인
→ files.get 결과와 medical_sources/{source_id}.source_ref.drive_modified_at, file_size_bytes, file_hash 비교
→ 변경 감지
→ medical_sources/{source_id}.source_status = STALE
→ medical_source_runtime/{source_id}.sync.status = STALE
→ 기존 medical_source_runtime.gemini_file.state = EXPIRED 처리
→ medical_wiki_pages 재생성 필요 표시
→ wiki page extraction 재실행
→ medical_wiki_pages/{page_id} 갱신
→ medical_wiki_index/main 재컴파일
→ medical_source_runtime.wiki_sync.status = READY
→ medical_sources/{source_id}.source_status = ACTIVE
→ medical_source_runtime.sync.status = READY
→ drive_file_index/{drive_file_id} 갱신
→ medical_wiki_logs에 page_rebuilt 또는 index_compiled 기록
```

### 삭제, 이동 또는 접근 불가 파일 처리

```text
change.removed == true 또는 files.get 실패 또는 files.get 결과 trashed == true
→ drive_file_index에서 patient_id, source_id 조회
→ medical_sources/{source_id}.source_status = DELETED 또는 INACTIVE
→ medical_source_runtime/{source_id}.sync.status = EXPIRED
→ medical_source_runtime.gemini_file.state = EXPIRED 처리
→ drive_file_index/{drive_file_id}.status = DELETED 또는 INACTIVE
→ medical_wiki_index에서 제외
→ Router catalog에서 제외
→ 최종 QA document pack 대상에서 제외
→ medical_wiki_logs에 source_deleted 또는 index_compiled 기록
```

주의:

- 삭제된 source는 감사와 장애 분석을 위해 일정 기간 논리 삭제 상태로 보관한다.
- 삭제된 source의 wiki page는 Router catalog에서 제외한다.
- 삭제된 source의 Gemini file은 재사용하지 않는다.
- 접근 권한이 일시적으로 실패한 경우와 실제 삭제를 구분할 수 있도록 runtime.last_failure에 실패 원인을 기록한다.
