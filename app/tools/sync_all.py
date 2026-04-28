from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from app.services.drive import DriveLookupError
from app.services.gemini_qa import GeminiQaError
from app.tools.sync_document_registry import run_sync as run_document_registry_sync
from app.tools.sync_patient_index import run_sync as run_patient_index_sync


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run patient_index sync first, then Gemini patient store/document_registry sync."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist both sync results back to Google Drive.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the combined sync result as JSON.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        patient_index_payload = run_patient_index_sync(apply=args.apply)
        document_registry_payload = run_document_registry_sync(apply=args.apply)
    except (DriveLookupError, GeminiQaError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    payload = {
        "applied": args.apply,
        "patient_index_sync": patient_index_payload,
        "document_registry_sync": document_registry_payload,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_summary(payload))
    return 0


def format_summary(payload: dict[str, object]) -> str:
    patient_index_sync = payload["patient_index_sync"]
    document_registry_sync = payload["document_registry_sync"]
    return "\n".join(
        [
            "sync_all summary",
            f"applied: {'yes' if payload['applied'] else 'no'}",
            f"patient_index added: {len(patient_index_sync['added'])}",
            f"patient_index updated: {len(patient_index_sync['updated'])}",
            f"document_registry ready_count: {document_registry_sync['ready_count']}",
            f"document_registry failed_count: {document_registry_sync['failed_count']}",
            f"document_registry skipped_count: {document_registry_sync['skipped_count']}",
            f"document_registry imported_count: {document_registry_sync['imported_count']}",
            f"document_registry deleted_count: {document_registry_sync['deleted_count']}",
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
