from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MedicalWikiCategoryDefinition:
    category: str
    label: str
    purpose: str
    typical_documents: tuple[str, ...]
    routing_hints: tuple[str, ...]
    exclusions: tuple[str, ...]


MEDICAL_WIKI_CATEGORY_DEFINITIONS: dict[str, MedicalWikiCategoryDefinition] = {
    "health_checkup": MedicalWikiCategoryDefinition(
        category="health_checkup",
        label="건강검진 기록",
        purpose="정기 또는 종합 건강검진 결과 문서",
        typical_documents=("건강검진 결과지", "종합검진 결과표", "검진 소견서"),
        routing_hints=("건강검진", "검진 결과", "종합소견", "검사 항목"),
        exclusions=("처방 복용법", "영상검사 판독", "입퇴원 경과"),
    ),
    "lab_result": MedicalWikiCategoryDefinition(
        category="lab_result",
        label="검사 결과",
        purpose="혈액, 소변, 기능검사 등 검사 결과 중심 문서",
        typical_documents=("혈액검사 결과지", "소변검사 결과지", "검사 결과표"),
        routing_hints=("혈액검사", "소변검사", "혈당", "HbA1c", "eGFR", "간기능", "신장기능"),
        exclusions=("처방 내역", "진단서 발급 내용", "진료 의뢰 내용"),
    ),
    "prescription": MedicalWikiCategoryDefinition(
        category="prescription",
        label="처방 기록",
        purpose="처방약, 복용 기록, 투약 내역 중심 문서",
        typical_documents=("처방전", "약 처방 내역", "투약 기록"),
        routing_hints=("처방", "복용약", "약물 기록", "혈압약", "당뇨약", "고지혈증약"),
        exclusions=("검사 수치", "영상 판독", "건강검진 종합소견"),
    ),
    "doctor_note": MedicalWikiCategoryDefinition(
        category="doctor_note",
        label="진료 기록",
        purpose="외래, 진료 경과, 의사 소견 중심 문서",
        typical_documents=("외래 진료기록", "진료 차트", "의사 소견 기록"),
        routing_hints=("진료기록", "외래진료", "진단명", "상병", "증상", "추적관찰"),
        exclusions=("비용 문의", "처방 용량", "검사 결과표만 확인하는 질문"),
    ),
    "diagnosis_certificate": MedicalWikiCategoryDefinition(
        category="diagnosis_certificate",
        label="진단서",
        purpose="진단서, 소견서, 증명서 성격의 문서",
        typical_documents=("진단서", "소견서", "진료확인서"),
        routing_hints=("진단서", "진단명", "발급 문서", "의사 소견"),
        exclusions=("검사 수치 상세", "처방 복용 내역", "영상검사 세부 판독"),
    ),
    "imaging_report": MedicalWikiCategoryDefinition(
        category="imaging_report",
        label="영상검사 결과",
        purpose="X-ray, CT, MRI, 초음파 등 영상검사 판독 문서",
        typical_documents=("영상검사 판독지", "CT 판독", "MRI 판독", "초음파 결과"),
        routing_hints=("영상검사", "흉부방사선", "X-ray", "CT", "MRI", "초음파", "영상소견"),
        exclusions=("혈액검사 수치", "처방 복용법", "건강검진 전체 요약"),
    ),
    "discharge_summary": MedicalWikiCategoryDefinition(
        category="discharge_summary",
        label="퇴원 요약",
        purpose="입원 경과와 퇴원 시 요약을 담은 문서",
        typical_documents=("퇴원 요약지", "입원 경과 요약", "퇴원 기록"),
        routing_hints=("입원", "퇴원", "입원 경과", "퇴원 소견", "퇴원약"),
        exclusions=("외래 단일 방문", "건강검진", "단일 검사 결과표"),
    ),
    "referral": MedicalWikiCategoryDefinition(
        category="referral",
        label="진료 의뢰서",
        purpose="타 의료기관 의뢰와 전달 정보를 담은 문서",
        typical_documents=("진료 의뢰서", "회송서", "전원 의뢰서"),
        routing_hints=("진료 의뢰", "전원", "회송", "의뢰 내용"),
        exclusions=("검사 수치 상세", "처방 복용법", "비용 문의"),
    ),
    "mixed_medical_record": MedicalWikiCategoryDefinition(
        category="mixed_medical_record",
        label="진료 및 검사 기록",
        purpose="진료기록, 검사결과, 처방 등 여러 유형이 섞인 문서",
        typical_documents=("통합 진료 기록", "진료 및 검사 묶음", "복합 의료 기록"),
        routing_hints=("진료기록", "검사결과", "처방내역", "진단명", "활력징후"),
        exclusions=("단일 문서 유형이 명확한 경우", "비의료 질문", "비용 문의"),
    ),
    "unknown": MedicalWikiCategoryDefinition(
        category="unknown",
        label="의료 문서",
        purpose="문서 유형을 안전하게 특정하기 어려운 의료 문서",
        typical_documents=("분류 불명 의료 문서",),
        routing_hints=("의료 문서", "기록 확인"),
        exclusions=("명확한 다른 category에 해당하는 경우", "비의료 질문"),
    ),
}

ALLOWED_MEDICAL_WIKI_CATEGORIES = tuple(MEDICAL_WIKI_CATEGORY_DEFINITIONS.keys())


def medical_wiki_category_label(category: str | None) -> str:
    normalized = (category or "").strip()
    if not normalized:
        return MEDICAL_WIKI_CATEGORY_DEFINITIONS["unknown"].label
    definition = MEDICAL_WIKI_CATEGORY_DEFINITIONS.get(normalized)
    return definition.label if definition is not None else normalized


def build_category_definitions_text() -> str:
    lines: list[str] = []
    for definition in MEDICAL_WIKI_CATEGORY_DEFINITIONS.values():
        lines.extend(
            [
                f"- {definition.category}: {definition.label}",
                f"  - 의미: {definition.purpose}",
                f"  - 대표 문서: {', '.join(definition.typical_documents)}",
                f"  - 선택 힌트: {', '.join(definition.routing_hints)}",
                f"  - 제외 힌트: {', '.join(definition.exclusions)}",
            ]
        )
    return "\n".join(lines)
