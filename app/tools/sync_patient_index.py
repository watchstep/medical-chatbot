from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from app.config import get_settings
from app.services.drive import DriveLookupError, build_default_drive_service


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sync Google Drive patient folders into patient_index.json.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the synced patient_index.json back to Google Drive.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the sync result as JSON.",
    )
    return parser


def run_sync(*, apply: bool) -> dict[str, object]:
    settings = get_settings()
    drive_service = build_default_drive_service(settings)
    patient_index, patient_index_file_id = drive_service.load_patient_index_with_file_id()
    result = drive_service.sync_patient_index()

    if apply:
        drive_service.persist_patient_index(
            patient_index_file_id=patient_index_file_id,
            patient_index=result.patient_index,
        )

    payload = result.to_dict()
    payload["applied"] = apply
    payload["changed"] = bool(result.added or result.updated)
    payload["existing_count"] = len(patient_index.patients)
    payload["synced_count"] = len(result.patient_index.patients)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        payload = run_sync(apply=args.apply)
    except DriveLookupError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_summary(payload))

    return 0


def format_summary(payload: dict[str, object]) -> str:
    lines = [
        "patient_index sync summary",
        f"applied: {'yes' if payload['applied'] else 'no'}",
        f"existing_count: {payload['existing_count']}",
        f"synced_count: {payload['synced_count']}",
        f"added: {len(payload['added'])}",
        f"updated: {len(payload['updated'])}",
        f"unchanged: {len(payload['unchanged'])}",
        f"skipped: {len(payload['skipped'])}",
        f"missing_in_drive: {len(payload['missing_in_drive'])}",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
