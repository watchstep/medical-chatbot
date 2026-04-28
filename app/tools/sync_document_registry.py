from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from app.config import get_settings
from app.services.document_sync import DocumentRegistrySyncService
from app.services.drive import DriveLookupError, build_default_drive_service
from app.services.gemini_qa import (
    GeminiQaError,
    build_default_gemini_patient_store_sync_service,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync Google Drive patient PDFs into document_registry.json and Gemini patient stores.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the synced document_registry.json back to Google Drive.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the sync result as JSON.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    settings = get_settings()
    try:
        payload = run_sync(apply=args.apply)
    except (DriveLookupError, GeminiQaError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_summary(payload))
    return 0


def run_sync(*, apply: bool) -> dict[str, object]:
    settings = get_settings()
    drive_service = build_default_drive_service(settings)
    gemini_store_sync_service = build_default_gemini_patient_store_sync_service(settings)
    sync_service = DocumentRegistrySyncService(
        settings=settings,
        drive_service=drive_service,
        gemini_store_sync_service=gemini_store_sync_service,
    )
    result = sync_service.sync_document_registry()

    document_registry_file_id = result.document_registry_file_id
    if apply:
        document_registry_file_id = drive_service.persist_document_registry(
            document_registry_file_id=result.document_registry_file_id,
            document_registry=result.document_registry,
        )

    payload = result.to_dict()
    payload["applied"] = apply
    payload["document_registry_file_id"] = document_registry_file_id
    return payload


def format_summary(payload: dict[str, object]) -> str:
    return "\n".join(
        [
            "document_registry sync summary",
            f"applied: {'yes' if payload['applied'] else 'no'}",
            f"ready_count: {payload['ready_count']}",
            f"failed_count: {payload['failed_count']}",
            f"skipped_count: {payload['skipped_count']}",
            f"imported_count: {payload['imported_count']}",
            f"deleted_count: {payload['deleted_count']}",
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
