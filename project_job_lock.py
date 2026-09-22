# -*- coding: utf-8 -*-
"""Small cross-process lock protecting one novel project from concurrent jobs."""

import ctypes
import json
import os
import time
import uuid


LOCK_FILENAME = ".novel_gui_job.lock"


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
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class ProjectJobLock:
    def __init__(self, project_dir):
        self.project_dir = os.path.abspath(project_dir)
        self.path = os.path.join(self.project_dir, LOCK_FILENAME)
        self.token = ""
    def _read_owner(self):
        try:
            with open(self.path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def acquire(self, task_name):
        os.makedirs(self.project_dir, exist_ok=True)
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
        if not self.token:
            return
        owner = self._read_owner()
        if owner.get("token") == self.token:
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
        self.token = ""
