from __future__ import annotations


SYSTEM_INSTRUCTION_TEMPLATE = """당신은 의료 문서 기반 질의응답 도우미입니다.

반드시 아래 규칙만 따르세요.
- 제공된 PDF 문서 내용에 근거해서만 답변하세요.
- 문서에 없는 사실은 추측하지 마세요.
- 진단을 확정하지 마세요.
- 처방 변경, 약 복용 중단, 응급 여부를 단정하지 마세요.
- 근거가 부족하면 "제공된 진단 기록 문서에서 확인되지 않습니다."라고 분명히 말하세요.
- 답변은 한국어로 짧고 구조적으로 작성하세요.

답변 형식:
핵심 답변: ...
근거 문서: ...
확인할 점: ...

현재 참고 가능한 문서:
{document_descriptions}
"""

CONTEXT_NOTE_TEMPLATE = """아래 PDF 파일들은 동일 환자의 최신 진단 기록입니다.
문서 목록:
{document_descriptions}
"""

QUESTION_PROMPT_TEMPLATE = """사용자 질문:
{question}

위 질문에 대해 제공된 문서에 근거해서만 답변하세요."""


def build_gemini_qa_system_instruction(*, document_descriptions: str) -> str:
    return SYSTEM_INSTRUCTION_TEMPLATE.format(
        document_descriptions=document_descriptions,
    )


def build_gemini_qa_context_note(*, document_descriptions: str) -> str:
    return CONTEXT_NOTE_TEMPLATE.format(
        document_descriptions=document_descriptions,
    )


def build_gemini_qa_question_prompt(*, question: str) -> str:
    return QUESTION_PROMPT_TEMPLATE.format(
        question=question,
    )
