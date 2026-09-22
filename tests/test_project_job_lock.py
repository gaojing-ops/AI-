import json
import os
import tempfile
import unittest

import project_job_lock


class ProjectJobLockTests(unittest.TestCase):
    def test_second_live_job_is_rejected_until_first_releases(self):
        with tempfile.TemporaryDirectory() as root:
            first = project_job_lock.ProjectJobLock(root)
            second = project_job_lock.ProjectJobLock(root)
            self.assertTrue(first.acquire("写第一章")[0])
            ok, reason = second.acquire("写第二章")
            self.assertFalse(ok)
            self.assertIn("写第一章", reason)
            first.release()
            self.assertTrue(second.acquire("写第二章")[0])
            second.release()

    def test_stale_lock_is_recovered(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, project_job_lock.LOCK_FILENAME)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"pid": 99999999, "task": "旧任务", "token": "stale"}, f)
            lock = project_job_lock.ProjectJobLock(root)
            self.assertTrue(lock.acquire("新任务")[0])
            lock.release()
