## 프로젝트 개요
- 의료진단기록 카카오톡 + Gemini 챗봇 PoC 구현
- 환자가 카카오채널 챗봇으로 질문하면, 서버는 Google Drive에서 해당 환자의 진단기록을 조회하고, Gemini API로 문서 기반 답변을 생성한 뒤 카카오 챗봇 응답으로 반환한다.

## 주요 기술

- Python
- FastAPI
- Uvicorn
- Kakao OpenBuilder Skill
- ngrok
- Google Drive API
- Gemini API
- GCP Cloud Run

## 전체 처리 흐름

## 전체 처리 흐름

```text
카카오 사용자 메시지 수신
→ FastAPI /kakao/skill
→ kakao_user_id 추출
→ 인증 세션 확인
→ 인증 필요 여부 판단

인증이 필요한 경우:
    → 인사 + 인증 안내 반환
    → 인증 입력 수신
    → patient_index.json 조회
    → 환자 존재 확인
    → 인증 성공 시 세션 저장
    → 환자 폴더 조회
    → meta.json 조회 또는 실제 파일 목록으로 최신 정보 계산
    → 최신 기록 안내

인증된 사용자인 경우:
    → 환자 폴더 조회
    → 최신 result/chart 문서 선택
    → Gemini API에 문서와 질문 전달
    → 카카오 simpleText 응답 반환
```

## 카카오톡 첫 인사 및 인증 게이트

챗봇은 사용자 상태에 따라 항상 먼저 인사 또는 인증 안내를 해야 한다.

상태별 처리:

| 사용자 상태 | 처리 |
|---|---|
| 첫 접속 | 기본 인사 + 인증 안내 |
| 재접속, 인증 세션 유효 | `{환자명}님 안녕하세요.` + 최신 기록 안내 또는 질문 처리 |
| 재접속, 인증 세션 만료 | 인사 + 재인증 안내 |
| 장기 미접속 | 인사 + 재인증 안내 |
| 미매핑 사용자 | 기본 인사 + 인증 안내 |
| 미등록 사용자 | 인증 시도 후 등록된 환자 정보를 찾지 못했다는 안내 |

중요:

- 카카오 웰컴 블록에는 개인정보 없는 기본 인사만 둔다.
- 실제 사용자별 인사, 인증 여부 판단, 최신 기록 안내는 FastAPI 스킬 서버에서 처리한다.
- 인증 전에는 환자 폴더, `meta.json`, PDF, Gemini API를 조회하지 않는다.
- 단, 사용자 매핑 확인을 위해 `patient_index.json`은 조회할 수 있다.

## 인증 세션 정책

PoC에서는 `kakao_user_id` 기준의 임시 인증 세션을 사용한다.

권장 정책:

```text
인증 유효 시간: 30분
30분 이내 재접속: 인증 유지
30분 초과 재접속: 재인증
서버 재시작으로 세션 소실: 재인증
```

인증 입력 형식:

```text
{이름} {생년월일}
```

예시:@

```text
손창선 19461230
```

인증 실패 시 어떤 항목이 틀렸는지 구체적으로 알려주지 않는다.

## Google Drive 폴더 구조

Google Drive 루트 폴더명은 반드시 아래와 같다.

```text
medical-chatbot/
```

폴더 구조는 아래 규칙을 따른다.

```text
medical-chatbot/
│
├── patients/
│   ├── P0001_손창선_19461230/
│   │   ├── result_20260421.pdf
│   │   ├── chart_20260421.pdf
│   │   └── meta.json  (선택)
│   │
│   └── P0002_홍길동_19800515/
│       ├── result_20260410.pdf
│       └── meta.json  (선택)
│
└── _system/
    └── patient_index.json
```

## 파일 네이밍 규칙

환자 폴더명:

```text
{환자ID}_{이름}_{생년월일}
```

의료 문서 파일명:

```text
{type}_{YYYYMMDD}.pdf
```

예시:

```text
result_20260421.pdf
chart_20260421.pdf
image_20260421.pdf
```

문서 타입:

| type   | 의미              |
| ------ | --------------- |
| result | 검사결과지           |
| chart  | 진료기록부           |
| image  | 영상 판독 문서, 향후 확장 |

최신 문서는 **Google Drive 수정 시간 기준이 아니라 파일명 날짜 suffix 기준**으로 판단한다.

## patient_index.json 역할

`_system/patient_index.json`은 환자 인증과 환자 폴더 조회의 기준.

예시:

```json
{
  "patients": [
    {
      "patient_id": "P0001",
      "name": "손창선",
      "birth": "19461230",
      "folder_name": "P0001_손창선_19461230",
      "kakao_user_ids": ["kakao-user-id-1"]
    }
  ]
}
```

반드시 지킬 것:

* 환자 인증은 반드시 `patient_index.json`을 먼저 조회한다.
* `kakao_user_id`가 `patient_index.json`에 없으면 먼저 인증을 요청한다.
* 이름과 생년월일 인증이 성공하면 해당 `kakao_user_id`를 `patient_index.json`의 `kakao_user_ids`에 저장한다.
* 저장된 `kakao_user_id`는 이후 재접속 시 환자 식별에 바로 사용한다.
* 환자 폴더는 직접 조합하지 말고 `folder_name` 값을 사용한다.

---

## meta.json 역할

각 환자 폴더의 `meta.json`은 선택 사항이다.

