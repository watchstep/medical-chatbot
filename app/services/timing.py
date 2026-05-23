from __future__ import annotations

import logging
import time
from typing import Any


def timing_start(logger: logging.Logger, event: str, **fields: Any) -> float:
    logger.info("timing event=%s%s", event, _format_fields(fields))
    return time.perf_counter()


def timing_done(
    logger: logging.Logger,
    event: str,
    started_at: float,
    *,
    status: str,
    **fields: Any,
) -> None:
    duration_ms = int((time.perf_counter() - started_at) * 1000)
    payload = {"status": status, **fields, "duration_ms": duration_ms}
    logger.info("timing event=%s%s", event, _format_fields(payload))


def _format_fields(fields: dict[str, Any]) -> str:
    parts = []
    for key, value in fields.items():
        if value is None or value == "":
            continue
        parts.append(f"{key}={_format_value(value)}")
    return (" " + " ".join(parts)) if parts else ""


def _format_value(value: Any) -> str:
    return str(value).replace("\n", " ").replace("\r", " ")
