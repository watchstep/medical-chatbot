from __future__ import annotations

import unittest

from app.services.cloud_tasks import normalize_task_id


class CloudTasksUtilTest(unittest.TestCase):
    def test_normalize_task_id_keeps_safe_characters(self) -> None:
        self.assertEqual(normalize_task_id("callback-job-JOB_123"), "callback-job-JOB_123")

    def test_normalize_task_id_replaces_unsafe_characters(self) -> None:
        self.assertEqual(normalize_task_id(" callback/job 123 "), "callback-job-123")

    def test_normalize_task_id_rejects_empty_value(self) -> None:
        with self.assertRaises(ValueError):
            normalize_task_id(" /// ")


if __name__ == "__main__":
    unittest.main()