`meta.json`이 있으면 환자의 최신 진료일, 보유 문서 목록, 최신 기록 요약을 담는 캐시 역할을 한다.
`meta.json`이 없으면 서버는 환자 폴더의 실제 PDF 파일 목록으로 동일한 정보를 계산한다.

예시:

```json
{
  "patient_id": "P0001",
  "name": "손창선",
  "birth": "19461230",
  "latest_visit_date": "20260421",
  "files": [
    {
      "type": "result",
      "date": "20260421",
      "filename": "result_20260421.pdf",
      "description": "검사결과지"
    },
    {
      "type": "chart",
      "date": "20260421",
      "filename": "chart_20260421.pdf",
      "description": "진료기록부"
    }
  ],
  "latest_summary": {
    "date": "20260421",
    "title": "2026년 4월 21일 진료 및 검사 기록",
    "description": "최근 검사결과지와 진료기록부가 등록되어 있습니다."
  }
}
```

## 반드시 지켜야 할 환자 조회 순서

환자 관련 기능은 반드시 아래 순서로 구현한다.

```text
kakao_user_id 추출
→ 인증 세션 확인
→ 인증 세션이 없으면 patient_index.json 에서 kakao_user_id 조회
→ 매핑이 있으면 환자 식별
→ 매핑이 없으면 인사 + 인증 안내
→ 인증 입력이면 patient_index.json 조회
→ 이름과 생년월일로 환자 확인
→ 인증 성공 시 kakao_user_id 저장
→ 인증 성공 시 세션 저장
→ folder_name으로 환자 폴더 조회
→ meta.json 조회 시도
→ 환자 폴더 내 파일 목록 조회
→ meta.json이 없으면 실제 파일 목록으로 최신 정보 계산
→ 최신 result/chart 선택
→ Gemini API에 문서와 질문 전달
```

이 순서를 바꾸지 않는다.

## 최신 진단기록 선택 규칙

- 환자 폴더 내 파일 목록을 조회한다.
- `{type}_{YYYYMMDD}.pdf` 규칙을 따르는 파일만 사용한다.
- 기본 문서는 최신 `result`, 최신 `chart`이다.
- 같은 type의 문서가 여러 개 있으면 `YYYYMMDD`가 가장 큰 파일을 선택한다.
- `image` 문서는 기본 질의에서는 제외하고 향후 확장용으로 둔다.
- `meta.json`과 실제 파일 목록이 다르면 실제 파일 목록을 우선한다.
- `meta.json`이 없으면 실제 파일 목록으로 `latest_visit_date`, `files`, `latest_summary`를 계산한다.
- 진단기록이나 차트가 갱신되면 이전 파일 대신 최신 파일을 사용한다.

## Gemini 답변 원칙

Gemini는 반드시 제공된 환자 PDF 문서에 근거해서만 답변한다.

금지:

* 문서에 없는 내용 추측
* 진단 확정
* 처방 변경 제안
* 약 복용 중단 권유
* 응급 여부 단정
* “괜찮습니다”, “문제 없습니다”처럼 단정적으로 안심시키기

답변에는 가능하면 아래 내용을 포함한다.

* 핵심 답변
* 근거 문서명
* 문서 날짜
* 관련 검사명 또는 진료기록
* 주의할 점
* 의료진에게 확인하면 좋은 질문

## 개인정보 보호 원칙

반드시 지킬 것:

* 환자 정보와 의료 문서 내용을 로그에 출력하지 않는다.
* Gemini 프롬프트 전체를 로그에 출력하지 않는다.
* PDF 원문 내용을 로그에 남기지 않는다.
* `.env`를 Git에 커밋하지 않는다.
* 서비스 계정 키를 Git에 커밋하지 않는다.
* 다른 환자의 PDF를 Gemini에 전달하지 않는다.

## 카카오 응답 형식

기본 응답은 `simpleText`를 사용한다.

```json
{
  "version": "2.0",
  "template": {
    "outputs": [
      {
        "simpleText": {
          "text": "답변 내용"
        }
      }
    ]
  }
}
```

오류가 발생해도 내부 에러를 노출하지 않는다.

## 로컬 개발

```bash
uvicorn app.main:app --reload --port 8000
```

```bash
ngrok http 8000
```

카카오 오픈빌더 Skill URL:

```text
https://{ngrok-domain}/kakao/skill
```

## 배포

PoC 배포는 GCP Cloud Run을 사용한다.

```text
Cloud Run HTTPS URL → 카카오 오픈빌더 Skill URL 등록
```

## 구현 금지 사항

* `patient_index.json` 확인 없이 환자 폴더부터 검색하지 않는다.
* `patient_index.json`에 없는 환자를 인증된 환자로 처리하지 않는다.
* 이름/생년월일 확인 없이 새로운 `kakao_user_id`를 환자에 저장하지 않는다.
* 실제 파일 목록 확인 없이 `meta.json`만 믿고 최신 기록을 판단하지 않는다.
* Google Drive 수정 시간으로 최신 문서를 판단하지 않는다.
* 파일명 규칙에 맞지 않는 PDF를 Gemini에 전달하지 않는다.
* 여러 환자 폴더를 동시에 검색해 답변하지 않는다.
* 다른 환자의 문서를 사용하지 않는다.
* 의료적 판단을 단정하지 않는다.
