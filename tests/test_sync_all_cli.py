from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from app.tools import sync_all


class SyncAllCliTest(unittest.TestCase):
    def test_cli_runs_both_sync_steps(self) -> None:
        stdout = io.StringIO()
        with patch.object(
            sync_all,
            "run_patient_index_sync",
            return_value={
                "applied": True,
                "added": [{"patient_id": "P0003"}],
                "updated": [],
                "unchanged": [],
                "skipped": [],
                "missing_in_drive": [],
            },
        ) as patient_sync_mock, patch.object(
            sync_all,
            "run_document_registry_sync",
            return_value={
                "applied": True,
                "ready_count": 4,
                "failed_count": 0,
                "skipped_count": 1,
                "imported_count": 3,
                "deleted_count": 1,
            },
        ) as registry_sync_mock, redirect_stdout(stdout):
            exit_code = sync_all.main(["--apply", "--json"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["applied"])
        self.assertEqual(payload["patient_index_sync"]["added"][0]["patient_id"], "P0003")
        self.assertEqual(payload["document_registry_sync"]["ready_count"], 4)
        patient_sync_mock.assert_called_once_with(apply=True)
        registry_sync_mock.assert_called_once_with(apply=True)


if __name__ == "__main__":
    unittest.main()
