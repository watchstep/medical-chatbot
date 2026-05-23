from __future__ import annotations

import hashlib
import logging

import httpx


logger = logging.getLogger(__name__)


def _hash_callback_url(callback_url: str) -> str:
    return hashlib.sha256(callback_url.encode("utf-8")).hexdigest()[:16]


def build_simple_text_response(text: str) -> dict:
    # Kakao OpenBuilder가 Callback 설정에 등록된 대기 메시지를 표시한다.
    # 여기서 simpleText를 함께 반환하지 않는다.
    # 최종 답변은 callback_url로 1회 전송한다.
    # 🩺  의료 문서를 확인하고 답변을 준비하고 있어요.
    # 답변까지 최대 1분 정도 걸릴 수 있습니다.
    return {
        "version": "2.0",
        "template": {
            "outputs": [
                {
                    "simpleText": {
                        "text": text,
                    }
                }
            ]
        },
    }


def build_callback_ack_response() -> dict:
    return {
        "version": "2.0",
        "useCallback": True,
    }


class KakaoCallbackService:
    def __init__(self, *, timeout_seconds: float = 10.0):
        self.timeout_seconds = timeout_seconds

    def send_text_response(self, *, callback_url: str, text: str) -> None:
        payload = build_simple_text_response(text)
        callback_url_hash = _hash_callback_url(callback_url)
        logger.info("kakao_callback send callback_url_hash=%s", callback_url_hash)
        response = httpx.post(
            callback_url,
            json=payload,
            timeout=self.timeout_seconds,
        )
        logger.info(
            "kakao_callback result status_code=%s callback_url_hash=%s",
            response.status_code,
            callback_url_hash,
        )
        response.raise_for_status()