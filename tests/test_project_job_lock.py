import json
import os
import tempfile
import threading
import subprocess
import sys
import unittest
from unittest import mock

import project_job_lock


class ProjectJobLockTests(unittest.TestCase):
    def test_stale_owner_snapshot_cannot_remove_new_live_owner(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, project_job_lock.LOCK_FILENAME)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"pid": 99999999, "task": "旧任务", "token": "stale"}, handle)
            first = project_job_lock.ProjectJobLock(root)
            second = project_job_lock.ProjectJobLock(root)
            read_started, continue_first = threading.Event(), threading.Event()
            original_read = project_job_lock.ProjectJobLock._read_owner
            result = []

            def paused_read(lock):
                owner = original_read(lock)
                if lock is first:
                    read_started.set()
                    if not continue_first.wait(5):
                        raise RuntimeError("test scheduling timeout")
                return owner

            with mock.patch.object(project_job_lock.ProjectJobLock, "_read_owner", paused_read):
                worker = threading.Thread(target=lambda: result.append(first.acquire("任务A")))
                worker.start()
                try:
                    self.assertTrue(read_started.wait(5))
                    second_result = second.acquire("任务B")
                finally:
                    continue_first.set()
                    worker.join(5)
            try:
                self.assertFalse(worker.is_alive())
                self.assertTrue(result[0][0])
                self.assertFalse(second_result[0])
                self.assertEqual("任务A", original_read(first)["task"])
            finally:
                first.release()
                second.release()

    def test_process_exit_releases_os_guard_and_stale_json_is_recovered(self):
        with tempfile.TemporaryDirectory() as root:
            child = subprocess.Popen([
                sys.executable, "-B", "-c",
                "import os,sys,project_job_lock; "
                "project_job_lock.GUARD_DIR=sys.argv[2]; "
                "lock=project_job_lock.ProjectJobLock(sys.argv[1]); "
                "ok,_=lock.acquire('crashed'); os._exit(0 if ok else 2)", root,
                str(project_job_lock.GUARD_DIR),
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
               cwd=os.path.dirname(project_job_lock.__file__))
            child.communicate(timeout=10)
            self.assertEqual(0, child.returncode)
            self.assertFalse(project_job_lock._pid_is_alive(child.pid))
            lock = project_job_lock.ProjectJobLock(root)
            try:
                self.assertTrue(lock.acquire("恢复任务")[0])
            finally:
                lock.release()

    def test_owner_write_failure_releases_guard(self):
        with tempfile.TemporaryDirectory() as root:
            failed = project_job_lock.ProjectJobLock(root)
            with mock.patch("project_job_lock.json.dump", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    failed.acquire("失败任务")
            recovered = project_job_lock.ProjectJobLock(root)
            try:
                self.assertTrue(recovered.acquire("新任务")[0])
            finally:
                recovered.release()

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
