from __future__ import annotations

import logging

import httpx


logger = logging.getLogger(__name__)


def build_simple_text_response(text: str) -> dict:
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
        logger.info("kakao_callback send callback_url=%s", callback_url)
        response = httpx.post(
            callback_url,
            json=payload,
            timeout=self.timeout_seconds,
        )
        logger.info(
            "kakao_callback result status_code=%s callback_url=%s",
            response.status_code,
            callback_url,
        )
        response.raise_for_status()
