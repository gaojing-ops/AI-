# -*- coding: utf-8 -*-
"""Small cross-process lock protecting one novel project from concurrent jobs."""

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import tempfile
import time
import uuid


LOCK_FILENAME = ".novel_gui_job.lock"
GUARD_FILENAME = LOCK_FILENAME + ".guard"
GUARD_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
                         "NovelTool", "job_locks")


def _pid_is_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied is not proof of death.
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259  # STILL_ACTIVE; an open handle alone is insufficient.
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class ProjectJobLock:
    def __init__(self, project_dir):
        self.project_dir = os.path.abspath(project_dir)
        self.path = os.path.join(self.project_dir, LOCK_FILENAME)
        self.token = ""
        self._guard_handle = None

    def _release_guard(self):
        handle, self._guard_handle = self._guard_handle, None
        if handle is not None:
            # Closing the descriptor releases its OS lock, including after a
            # process crash. Never unlink the guard: all contenders must open
            # the same file identity.
            handle.close()

    def _acquire_guard(self):
        # Runtime locks live outside manuscripts: refusing an operation must
        # leave the project tree unchanged. The name contains only a path hash.
        identity = os.path.normcase(os.path.realpath(self.project_dir))
        name = hashlib.sha256(identity.encode("utf-8")).hexdigest() + ".lock"
        os.makedirs(GUARD_DIR, exist_ok=True)
        fd = os.open(os.path.join(GUARD_DIR, name), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._guard_handle = os.fdopen(fd, "r+b", buffering=0)
        except Exception:
            os.close(fd)
            raise
    def _read_owner(self):
        try:
            with open(self.path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def acquire(self, task_name):
        os.makedirs(self.project_dir, exist_ok=True)
        if self._guard_handle is not None:
            return False, "当前实例已持有项目任务锁。"
        try:
            self._acquire_guard()
        except OSError:
            owner = self._read_owner()
            label = owner.get("task") or "未知任务"
            return False, f"该项目已有任务正在运行：{label}，或任务锁不可写。"
        try:
            result = self._acquire_owner(task_name)
            if not result[0]:
                self._release_guard()
            return result
        except Exception:
            self._release_guard()
            raise

    def _acquire_owner(self, task_name):
        # The OS guard is held for the entire job, so stale-owner removal and
        # replacement cannot race with another cooperating process.
        for _ in range(2):
            token = uuid.uuid4().hex
            payload = {
                "pid": os.getpid(),
                "task": str(task_name or "后台任务"),
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "token": token,
            }
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            except FileExistsError:
                owner = self._read_owner()
                if owner and _pid_is_alive(owner.get("pid")):
                    label = owner.get("task") or "未知任务"
                    started = owner.get("started_at") or "未知时间"
                    return False, f"该项目已有任务正在运行：{label}（{started}）"
                try:
                    os.remove(self.path)
                except OSError:
                    return False, "项目任务锁存在且无法清理，请关闭其他工具实例后重试。"
                continue
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
            except Exception:
                try:
                    os.remove(self.path)
                except OSError:
                    pass
                raise
            self.token = token
            return True, ""
        return False, "无法取得项目任务锁。"

    def release(self):
        try:
            if self.token:
                owner = self._read_owner()
                if owner.get("token") == self.token:
                    try:
                        os.remove(self.path)
                    except FileNotFoundError:
                        pass
        finally:
            self.token = ""
            self._release_guard()
