from __future__ import annotations

import hashlib


def hash_kakao_user_id(kakao_user_id: str) -> str:
    return "sha256:" + hashlib.sha256(kakao_user_id.encode("utf-8")).hexdigest()
