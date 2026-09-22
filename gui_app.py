# -*- coding: utf-8 -*-
"""
通用长篇小说一键托管工具 GUI 桌面版
==================================
功能：写新章 / 续写 / 一键托管批量生成 / 伏笔追踪 / 历史回看 / 自动保存 / 记忆压缩
      健康检查 / 实体面板 / 回滚 / 批量控制台 / 一键导出排版
托管链路：生成 -> 本地快检 -> 质检(硬伤优先) -> 设定总校 -> 跨章检 -> 保存 -> 记忆维护
"""
import os
import sys

# The clean distribution keeps third-party wheels in a project-local folder
# so the user does not have to install Python packages manually.  Bootstrap
# that folder before importing httpx/openai.  Frozen builds already bundle
# their own dependencies and must keep using the frozen import path.
if not getattr(sys, "frozen", False):
    _LOCAL_PACKAGES = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        ".tools",
        "python-packages",
    )
    if os.path.isdir(_LOCAL_PACKAGES) and _LOCAL_PACKAGES not in sys.path:
        sys.path.insert(0, _LOCAL_PACKAGES)

import re
import json
import hashlib
import glob
import time
import math
import copy
import shutil
import threading
import tkinter as tk
import httpx
from tkinter import ttk, scrolledtext, messagebox, filedialog, simpledialog
from datetime import datetime
from openai import OpenAI

try:
    import tiktoken
except Exception:
    tiktoken = None

# 模型预设（仅存储默认配置，Key 从 config.json 加载）
PROVIDER_DEEPSEEK = "deepseek"
PROVIDER_CODEX_SOL = "codex_sol"
CODEX_SOL_MODEL = "gpt-5.6-sol"

MODEL_PRESETS = {
    "DeepSeek-V4": {
        "provider": PROVIDER_DEEPSEEK,
        "config_key_field": "api_key",
        "base_url": "https://api.deepseek.com",
        "model_config_field": "model",
        "review_model_config_field": "review_model",
        "model": "deepseek-v4-flash",
        "review_model": "deepseek-v4-pro",
    },
    "Codex GPT-5.6-sol": {
        "provider": PROVIDER_CODEX_SOL,
        "model": CODEX_SOL_MODEL,
        "review_model": CODEX_SOL_MODEL,
    },
}
LEGACY_DEEPSEEK_MODELS = {"deepseek-chat", "deepseek-reasoner"}
PROVIDER_PRESET_NAMES = {
    preset["provider"]: name for name, preset in MODEL_PRESETS.items()
}


class ModelProviderConfigError(RuntimeError):
    """Raised when a project names an unsupported model provider."""


def normalize_model_provider(value, *, missing_defaults_to_deepseek=True):
    """Return a stable provider id; unknown persisted values fail closed."""
    if value is None and missing_defaults_to_deepseek:
        return PROVIDER_DEEPSEEK
    provider = str(value or "").strip().lower()
    if provider not in PROVIDER_PRESET_NAMES:
        raise ModelProviderConfigError(
            f"未知模型提供方：{provider or '空值'}；已停止模型调用"
        )
    return provider


def resolve_preset_model(preset, config, review=False):
    """Resolve generation/review models while migrating retired DeepSeek aliases."""
    default_key = "review_model" if review else "model"
    if preset.get("provider") == PROVIDER_CODEX_SOL:
        # Codex production evidence is valid only for this exact model.  It is
        # intentionally not configurable through project model fields.
        return CODEX_SOL_MODEL
    field_key = "review_model_config_field" if review else "model_config_field"
    field = preset.get(field_key, default_key)
    configured = str((config or {}).get(field) or "").strip()
    if preset.get("base_url") == "https://api.deepseek.com" and configured in LEGACY_DEEPSEEK_MODELS:
        configured = ""
    return configured or preset[default_key]

# ============================================================
# PyInstaller 打包兼容 (Fix Bug4)
# ============================================================
if getattr(sys, 'frozen', False):
    _exe_dir = os.path.dirname(sys.executable)
else:
    _exe_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _exe_dir)

import generator
import rag_engine
import cross_chapter_scanner
import commercial_reviewer
import commercial_planner
import repair_engine
import knowledge_manager
import book_initializer
import continuity_guard
import state_ledger
import state_repair
import temporal_memory
import chapter_commit
import chapter_validator
import deepseek_runtime
try:
    import codex_sol_runtime
except ImportError:
    # A clean DeepSeek-only installation may not have the optional Codex
    # runtime yet.  Selecting Codex will fail closed with an actionable error.
    codex_sol_runtime = None
import narrative_guard
import semantic_consistency_guard
import story_architect
import project_job_lock
import publishing_rules
import project_status
import book_runner
import formal_suffix_rewrite

# 持久化配置集中放入运行目录，避免把临时状态散落在源码根目录。
RUNTIME_DIR = os.path.join(_exe_dir, ".runtime")
LAST_PROJECT_FILE = os.path.join(RUNTIME_DIR, "gui_last_project.json")
LEGACY_LAST_PROJECT_FILE = os.path.join(_exe_dir, "gui_last_project.json")

def load_last_project():
    for state_file in (LAST_PROJECT_FILE, LEGACY_LAST_PROJECT_FILE):
        if not os.path.exists(state_file):
            continue
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("last_project_dir", "")
        except Exception:
            continue
    return ""

def save_last_project(path):
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    with open(LAST_PROJECT_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_project_dir": path}, f, ensure_ascii=False)

def apply_project_dir(project_dir):
    """将 generator 的所有目录指向指定的项目文件夹 (Fix7: 移除 os.chdir)"""
    generator.DIRS = {
        "chars":  os.path.join(project_dir, "characters"),
        "world":  os.path.join(project_dir, "world_building"),
        "plot":   os.path.join(project_dir, "plot"),
        "out":    os.path.join(project_dir, "output"),
        "hist":   os.path.join(project_dir, "history"),
        "logs":   os.path.join(project_dir, "logs"),
        "publish": os.path.join(project_dir, "publish"),
    }
    for d in generator.DIRS.values():
        os.makedirs(d, exist_ok=True)


def _atomic_write_text(path, content):
    """Commit a text file as one checkpoint so crashes cannot leave partial output."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(content or "")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, path)

def _num_to_cn_chapter(n):
    """数字转中文章节号 (1->一, 11->十一, 100->一百)"""
    if n <= 0:
        return str(n)
    digits = '零一二三四五六七八九'
    if n < 10:
        return digits[n]
    if n < 20:
        return '十' + (digits[n % 10] if n % 10 else '')
    if n < 100:
        return digits[n // 10] + '十' + (digits[n % 10] if n % 10 else '')
    if n < 1000:
        result = digits[n // 100] + '百'
        remainder = n % 100
        if remainder == 0:
            return result
        if remainder < 10:
            return result + '零' + digits[remainder]
        return result + _num_to_cn_chapter(remainder)
    if n < 10000:
        result = digits[n // 1000] + '千'
        remainder = n % 1000
        if remainder == 0:
            return result
        if remainder < 100:
            return result + '零' + _num_to_cn_chapter(remainder)
        return result + _num_to_cn_chapter(remainder)
    return str(n)


# ============================================================
# 主应用
# ============================================================
class NovelGeneratorGUI:
    GENERATION_INPUT_TOKEN_BUDGET = 110000
    GENERATION_SYSTEM_CHAR_CAP = 45000
    GENERATION_USER_CHAR_CAP = 14000
    GENERATION_MIN_SYSTEM_CHAR_CAP = 18000
    GENERATION_MIN_USER_CHAR_CAP = 5000
    RAG_DOC_CHAR_LIMIT = 1500

    STORY_VOLUME_RANGES = [
        (1, 50, "第一卷"),
        (51, 100, "第二卷"),
        (101, 150, "第三卷"),
        (151, 200, "第四卷"),
        (201, 236, "第五卷"),
        (237, 273, "第六卷"),
    ]
    DEFAULT_TOOL_RULES = publishing_rules.default_tool_rules()
    HIGH_RISK_WATCH_KEYWORDS = {
        "严重时间线回档", "已死亡角色复活", "核心真相提前坐实",
        "未授权新组织", "未铺垫新Boss", "主角能力规则改写",
    }
    STYLE_RISK_KEYWORDS = {
        "章尾短句堆叠", "章尾破折号过密", "模糊词过密",
        "对白停顿模板过密", "深呼吸模板过密", "突发副词过密",
    }

    def __init__(self, root):
        self.root = root
        self.root.title("一键生成整本小说")
        self.root.geometry("920x720")
        self.root.minsize(760, 600)

        self.config = generator.load_config()
        self.is_generating = False
        self.last_saved_text = ""
        self.current_req = ""
        self.generated_content = ""
        self.project_dir = ""
        self.current_vol = 1
        self.next_chap = 1
        self.filepath = ""
        self.latest_chap = 0
        self.latest_filepath = ""

        # Fix4: threading.Event 代替 bool (线程安全)
        self._stop_event = threading.Event()
        self.is_batch_running = False
        self.batch_log_win = None
        self.batch_log_text = None
        self._tool_rules_cache = None
        self._reveal_rules_cache = None
        self._project_config_error = ""
        self._current_batch_job_id = ""
        self._commercial_review_running = False
        self._initialization_running = False
        self._tool_task_running = False
        self._active_task_name = ""
        self._project_job_lock = None
        self._busy_sensitive_widgets = []
        self._active_preset_name = PROVIDER_PRESET_NAMES[PROVIDER_DEEPSEEK]
        self._batch_selected_chars = set()
        self._batch_selected_world = set()
        self._closing_requested = False
        self._closed = False
        self._model_call_count = 0
        self._model_call_limit = 0
        self._chapter_model_call_count = 0
        self._chapter_model_call_limit = 0
        self._chapter_model_call_number = 0
        self._usage_prompt_tokens = 0
        self._usage_completion_tokens = 0
        self._usage_reasoning_tokens = 0
        self._usage_estimated_cost_cny = 0.0
        self._model_usage_persistence_fault = ""
        self._model_usage_unpersisted_cost_cny = 0.0
        self._deepseek_preflight_cache = {}
        self._provider_preflight_cache = {}
        self._deepseek_direct_mode = False
        self._model_call_lock = threading.Lock()

        self.create_widgets()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # 尝试加载上次的项目文件夹
        last_dir = load_last_project()
        if last_dir and os.path.isdir(last_dir):
            self.switch_project(last_dir)
        else:
            default_project = generator.DEFAULT_PROJECT_DIR
            os.makedirs(default_project, exist_ok=True)
            self.switch_project(default_project)
            self.refresh_knowledge_base()

        self.update_word_count()
        self.root.after(60000, self.auto_save_loop)
        self.root.after(
            1800,
            lambda: self._auto_resume_interrupted_full_book(prompt_user=False),
        )

    # ============================================================
    # 线程安全 UI 辅助方法 (Fix5)
    # ============================================================
    def _ui(self, callback):
        """调度回调到主线程执行"""
        self.root.after(0, callback)

    def _ui_append(self, text):
        """线程安全地向 result_text 追加文本"""
        self.root.after(0, lambda: (
            self.result_text.insert(tk.END, text),
            self.result_text.see(tk.END)
        ))

    def _ui_clear(self, initial_text=""):
        """线程安全地清空 result_text 并可选写入初始文本"""
        def _do():
            self.result_text.delete(1.0, tk.END)
            if initial_text:
                self.result_text.insert(tk.END, initial_text)
        self.root.after(0, _do)

    def _is_busy(self):
        return bool(
            self._active_task_name
            or self.is_batch_running
            or self.is_generating
            or self._commercial_review_running
            or self._initialization_running
            or self._tool_task_running
        )

    def _set_busy_widgets(self, busy):
        state = tk.DISABLED if busy else tk.NORMAL
        for widget in self._busy_sensitive_widgets:
            try:
                widget.config(state=state)
            except (tk.TclError, AttributeError):
                pass
        if hasattr(self, "model_combo"):
            try:
                self.model_combo.config(state="disabled" if busy else "readonly")
            except tk.TclError:
                pass
        if hasattr(self, "btn_stop"):
            self.btn_stop.config(
                state=(
                    tk.NORMAL
                    if busy and (self.is_batch_running or self._initialization_running)
                    else tk.DISABLED
                )
            )

    def _begin_project_task(self, task_name, show_error=True):
        """Reserve the current project for one background/API task."""
        if self._is_busy():
            if show_error:
                messagebox.showwarning(
                    "任务正在运行",
                    f"当前任务“{self._active_task_name or '后台处理'}”尚未结束，请稍后再试。",
                )
            return False
        lock = project_job_lock.ProjectJobLock(self.project_dir)
        ok, reason = lock.acquire(task_name)
        if not ok:
            if show_error:
                messagebox.showerror("项目已被占用", reason)
            return False
        self._project_job_lock = lock
        self._active_task_name = str(task_name or "后台任务")
        self._set_busy_widgets(True)
        self._refresh_readiness_status()
        return True

    def _finish_project_task(self):
        """Release the project reservation; safe to call from a worker thread."""
        lock = self._project_job_lock
        self._project_job_lock = None
        if lock is not None:
            lock.release()
        self._active_task_name = ""

        def _restore():
            self._set_busy_widgets(False)
            self._refresh_readiness_status()
            if self._closing_requested:
                self.root.after(100, self._poll_close_when_safe)

        self._ui(_restore)

    def _ensure_batch_log_window(self):
        """进度固定显示在主窗口，不再弹出第二个日志窗。"""
        self.batch_log_win = None
        self.batch_log_text = getattr(self, "progress_text", None)

    def _ui_progress_append(self, text, clear=False):
        """所有后台步骤统一写入主窗口的进度页。"""
        def _do():
            target = getattr(self, "progress_text", None) or self.result_text
            if clear:
                target.delete(1.0, tk.END)
            target.insert(tk.END, text)
            target.see(tk.END)

        self.root.after(0, _do)

    def _poll_close_when_safe(self):
        if self._closed:
            return
        if self._is_busy():
            self.root.after(400, self._poll_close_when_safe)
            return
        self._finalize_close()

    def _finalize_close(self):
        if self._closed:
            return
        self._autosave_current_project_draft()
        lock = self._project_job_lock
        self._project_job_lock = None
        if lock is not None:
            lock.release()
        self._closed = True
        self.root.destroy()

    def on_close(self):
        """运行中只请求安全停止；空闲时先保存草稿再退出。"""
        if self._closed:
            return
        if not self._is_busy():
            self._finalize_close()
            return
        if self._closing_requested:
            return
        message = (
            "整本生成正在运行。现在退出会先停止继续开新章，"
            "并在当前安全步骤结束后自动关闭，已完成章节不会丢失。\n\n是否安全退出？"
            if self.is_batch_running else
            "后台准备工作正在运行，无法在文件写到一半时强制关闭。"
            "系统会在当前步骤结束后自动退出。\n\n是否继续？"
        )
        if not messagebox.askyesno("安全退出", message):
            return
        self._closing_requested = True
        if self.is_batch_running or self._initialization_running:
            self.stop_batch()
        self._ui_progress_append("\n[INFO] 已请求安全退出，正在完成当前安全步骤...\n")
        self.root.after(300, self._poll_close_when_safe)

    def _tool_rules_path(self):
        plot_dir = generator.DIRS.get("plot") or os.path.join(self.project_dir, "plot")
        return os.path.join(plot_dir, "tool_rules.json")

    def _load_tool_rules(self, create_if_missing=False):
        """加载项目级托管规则。缺失字段使用 DEFAULT_TOOL_RULES 兜底。"""
        if self._tool_rules_cache is not None:
            return self._tool_rules_cache

        rules = json.loads(json.dumps(self.DEFAULT_TOOL_RULES, ensure_ascii=False))
        path = self._tool_rules_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    custom = json.load(f)
                if isinstance(custom, dict):
                    for key, value in custom.items():
                        rules[key] = value
            except Exception:
                pass
        elif create_if_missing:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(rules, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

        self._tool_rules_cache = rules
        return rules

    def _build_effective_project_rules(self):
        existing = self._load_tool_rules(create_if_missing=False)
        return publishing_rules.build_project_tool_rules(
            project_config=self.config,
            reveal_rules=self._load_reveal_rules() or {},
            existing_rules=existing,
        )

    def _write_generated_project_rules(self):
        """Build platform + theme rules for the active project."""
        rules = self._build_effective_project_rules()
        _atomic_write_text(
            self._tool_rules_path(),
            json.dumps(rules, ensure_ascii=False, indent=2),
        )
        _atomic_write_text(
            os.path.join(generator.DIRS["plot"], "番茄发布与题材规则.txt"),
            publishing_rules.render_rules_document(rules),
        )
        self._tool_rules_cache = None
        self._reveal_rules_cache = None
        generator.reload_project_tool_rules()
        cross_chapter_scanner.reload_project_tool_rules(self.project_dir)
        return rules

    def _project_rules_sync_status(self):
        """Return whether generated rules still match their project sources."""
        reveal_rules = self._load_reveal_rules() or {}
        expected = publishing_rules.project_rules_fingerprint(
            self.config, reveal_rules
        )
        try:
            with open(self._tool_rules_path(), "r", encoding="utf-8-sig") as f:
                disk_rules = json.load(f)
        except Exception:
            return False, "tool_rules.json 缺失或无法读取"
        metadata = disk_rules.get("ruleset_metadata") or {}
        actual = str(metadata.get("project_fingerprint") or "").strip()
        if not actual:
            return False, "tool_rules.json 缺少项目指纹，可能仍是旧规则"
        if actual != expected:
            return False, "项目简介、题材或真相揭示表已变化，发布规则尚未同步"
        return True, "项目发布规则与当前配置同步"

    def rebuild_project_publishing_rules(self):
        if self._is_busy():
            messagebox.showwarning("无法生成规则", "后台任务运行期间不能重建项目规则。")
            return
        if not messagebox.askyesno(
            "生成发布规则",
            "将根据番茄官方发布规范、当前项目题材简介和真相揭示表，"
            "重建 tool_rules.json。\n\n已人工维护的事件模式和旧名映射会保留。是否继续？",
        ):
            return
        try:
            rules = self._write_generated_project_rules()
            self.refresh_knowledge_base()
            self.refresh_status()
            messagebox.showinfo(
                "规则已生成",
                f"平台规则、题材禁区和真相门控已写入。\n"
                f"当前扫描规则：{len(rules.get('scanner_forbidden_terms') or [])}条；"
                f"章节门控：{len(rules.get('chapter_gated_terms') or [])}条。",
            )
        except Exception as exc:
            messagebox.showerror("规则生成失败", str(exc))

    def _batch_state_path(self):
        return os.path.join(generator.DIRS.get("logs", generator.DIRS["out"]), "batch_state.json")

    def _get_latest_chapter_info(self):
        """Narrow progress adapter consumed by the GUI-independent runner."""
        return generator.get_latest_chapter_info()

    def _batch_audit_path(self):
        return os.path.join(generator.DIRS.get("logs", generator.DIRS["out"]), "batch_audit.jsonl")

    def _load_batch_state(self):
        path = self._batch_state_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            # 旧版 Sol 试写重建流程可能已经把正式稿完整写回磁盘，却把
            # “尚未重建”状态遗留在日志里。只针对这个已知的旧阶段做
            # 自校正，正式章节目录始终是进度真相。
            if data.get("stage") == "sol_staging_content_review_passed_needs_formal_rebuild":
                target = int(data.get("target_end") or 0)
                _, _, _, latest_chapter, _ = generator.get_latest_chapter_info()
                if target > 0 and int(latest_chapter or 0) >= target:
                    planned_end = max(
                        int(latest_chapter or 0),
                        int(self.config.get("target_total_chapters", 0) or 0),
                    )
                    data.update({
                        "status": "ready",
                        "mode": "full_book",
                        "target_end": planned_end,
                        "completed_count": int(latest_chapter or 0),
                        "resume_allowed": False,
                        "stage": "official_chapters_verified",
                        "message": (
                            f"已按正式章节目录核对到第{latest_chapter}章；"
                            f"可从第{int(latest_chapter) + 1}章继续。"
                        ),
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    })
                    _atomic_write_text(
                        path, json.dumps(data, ensure_ascii=False, indent=2)
                    )
            return data
        except Exception:
            return {}

    def _auto_resume_interrupted_full_book(self, prompt_user=False):
        """Resume only crash/network-interrupted trial/full-book jobs, never manual stops."""
        if self.is_batch_running or not self.config.get("auto_resume_full_book", False):
            return
        state = self._load_batch_state()
        if state.get("mode") not in {"full_book", "trial"}:
            return
        resumable = state.get("status") == "running" or (
            state.get("status") == "interrupted" and state.get("resume_allowed") is True
        )
        if not resumable:
            return
        if state.get("status") == "interrupted":
            resume_cycles = int(state.get("network_resume_cycles") or 0)
            max_cycles = max(0, int(self.config.get("auto_resume_network_max_cycles", 6) or 0))
            if resume_cycles >= max_cycles:
                self._write_batch_state(
                    status="paused",
                    resume_allowed=False,
                    message=f"网络自动恢复已达到{max_cycles}轮上限，请检查网络后再继续。",
                )
                return
        target_end = int(state.get("target_end") or 0)
        _, next_chap, _, _, _ = generator.get_latest_chapter_info()
        remaining = target_end - next_chap + 1
        if remaining <= 0:
            self._write_batch_state(status="completed", resume_allowed=False)
            return
        failures = [
            desc for desc, status in self._run_health_check_internal(
                future_window=min(remaining, int(self.config.get("health_check_future_chapters", 30) or 30))
            )
            if status == "FAIL"
        ]
        if failures:
            self._write_batch_state(
                status="paused",
                resume_allowed=False,
                message="自动恢复前体检失败：" + "；".join(failures[:5]),
            )
            return
        if prompt_user and not messagebox.askyesno(
            "发现未完成的全本任务",
            f"上次任务停在第 {next_chap - 1} 章，计划写到第 {target_end} 章。\n\n"
            f"继续后还将生成 {remaining} 章并调用当前所选模型。现在恢复吗？",
        ):
            self._write_batch_state(
                status="paused",
                resume_allowed=False,
                message="启动时由用户选择暂不恢复。",
            )
            return
        self._start_batch_job(
            remaining,
            label=(
                "30章试写（自动恢复）"
                if state.get("mode") == "trial"
                else "一键写完整本（自动恢复）"
            ),
            target_end=target_end,
            resume=True,
        )

    def _write_batch_state(self, **updates):
        """把批量托管进度落盘，程序关闭后也能看到停在哪一步。"""
        state = {}
        state_path = self._batch_state_path()
        if os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except Exception:
                state = {}
        if not isinstance(state, dict):
            state = {}
        if updates.get("job_id") and updates["job_id"] != state.get("job_id"):
            # This file is the current job projection, not the batch audit log.
            state = {}
        if updates.get("status") == "running":
            state.pop("finished_at", None)
        state.update(updates)
        state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            os.makedirs(os.path.dirname(state_path), exist_ok=True)
            temp_path = state_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, state_path)
        except Exception:
            pass

    def _append_batch_audit(self, event):
        event = dict(event or {})
        event.setdefault("job_id", self._current_batch_job_id)
        event.setdefault("time", datetime.now().isoformat(timespec="seconds"))
        try:
            os.makedirs(generator.DIRS.get("logs", generator.DIRS["out"]), exist_ok=True)
            with open(self._batch_audit_path(), "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _default_project_config(self):
        return {
            # "simple" keeps deterministic hard gates and low-frequency
            # commercial reviews.  "strict" enables the legacy per-chapter
            # model audit and structured evidence chain.
            "release_mode": "simple",
            "doctor_evidence_enforcement": False,
            "legacy_evidence_exempt_through_chapter": 0,
            "model_provider": PROVIDER_DEEPSEEK,
            "codex_executable": "",
            "codex_timeout_seconds": 900,
            "book_title": "我的新小说",
            "genre_template": "通用空白",
            "book_brief": "",
            "target_total_chapters": 300,
            "volume_ranges": [
                [1, 50, "第一卷"],
                [51, 100, "第二卷"],
                [101, 150, "第三卷"],
                [151, 200, "第四卷"],
                [201, 250, "第五卷"],
                [251, 300, "第六卷"],
            ],
            "chapter_char_min": 3200,
            "chapter_char_target_min": 3500,
            "chapter_char_target_max": 4500,
            "word_count_rewrite_floor": repair_engine.DEFAULT_WORD_COUNT_REWRITE_FLOOR,
            "early_reveal_sanitizer": True,
            "auto_outline_from_volume": True,
            "rolling_outline_batch_size": 10,
            "rolling_outline_trigger_lookahead": 3,
            "auto_repair_max_attempts": repair_engine.DEFAULT_MAX_REPAIR_ATTEMPTS,
            "api_retry_max_attempts": 5,
            "api_retry_backoff_seconds": 3,
            "auto_resume_network_delay_seconds": 60,
            "auto_resume_network_max_cycles": 6,
            "semantic_arc_audit_enabled": True,
            "semantic_arc_audit_interval": 10,
            "semantic_arc_audit_lookback": 20,
            "semantic_arc_audit_fulltext_chars": 52000,
            "semantic_arc_audit_fail_closed": True,
            "narrative_guard_enabled": True,
            "narrative_guard_min_score": 75,
            "narrative_guard_drift_window": 5,
            "narrative_guard_marginal_score": 82,
            "narrative_guard_max_consecutive_marginal": 2,
            "narrative_trigger_markers": [],
            "commercial_review_enabled": True,
            "commercial_review_milestones": [3, 10, 30],
            "commercial_review_interval": 10,
            "commercial_review_volume_end": True,
            "commercial_review_pause_on_fail": True,
            "commercial_review_pause_on_unavailable": True,
            "golden_three_gate_chapter": 3,
            "golden_three_require_explicit_continue": True,
            "commercial_hard_gate_chapters": [3, 30],
            "commercial_hard_gate_require_explicit_continue": True,
            "commercial_review_lookback": 30,
            "commercial_review_fulltext_chars": 120000,
            "revision_debt_enabled": True,
            "revision_debt_horizon": 10,
            "commercial_project_gate_enabled": True,
            "commercial_project_min_score": 78,
            "commercial_project_max_attempts": 2,
            "hold_publish_until_commercial_pass": True,
            "min_review_continue_chars": repair_engine.DEFAULT_MIN_SALVAGE_CHARS,
            "cross_chapter_high_policy": "pause",
            "max_consecutive_review_chapters": 5,
            "canon_guard_enabled": True,
            "strict_outline_contract": True,
            "auto_resume_full_book": True,
            "trial_stop_chapter": 30,
            "trial_continue_confirmed": False,
            "trial_gate_migration_required": False,
            "auto_initialize_knowledge": True,
            "knowledge_writeback_interval": 10,
            "health_check_future_chapters": 30,
            "structured_state_enabled": True,
            "state_delta_max_attempts": 2,
            "state_delta_independent_audit": True,
            "pre_save_continuity_audit_enabled": True,
            "context_trace_enabled": True,
            "state_context_max_chars": 5200,
            "temporal_memory_enabled": True,
            "temporal_memory_max_hits": 24,
            "temporal_memory_max_chars": 2800,
            "subplot_stale_warn_chapters": 8,
            "subplot_stale_fail_chapters": 20,
            "event_cooldown_enabled": True,
            "unattended_cost_confirmed": False,
            "max_model_calls_per_chapter": 16,
            "max_estimated_cost_cny": 100.0,
            "recovered_external_cost_cny": 0.0,
            "recovered_external_cost_evidence": "",
            "recovered_external_cost_evidence_sha256": "",
            "deepseek_preflight_enabled": True,
            "provider_preflight_cache_seconds": 300,
            "deepseek_preflight_cache_seconds": 300,
            "deepseek_generation_thinking": False,
            "deepseek_review_thinking": True,
            "deepseek_review_thinking_min_tokens": 6000,
            "deepseek_review_reasoning_effort": "high",
            "model_usage_ledger_enabled": True,
            "enabled_skills": [],
            "tone_rules": "",
        }

    def _release_mode(self):
        mode = str(
            self.config.get("release_mode", "strict") or "strict"
        ).strip().lower()
        return "simple" if mode == "simple" else "strict"

    def _strict_release_mode(self):
        return self._release_mode() == "strict"

    def _commercial_review_runtime_config(self):
        """Keep stage reviews lightweight and advisory in simple release mode."""
        review_config = dict(self.config)
        if self._strict_release_mode():
            return review_config
        review_config.update({
            # A truthy sentinel avoids the review helper's legacy default
            # fallback while producing no real hard-gate chapter.
            "commercial_hard_gate_chapters": [-1],
            "commercial_hard_gate_require_explicit_continue": False,
            "golden_three_require_explicit_continue": False,
            "commercial_review_volume_end": False,
            "commercial_review_lookback": min(
                10,
                max(
                    3,
                    int(
                        review_config.get("commercial_review_lookback", 10)
                        or 10
                    ),
                ),
            ),
            "commercial_review_fulltext_chars": min(
                40000,
                max(
                    24000,
                    int(
                        review_config.get(
                            "commercial_review_fulltext_chars", 40000
                        )
                        or 40000
                    ),
                ),
            ),
        })
        return review_config

    @staticmethod
    def _commercial_review_is_clear_pass(result):
        result = result or {}
        return bool(
            result
            and not result.get("malformed")
            and not result.get("gate_blocked")
            and str(result.get("status") or "").upper() == "PASS"
            and str(result.get("current") or "").upper() == "PASS"
            and str(result.get("action") or "").upper() == "CONTINUE"
        )

    def _get_volume_ranges(self):
        ranges = self.config.get("volume_ranges") or self.STORY_VOLUME_RANGES
        normalized = []
        for item in ranges:
            try:
                start, end, name = item[0], item[1], item[2]
                normalized.append((int(start), int(end), str(name)))
            except Exception:
                continue
        return normalized or list(self.STORY_VOLUME_RANGES)

    def _get_chapter_char_limits(self):
        return {
            "min": int(self.config.get("chapter_char_min", 3200) or 3200),
            "target_min": int(self.config.get("chapter_char_target_min", 3500) or 3500),
            "target_max": int(self.config.get("chapter_char_target_max", 4500) or 4500),
            "rewrite_floor": int(self.config.get("word_count_rewrite_floor", repair_engine.DEFAULT_WORD_COUNT_REWRITE_FLOOR) or repair_engine.DEFAULT_WORD_COUNT_REWRITE_FLOOR),
        }

    def _get_story_volume_name(self, chap_num):
        for start, end, volume_name in self._get_volume_ranges():
            if start <= chap_num <= end:
                return volume_name
        return None

    def _get_story_volume_end(self, chap_num):
        for start, end, _volume_name in self._get_volume_ranges():
            if start <= chap_num <= end:
                return end
        return 0

    def _get_story_planned_end_chapter(self):
        configured = int(self.config.get("target_total_chapters", 0) or 0)
        return configured or max((end for _, end, _ in self._get_volume_ranges()), default=0)

    def _get_trial_stop_chapter(self):
        planned_end = self._get_story_planned_end_chapter()
        try:
            stop = int(self.config.get("trial_stop_chapter", 30) or 0)
        except (TypeError, ValueError):
            return 0
        return stop if 3 <= stop < planned_end else 0

    def _trial_mode_pending(self):
        return bool(
            self._get_trial_stop_chapter()
            and not self.config.get("trial_continue_confirmed", False)
        )

    def _load_latest_commercial_review(self):
        path = os.path.join(
            self._commercial_review_report_dir(), "latest_commercial_review.json"
        )
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _latest_commercial_review_valid(self, latest, expected_chapters=None):
        if not isinstance(latest, dict) or latest.get("malformed"):
            return False
        try:
            commercial_reviewer.verify_review_receipt(
                latest, model_name=str(latest.get("model") or "")
            )
        except Exception:
            return False
        reviewed = [int(item) for item in (latest.get("reviewed_chapters") or [])]
        expected = [
            int(item)
            for item in (
                expected_chapters
                if expected_chapters is not None
                else (latest.get("expected_chapters") or reviewed)
            )
        ]
        return bool(reviewed and reviewed == expected)

    def _legacy_evidence_exempt_through_chapter(self):
        """Return the sealed legacy prefix that predates current evidence receipts."""
        try:
            boundary = int(
                self.config.get("legacy_evidence_exempt_through_chapter", 0) or 0
            )
        except (TypeError, ValueError):
            boundary = 0
        return max(0, boundary)

    def _chapter_requires_current_evidence(self, chapter):
        return int(chapter or 0) > self._legacy_evidence_exempt_through_chapter()

    def _evidence_ledger_audit_chapter(self, through_chapter):
        """Audit the materialized ledger at the live formal tip, not an old gate."""
        chapters = [
            int(chapter)
            for chapter in self._official_chapter_paths_by_number()
        ]
        return max(chapters, default=max(0, int(through_chapter or 0)))

    @staticmethod
    def _overdue_revision_debts(debt, next_chapter):
        blockers = []
        for item in dict(debt or {}).get("items", []):
            if not isinstance(item, dict) or item.get("status") != "OPEN":
                continue
            try:
                due_by = int(item.get("due_by") or 0)
            except (TypeError, ValueError):
                due_by = 0
            if due_by <= 0 or int(next_chapter or 0) > due_by:
                blockers.append(item)
        return blockers

    def _hard_gate_integrity_issues(self, through_chapter):
        """Deterministic manifest that no model PASS is allowed to override."""
        through = int(through_chapter or 0)
        if through <= 0:
            return ["硬闸门章号无效"]
        issues = []
        official = {
            int(number): path
            for number, path in self._official_chapter_paths_by_number().items()
            if int(number) <= through
        }
        expected = list(range(1, through + 1))
        if sorted(official) != expected:
            missing = [number for number in expected if number not in official]
            issues.append(
                "正式章节覆盖不完整，缺少第"
                + "、".join(str(item) for item in missing[:20])
                + "章"
            )
            return issues

        statuses = self._load_latest_chapter_statuses()
        receipt_dir = os.path.join(
            generator.DIRS["plot"], "runtime", "commit_receipts"
        )
        rows = []
        for chapter in expected:
            path = official[chapter]
            text = generator.read_text_exact(path)
            rows.append((chapter, text))
            digest = state_ledger.chapter_sha256(text)
            status = statuses.get(chapter) or {}
            if status.get("status") != "正式可用":
                issues.append(f"第{chapter}章状态不是正式可用")
            if str(status.get("chapter_sha256") or "") != digest:
                issues.append(f"第{chapter}章状态哈希与正文不一致")
            requires_current_evidence = self._chapter_requires_current_evidence(
                chapter
            )
            if self._strict_release_mode() and requires_current_evidence:
                try:
                    narrative_guard.verify_persisted_audit(
                        generator.DIRS["plot"],
                        chapter,
                        text,
                        expected_audit_id=str(status.get("narrative_audit_id") or ""),
                        expected_review_model=self._narrative_review_model_for_chapter(
                            chapter, status
                        ),
                        min_score=int(
                            self.config.get("narrative_guard_min_score", 75) or 75
                        ),
                    )
                except Exception as exc:
                    issues.append(f"第{chapter}章叙事审计无效：{exc}")
            if not requires_current_evidence:
                continue
            receipt_path = os.path.join(
                receipt_dir, f"chapter_{chapter:04d}_{digest[:12]}.json"
            )
            try:
                with open(receipt_path, "r", encoding="utf-8-sig") as handle:
                    receipt = json.load(handle)
                chapter_commit.verify_saved_chapter(
                    receipt,
                    chapter_path_override=path,
                    plot_dir_override=generator.DIRS["plot"],
                )
                if chapter == through and self._strict_release_mode():
                    lookback = max(
                        3,
                        int(
                            self.config.get("commercial_review_lookback", 30)
                            or 30
                        ),
                    )
                    gate_expected = commercial_reviewer.expected_hard_gate_chapters(
                        through,
                        lookback,
                        self._get_volume_ranges(),
                        target_total_chapters=self._get_story_planned_end_chapter(),
                    )
                    review = receipt.get("commercial_review_result") or {}
                    if not receipt.get("commercial_review_required"):
                        raise RuntimeError("硬闸门提交收据未标记商业审稿必需")
                    if not receipt.get("commercial_hard_gate"):
                        raise RuntimeError("硬闸门提交收据未标记为硬闸门")
                    if [
                        int(item)
                        for item in (
                            receipt.get("commercial_expected_chapters") or []
                        )
                    ] != gate_expected:
                        raise RuntimeError("硬闸门提交收据覆盖区间不正确")
                    if (
                        review.get("malformed")
                        or review.get("gate_blocked")
                        or review.get("status") != "PASS"
                        or review.get("current") != "PASS"
                        or review.get("action") != "CONTINUE"
                    ):
                        raise RuntimeError("硬闸门没有取得明确 PASS/CONTINUE")
                    commercial_reviewer.verify_review_receipt(
                        review,
                        model_name=self._commercial_review_model_for_result(
                            review,
                            recorded_model=receipt.get("commercial_review_model"),
                        ),
                    )
                    chapter_hashes = {
                        str(key): str(value)
                        for key, value in dict(
                            review.get("chapter_hashes") or {}
                        ).items()
                    }
                    for gate_chapter in gate_expected:
                        gate_text = generator.read_text_exact(official[gate_chapter])
                        if chapter_hashes.get(str(gate_chapter)) != state_ledger.chapter_sha256(
                            gate_text
                        ):
                            raise RuntimeError(
                                f"商业审稿正文哈希与第{gate_chapter}章正式稿不一致"
                            )
            except Exception as exc:
                issues.append(f"第{chapter}章正式提交收据无效：{exc}")

        project_root = self.project_dir or os.path.dirname(generator.DIRS["plot"])
        semantic_report = semantic_consistency_guard.scan_chapters(
            project_root, rows
        )
        if not semantic_report.get("passed"):
            issues.append(
                "全量语义一致性未通过："
                + self._semantic_guard_message(semantic_report, limit=6)
            )
        if self._strict_release_mode():
            ledger_tip = self._evidence_ledger_audit_chapter(through)
            ledger = state_ledger.audit_state(generator.DIRS["plot"], ledger_tip)
            if ledger.get("status") != "PASS":
                issues.append("证据正史账本未通过：" + str(ledger.get("message") or ""))
        try:
            debt = commercial_reviewer.load_revision_debt(
                os.path.join(generator.DIRS["plot"], "revision_debt.json")
            )
        except commercial_reviewer.RevisionDebtError as exc:
            issues.append(str(exc))
            return issues[:80]
        if self._strict_release_mode():
            next_chapter = self._evidence_ledger_audit_chapter(through) + 1
            overdue_debts = self._overdue_revision_debts(debt, next_chapter)
            if overdue_debts:
                issues.append("仍有已逾期且未逐项核销的商业修订债务")
        return issues[:80]

    def _trial_gate_ready(self):
        stop = self._get_trial_stop_chapter()
        if not stop:
            return False
        if not self._strict_release_mode():
            # 简单模式以正式正文、状态哈希、提交收据和确定性全量扫描
            # 为准。低频商业审稿是建议，不再成为第31章的外部凭证锁。
            return not self._hard_gate_integrity_issues(stop)
        latest = self._load_latest_commercial_review()
        if not (
            int(latest.get("chapter") or 0) == stop
            and latest.get("status") == "PASS"
            and latest.get("action") == "CONTINUE"
            and not latest.get("gate_blocked")
            and self._latest_commercial_review_valid(
                latest, expected_chapters=list(range(1, stop + 1))
            )
        ):
            return False
        debt_path = os.path.join(generator.DIRS["plot"], "revision_debt.json")
        try:
            debt = commercial_reviewer.load_revision_debt(debt_path)
        except commercial_reviewer.RevisionDebtError:
            return False
        if any(
            item.get("status") == "OPEN"
            and int(item.get("opened_chapter") or 0) < stop
            for item in debt.get("items", [])
        ):
            return False
        status = self._load_latest_chapter_statuses().get(stop) or {}
        if status.get("status") != "正式可用":
            return False
        return not self._hard_gate_integrity_issues(stop)

    def _get_generation_target_end(self):
        planned_end = self._get_story_planned_end_chapter()
        if self._trial_mode_pending():
            return min(planned_end, self._get_trial_stop_chapter())
        return planned_end

    def _extract_volume_outline_from_master(self, volume_name):
        master_path = os.path.join(generator.DIRS["plot"], "全书大纲.txt")
        if not os.path.exists(master_path):
            return ""

        content = generator.read_text_safe(master_path)
        if not content:
            return ""

        lines = content.split("\n")
        capturing = False
        result_lines = []
        volume_header_pattern = re.compile(r'^(?:▌|【)?第.+卷')

        for line in lines:
            stripped = line.strip()
            is_target_header = (
                f"▌{volume_name}" in stripped
                or stripped.startswith(volume_name + "：")
                or stripped.startswith(volume_name + ":")
                or stripped.startswith(f"【{volume_name}：")
                or stripped.startswith(f"【{volume_name}:")
            )
            if is_target_header:
                capturing = True
                result_lines = [line]
                continue
            if capturing:
                if (
                    volume_header_pattern.match(stripped)
                    and "卷" in stripped
                    and volume_name not in stripped
                ):
                    break
                if stripped.startswith("▌") and volume_name not in stripped:
                    break
                result_lines.append(line)

        return "\n".join(result_lines).strip()

    def _get_outline_candidate_files(self, chap_num=None):
        plot_dir = generator.DIRS["plot"]
        candidates = []
        for pattern in (
            "*逐章细纲*.txt", "*逐章细纲*.md",
            "*章节细纲*.txt", "*章节细纲*.md",
            "*章细纲*.txt", "*章细纲*.md",
            "*前10章细纲*.txt", "*前10章细纲*.md",
        ):
            for fpath in glob.glob(os.path.join(plot_dir, pattern)):
                name = os.path.basename(fpath)
                if "模板" in name or "当前卷大纲" in name or "全书大纲" in name:
                    continue
                if os.path.isfile(fpath):
                    candidates.append(fpath)

        # 去重并按相关性排序：优先当前卷相关细纲
        unique_files = []
        seen = set()
        for fpath in candidates:
            norm = os.path.normcase(fpath)
            if norm not in seen:
                unique_files.append(fpath)
                seen.add(norm)

        if chap_num is None:
            return unique_files

        volume_name = self._get_story_volume_name(chap_num) or ""
        return sorted(
            unique_files,
            key=lambda p: (
                0 if volume_name and volume_name in os.path.basename(p) else 1,
                0 if "逐章细纲" in os.path.basename(p) else 1,
                os.path.basename(p),
            )
        )

    # ============================================================
    # 项目管理
    # ============================================================
    def select_project_folder(self):
        if self._is_busy():
            messagebox.showwarning("无法切换项目", "后台任务运行期间不能切换项目，请先停止或等待任务完成。")
            return
        folder = filedialog.askdirectory(title="选择小说项目文件夹")
        if folder:
            self.switch_project(folder)

    def _reset_project_scoped_caches(self):
        self._tool_rules_cache = None
        self._reveal_rules_cache = None
        self._deepseek_preflight_cache = {}
        self._provider_preflight_cache = {}
        if hasattr(self, "_model_call_lock"):
            with self._model_call_lock:
                self._model_call_count = 0
                self._model_call_limit = 0
                self._chapter_model_call_count = 0
                self._chapter_model_call_limit = 0
                self._chapter_model_call_number = 0
                self._usage_prompt_tokens = 0
                self._usage_completion_tokens = 0
                self._usage_reasoning_tokens = 0
                self._usage_estimated_cost_cny = 0.0
                self._model_usage_persistence_fault = ""
                self._model_usage_unpersisted_cost_cny = 0.0
                self._commercial_review_call_active = False
                self._commercial_review_stage_remaining = 0
                self._commercial_review_fallback_call_active = False
                self._commercial_review_fallback_eligible = 0
                self._reader_staging_validation_cache = {}

    def _autosave_current_project_draft(self):
        if not self.project_dir or not hasattr(self, "result_text"):
            return
        current_text = self.result_text.get(1.0, tk.END).strip()
        if not current_text or current_text == self.last_saved_text:
            return
        try:
            _atomic_write_text(
                os.path.join(generator.DIRS["hist"], "autosave_draft.txt"),
                current_text,
            )
            self.last_saved_text = current_text
        except Exception:
            pass

    def switch_project(self, folder):
        if self._is_busy():
            messagebox.showwarning("无法切换项目", "后台任务运行期间不能切换项目，请先停止或等待任务完成。")
            return False
        folder = os.path.abspath(folder)
        self._autosave_current_project_draft()
        self.project_dir = folder
        apply_project_dir(folder)
        save_last_project(folder)
        generator.init_demo_files()
        self._reset_project_scoped_caches()
        self._load_tool_rules(create_if_missing=True)
        # 加载项目级配置 (Feature 2)
        self._load_project_config(folder)
        if hasattr(generator, "reload_project_tool_rules"):
            generator.reload_project_tool_rules()
        if hasattr(cross_chapter_scanner, "reload_project_tool_rules"):
            cross_chapter_scanner.reload_project_tool_rules(folder)
        self.current_vol, self.next_chap, self.filepath, self.latest_chap, self.latest_filepath = generator.get_latest_chapter_info()
        self.refresh_knowledge_base()
        self.refresh_status()
        self.result_text.delete(1.0, tk.END)
        self.reader_text.delete(1.0, tk.END)
        self.reader_lbl.config(text="未加载")
        self.generated_content = ""
        self.current_req = ""
        self.last_saved_text = ""
        self.folder_lbl.config(text=os.path.basename(folder))
        self.root.title(f"一键生成整本小说 - {os.path.basename(folder)}")
        return True

    def _load_project_config(self, folder):
        """加载项目级配置 (Feature 2: 项目级配置)"""
        pconfig_path = os.path.join(folder, "project_config.json")
        defaults = self._default_project_config()
        # Project settings must not leak from the previously opened book.
        self.config.update(defaults)
        self._project_config_error = ""
        if os.path.exists(pconfig_path):
            try:
                with open(pconfig_path, "r", encoding="utf-8") as f:
                    pconfig = json.load(f)
                if not isinstance(pconfig, dict):
                    raise ValueError("project_config.json 顶层必须是对象")
                changed = False
                provider_was_missing = "model_provider" not in pconfig
                raw_provider = pconfig.get("model_provider")
                try:
                    provider = normalize_model_provider(raw_provider)
                except ModelProviderConfigError:
                    # Keep the invalid value visible in runtime state.  Do not
                    # silently route an unknown project through DeepSeek.
                    self.config["model_provider"] = raw_provider
                    self._active_preset_name = ""
                    raise
                if provider_was_missing or pconfig.get("model_provider") != provider:
                    pconfig["model_provider"] = provider
                    changed = True
                self._active_preset_name = PROVIDER_PRESET_NAMES[provider]
                had_trial_confirmation = "trial_continue_confirmed" in pconfig
                for key, value in defaults.items():
                    if key not in pconfig:
                        pconfig[key] = value
                        changed = True
                if not had_trial_confirmation:
                    legacy_latest = 0
                    output_dir = os.path.join(folder, "output")
                    if os.path.isdir(output_dir):
                        for root_dir, dirs, files in os.walk(output_dir):
                            dirs[:] = [name for name in dirs if name != ".backup"]
                            for filename in files:
                                match = re.fullmatch(r"第(\d{1,6})章\.txt", filename)
                                if match:
                                    legacy_latest = max(legacy_latest, int(match.group(1)))
                    pconfig["trial_continue_confirmed"] = False
                    pconfig["trial_gate_migration_required"] = bool(
                        legacy_latest
                        >= int(pconfig.get("trial_stop_chapter", 30) or 30)
                    )
                    changed = True
                if changed:
                    _atomic_write_text(
                        pconfig_path,
                        json.dumps(pconfig, ensure_ascii=False, indent=2),
                    )
                # 隔离字段同步
                for key in (
                    "release_mode", "doctor_evidence_enforcement",
                    "legacy_evidence_exempt_through_chapter",
                    "model_provider", "codex_executable", "codex_timeout_seconds",
                    "model", "review_model", "temperature", "current_volume", "enabled_skills", "tone_rules",
                    "book_title", "genre_template", "book_brief", "target_total_chapters", "volume_ranges",
                    "chapter_char_min", "chapter_char_target_min", "chapter_char_target_max", "word_count_rewrite_floor",
                    "early_reveal_sanitizer",
                    "auto_outline_from_volume", "health_check_future_chapters",
                    "rolling_outline_batch_size", "rolling_outline_trigger_lookahead", "auto_repair_max_attempts",
                    "api_retry_max_attempts", "api_retry_backoff_seconds",
                    "auto_resume_network_delay_seconds", "auto_resume_network_max_cycles",
                    "semantic_arc_audit_enabled", "semantic_arc_audit_interval", "semantic_arc_audit_lookback",
                    "semantic_arc_audit_fulltext_chars", "semantic_arc_audit_fail_closed",
                    "narrative_guard_enabled", "narrative_guard_min_score",
                    "narrative_guard_drift_window", "narrative_guard_marginal_score",
                    "narrative_guard_max_consecutive_marginal", "narrative_trigger_markers",
                    "commercial_review_enabled", "commercial_review_milestones", "commercial_review_interval", "commercial_review_volume_end",
                    "commercial_review_pause_on_fail", "commercial_review_pause_on_unavailable",
                    "golden_three_gate_chapter", "golden_three_require_explicit_continue",
                    "commercial_hard_gate_chapters", "commercial_hard_gate_require_explicit_continue",
                    "commercial_review_lookback", "commercial_review_fulltext_chars",
                    "revision_debt_enabled", "revision_debt_horizon",
                    "commercial_project_gate_enabled", "commercial_project_min_score",
                    "commercial_project_max_attempts", "hold_publish_until_commercial_pass",
                    "min_review_continue_chars", "cross_chapter_high_policy", "max_consecutive_review_chapters",
                    "canon_guard_enabled", "strict_outline_contract", "auto_resume_full_book",
                    "trial_stop_chapter", "trial_continue_confirmed",
                    "trial_gate_migration_required",
                    "auto_initialize_knowledge", "knowledge_writeback_interval",
                    "structured_state_enabled", "state_delta_max_attempts", "state_delta_independent_audit",
                    "pre_save_continuity_audit_enabled", "context_trace_enabled", "state_context_max_chars",
                    "temporal_memory_enabled", "temporal_memory_max_hits", "temporal_memory_max_chars",
                    "subplot_stale_warn_chapters",
                    "subplot_stale_fail_chapters", "event_cooldown_enabled",
                    "unattended_cost_confirmed", "max_model_calls_per_chapter",
                    "max_estimated_cost_cny",
                    "recovered_external_cost_cny",
                    "recovered_external_cost_evidence",
                    "recovered_external_cost_evidence_sha256",
                    "deepseek_preflight_enabled", "provider_preflight_cache_seconds",
                    "deepseek_preflight_cache_seconds",
                    "deepseek_generation_thinking", "deepseek_review_thinking",
                    "deepseek_review_thinking_min_tokens",
                    "deepseek_review_reasoning_effort", "model_usage_ledger_enabled",
                ):
                    if key in pconfig:
                        self.config[key] = pconfig[key]
            except Exception as exc:
                self._project_config_error = f"project_config.json 无法加载：{str(exc)[:160]}"
        else:
            # 不存在则从当前配置创建
            try:
                pconfig = dict(defaults)
                for key in (
                    "model_provider", "codex_executable", "codex_timeout_seconds",
                    "model", "review_model", "temperature", "current_volume",
                    "enabled_skills", "tone_rules",
                ):
                    if key in self.config:
                        pconfig[key] = self.config.get(key)
                provider = normalize_model_provider(pconfig.get("model_provider"))
                pconfig["model_provider"] = provider
                self._active_preset_name = PROVIDER_PRESET_NAMES[provider]
                _atomic_write_text(
                    pconfig_path,
                    json.dumps(pconfig, ensure_ascii=False, indent=2),
                )
                self.config.update(pconfig)
            except Exception as exc:
                self._project_config_error = f"project_config.json 无法创建：{str(exc)[:160]}"

    def _get_runtime_api_key(self):
        return generator.get_runtime_api_key(self.config)

    def _has_saved_api_key(self):
        saved = str(self.config.get("api_key") or "").strip()
        return bool(saved and saved != "YOUR_DEEPSEEK_API_KEY_HERE")

    def _global_config_payload(self):
        defaults = generator.default_config()
        for key in (
            "api_key", "base_url", "model_provider", "codex_executable",
            "codex_timeout_seconds", "model", "review_model", "temperature",
            "max_tokens", "current_volume", "author_name",
        ):
            if key in self.config:
                defaults[key] = self.config[key]
        # 环境变量只用于本次运行，绝不复制进 config.json。
        env_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
        if env_key and defaults.get("api_key") == env_key:
            defaults["api_key"] = "YOUR_DEEPSEEK_API_KEY_HERE"
        return defaults

    def _persist_global_config(self):
        _atomic_write_text(
            os.path.join(_exe_dir, "config.json"),
            json.dumps(self._global_config_payload(), ensure_ascii=False, indent=2),
        )
        generator.config = dict(self._global_config_payload())

    def _project_needs_setup(self):
        title = str(self.config.get("book_title") or "").strip()
        brief = str(self.config.get("book_brief") or "").strip()
        total = int(self.config.get("target_total_chapters", 0) or 0)
        return (
            not title
            or title == "未命名项目"
            or not brief
            or total < 1
        )

    def _project_has_official_chapters(self, folder=None):
        output_dir = os.path.join(folder or self.project_dir, "output")
        if not os.path.isdir(output_dir):
            return False
        for root_dir, dirs, files in os.walk(output_dir):
            dirs[:] = [d for d in dirs if d != ".backup"]
            if any(self._is_official_chapter_file(name) for name in files):
                return True
        return False

    def _build_volume_ranges(self, total, volume_size):
        ranges = []
        start = 1
        vol_idx = 1
        while start <= total:
            end = min(total, start + volume_size - 1)
            ranges.append([start, end, f"第{_num_to_cn_chapter(vol_idx)}卷"])
            start = end + 1
            vol_idx += 1
        return ranges

    def _unique_project_folder(self, title):
        safe_name = re.sub(r'[<>:"/\\|?*]+', "_", title).strip(" .") or "我的小说"
        parent = os.path.join(_exe_dir, "projects")
        os.makedirs(parent, exist_ok=True)
        candidate = os.path.join(parent, safe_name)
        # A title may collide with a legacy file (not only a directory).  Keep
        # the existing path untouched and allocate an isolated project folder
        # instead of letting os.listdir(file) raise NotADirectoryError.
        if not os.path.exists(candidate):
            return candidate
        if os.path.isdir(candidate) and not os.listdir(candidate):
            return candidate
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        candidate = os.path.join(parent, f"{safe_name}_{stamp}")
        # Keep this safe under repeated starts in the same second or a stale
        # timestamped folder from a cancelled setup.
        suffix = 1
        while os.path.exists(candidate):
            candidate = os.path.join(parent, f"{safe_name}_{stamp}_{suffix}")
            suffix += 1
        return candidate

    def _configure_book_project(self, title, genre, book_brief, total, volume_size, force_new=False):
        if not force_new and self._project_has_official_chapters():
            existing_title = str(self.config.get("book_title") or "").strip()
            existing_genre = str(self.config.get("genre_template") or "").strip()
            existing_brief = str(self.config.get("book_brief") or "").strip()
            existing_total = int(self.config.get("target_total_chapters", 0) or 0)
            existing_ranges = self._get_volume_ranges()
            existing_volume_size = (
                existing_ranges[0][1] - existing_ranges[0][0] + 1
                if existing_ranges else 0
            )
            changed = (
                title.strip() != existing_title
                or (genre or "通用空白").strip() != existing_genre
                or book_brief.strip() != existing_brief
                or int(total) != existing_total
                or int(volume_size) != existing_volume_size
            )
            if changed:
                raise RuntimeError(
                    "这本书已经有正式章节，不能再改书名、题材、主题或篇幅，"
                    "否则会让大纲、正史和已写正文互相冲突。请点“新建一本”。"
                )
        ranges = self._build_volume_ranges(total, volume_size)
        create_new = force_new or self._project_needs_setup()
        if create_new:
            folder = self._unique_project_folder(title)
            os.makedirs(folder, exist_ok=True)
            if not self.switch_project(folder):
                raise RuntimeError("无法切换到新书项目目录")

        pconfig = self._default_project_config()
        # 修改已有项目时保留已生效的高级安全设置。
        current_path = os.path.join(self.project_dir, "project_config.json")
        if os.path.exists(current_path):
            try:
                with open(current_path, "r", encoding="utf-8") as f:
                    current = json.load(f)
                if isinstance(current, dict):
                    pconfig.update(current)
            except Exception:
                pass
        pconfig.update({
            "book_title": title,
            "genre_template": genre or "通用空白",
            "book_brief": book_brief.strip(),
            "target_total_chapters": total,
            "volume_ranges": ranges,
        })
        if not self._project_has_official_chapters():
            pconfig["trial_continue_confirmed"] = False
        _atomic_write_text(current_path, json.dumps(pconfig, ensure_ascii=False, indent=2))
        self.config.update(pconfig)

        plot_dir = generator.DIRS["plot"]
        self._ensure_text_file(os.path.join(plot_dir, "全书大纲.txt"), f"《{title}》全书大纲\n\n等待系统自动生成。\n")
        self._ensure_text_file(os.path.join(plot_dir, "当前卷大纲.txt"), "第一卷\n\n等待系统自动生成。\n")
        self._ensure_text_file(os.path.join(plot_dir, "第一卷逐章细纲.txt"), "第1章 标题待定\n\n核心事件：等待系统自动生成。\n")
        self._ensure_text_file(os.path.join(plot_dir, "基调铁律.txt"), "等待系统根据题材与番茄规则自动生成。\n")
        self._ensure_text_file(os.path.join(plot_dir, "唯一真相设定表.md"), "# 唯一真相设定表\n\n等待系统自动生成。\n")
        self._ensure_text_file(os.path.join(plot_dir, "时间线锚点.txt"), "时间线锚点由系统随章节自动维护。\n")
        self._ensure_text_file(os.path.join(plot_dir, "伏笔与因果追踪表.txt"), "伏笔与因果由系统随章节自动维护。\n")
        self._ensure_text_file(os.path.join(plot_dir, "关键词一致性检查表.txt"), "专有名词一致性由系统自动维护。\n")
        self._ensure_text_file(os.path.join(plot_dir, "每10章审稿清单.txt"), "每10章自动复核时间线、角色状态、伏笔、真相揭示与重复事件。\n")
        self._load_project_config(self.project_dir)
        self._write_generated_project_rules()
        if hasattr(generator, "reload_project_tool_rules"):
            generator.reload_project_tool_rules()
        if hasattr(cross_chapter_scanner, "reload_project_tool_rules"):
            cross_chapter_scanner.reload_project_tool_rules(self.project_dir)
        self.refresh_knowledge_base()
        self.refresh_status()
        return ranges

    def get_model_provider(self):
        return normalize_model_provider((self.config or {}).get("model_provider"))

    def _provider_display_name(self):
        return PROVIDER_PRESET_NAMES[self.get_model_provider()]

    def _active_model_preset(self):
        provider = self.get_model_provider()
        name = PROVIDER_PRESET_NAMES[provider]
        self._active_preset_name = name
        return MODEL_PRESETS[name]

    def get_client(self):
        """Return a DeepSeek client only; provider-neutral code must not call it."""
        if self.get_model_provider() != PROVIDER_DEEPSEEK:
            return None
        preset = self._active_model_preset()
        key_field = preset.get("config_key_field", "api_key")
        api_key = self._get_runtime_api_key()
        if not api_key:
            # Fix: 线程安全弹窗
            self._ui(lambda kf=key_field: messagebox.showwarning("提示", "请先填写 DeepSeek API Key。"))
            return None
        base_url = str(self.config.get("base_url") or preset["base_url"]).strip()
        kwargs = {
            "api_key": api_key,
            "base_url": base_url,
            "timeout": 120,
        }
        if getattr(self, "_deepseek_direct_mode", False):
            # Windows system proxy + Clash Fake-IP can produce a valid TCP
            # tunnel but abort TLS. Direct mode still uses the TUN interface,
            # while ignoring the broken explicit proxy discovered by httpx.
            kwargs["http_client"] = httpx.Client(trust_env=False, timeout=120)
        return OpenAI(**kwargs)

    def get_model_name(self):
        preset = self._active_model_preset()
        return resolve_preset_model(preset, self.config, review=False)

    def get_review_model_name(self):
        preset = self._active_model_preset()
        return resolve_preset_model(preset, self.config, review=True)

    @staticmethod
    def _require_automatic_review_model(model_name, evidence_label):
        model = str(model_name or "").strip()
        if not model or "manual" in model.lower():
            raise narrative_guard.NarrativeGuardError(
                f"{evidence_label}缺少有效的自动审查模型"
            )
        return model

    def _narrative_review_model_for_chapter(self, chap_num, chapter_status=None):
        """Resolve the model bound to historical evidence, never an empty wildcard."""
        chapter = int(chap_num)
        status = (
            chapter_status
            if isinstance(chapter_status, dict)
            else (self._load_latest_chapter_statuses().get(chapter) or {})
        )
        if "narrative_review_model" in status:
            return self._require_automatic_review_model(
                status.get("narrative_review_model"),
                f"第{chapter}章状态记录",
            )

        audit_path = os.path.join(
            generator.DIRS["plot"],
            "narrative_audits",
            f"chapter_{chapter:04d}.json",
        )
        if os.path.exists(audit_path):
            try:
                with open(audit_path, "r", encoding="utf-8-sig") as handle:
                    audit = json.load(handle)
            except Exception as exc:
                raise narrative_guard.NarrativeGuardError(
                    f"第{chapter}章叙事审计无法读取模型：{exc}"
                ) from exc
            if not isinstance(audit, dict):
                raise narrative_guard.NarrativeGuardError(
                    f"第{chapter}章叙事审计不是 JSON 对象"
                )
            review_model = self._require_automatic_review_model(
                audit.get("review_model"), f"第{chapter}章叙事审计"
            )
            persisted_model = self._require_automatic_review_model(
                audit.get("model"), f"第{chapter}章持久化叙事审计"
            )
            if review_model != persisted_model:
                raise narrative_guard.NarrativeGuardError(
                    f"第{chapter}章叙事审计模型记录不一致"
                )
            return review_model

        return self._require_automatic_review_model(
            self.get_review_model_name(), "当前审稿配置"
        )

    def _commercial_review_model_for_result(self, result, recorded_model=""):
        """Prefer the model already sealed into historical commercial evidence."""
        if recorded_model:
            return self._require_automatic_review_model(
                recorded_model, "商业审稿提交收据"
            )
        receipt = (result or {}).get("review_receipt") or {}
        if isinstance(receipt, dict) and "model" in receipt:
            return self._require_automatic_review_model(
                receipt.get("model"), "商业审稿调用收据"
            )
        if isinstance(result, dict) and "model" in result:
            return self._require_automatic_review_model(
                result.get("model"), "商业审稿结果"
            )
        return self._require_automatic_review_model(
            self.get_review_model_name(), "当前审稿配置"
        )

    def _has_configured_api_key(self):
        return bool(self._get_runtime_api_key())

    def _codex_evidence_dir(self, task_type="preflight"):
        safe_task = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(task_type or "call"))
        log_dir = generator.DIRS.get("logs") or os.path.join(self.project_dir, "logs")
        path = os.path.join(log_dir, "codex_sol_evidence", safe_task)
        os.makedirs(path, exist_ok=True)
        return path

    def _resolve_codex_executable(self):
        if codex_sol_runtime is None:
            raise ModelProviderConfigError(
                "Codex GPT-5.6-sol 运行层缺失，已停止模型调用"
            )
        # The approved CLI is vendored with the application, not inside each
        # book's data directory.  Book evidence remains project-scoped below.
        project_root = os.path.abspath(_exe_dir)
        return codex_sol_runtime.resolve_project_executable(
            project_root,
            configured=str(self.config.get("codex_executable") or "").strip(),
        )

    def _has_configured_model_provider(self):
        try:
            provider = self.get_model_provider()
            if provider == PROVIDER_DEEPSEEK:
                return self._has_configured_api_key()
            executable = self._resolve_codex_executable()
            return bool(executable and os.path.isfile(executable))
        except Exception:
            return False

    def _provider_preflight_cache_key(self, provider=None):
        provider = provider or self.get_model_provider()
        if provider == PROVIDER_DEEPSEEK:
            preset = MODEL_PRESETS[PROVIDER_PRESET_NAMES[provider]]
            base_url = str(self.config.get("base_url") or preset["base_url"]).strip()
            key_fingerprint = hashlib.sha256(
                self._get_runtime_api_key().encode("utf-8")
            ).hexdigest()
            return "|".join((
                provider,
                base_url,
                self.get_model_name(),
                self.get_review_model_name(),
                key_fingerprint,
            ))
        executable = os.path.realpath(self._resolve_codex_executable())
        with open(executable, "rb") as handle:
            executable_sha256 = hashlib.sha256(handle.read()).hexdigest()
        return "|".join((
            provider,
            executable,
            executable_sha256,
            CODEX_SOL_MODEL,
        ))

    def _get_cached_provider_preflight(self):
        try:
            key = self._provider_preflight_cache_key()
        except Exception:
            return {}
        cache = getattr(self, "_provider_preflight_cache", {}) or {}
        value = cache.get(key)
        if not isinstance(value, dict):
            return {}
        if (
            self.get_model_provider() == PROVIDER_CODEX_SOL
            and value.get("status") == "PASS"
            and not self._codex_preflight_evidence_valid(value)
        ):
            return {}
        return value

    def _codex_preflight_evidence_valid(self, cache):
        path = os.path.realpath(str((cache or {}).get("evidence_path") or ""))
        expected = str((cache or {}).get("evidence_sha256") or "").lower()
        evidence_root = os.path.realpath(
            os.path.join(
                generator.DIRS.get("logs") or os.path.join(self.project_dir, "logs"),
                "codex_sol_evidence",
            )
        )
        try:
            inside = os.path.commonpath([evidence_root, path]) == evidence_root
        except (ValueError, OSError):
            inside = False
        if (
            not inside
            or not os.path.isfile(path)
            or not re.fullmatch(r"[0-9a-f]{64}", expected)
        ):
            return False
        try:
            with open(path, "rb") as handle:
                actual = hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            return False
        return actual == expected

    def _run_deepseek_preflight(self, force=False):
        """Read-only API/key/model/balance validation before paid generation."""
        if self.get_model_provider() != PROVIDER_DEEPSEEK:
            raise ModelProviderConfigError("当前项目未选择 DeepSeek")
        if not self.config.get("deepseek_preflight_enabled", True):
            return {"status": "SKIP", "message": "DeepSeek 预检已关闭"}
        now = time.time()
        cache_key = self._provider_preflight_cache_key(PROVIDER_DEEPSEEK)
        cache = (getattr(self, "_provider_preflight_cache", {}) or {}).get(
            cache_key, {}
        )
        ttl = max(
            30,
            int(
                self.config.get(
                    "provider_preflight_cache_seconds",
                    self.config.get("deepseek_preflight_cache_seconds", 300),
                )
                or 300
            ),
        )
        if (
            not force
            and cache.get("status") == "PASS"
            and now - float(cache.get("checked_at_epoch") or 0) <= ttl
        ):
            return cache
        client = self.get_client()
        if client is None:
            raise deepseek_runtime.DeepSeekPreflightError("未配置 DeepSeek API Key")
        preset = MODEL_PRESETS[PROVIDER_PRESET_NAMES[PROVIDER_DEEPSEEK]]
        base_url = str(self.config.get("base_url") or preset["base_url"]).strip()
        try:
            result = deepseek_runtime.preflight(
                client=client,
                api_key=self._get_runtime_api_key(),
                base_url=base_url,
                required_models=[self.get_model_name(), self.get_review_model_name()],
            )
        except Exception as exc:
            if (
                not getattr(self, "_deepseek_direct_mode", False)
                and deepseek_runtime.is_transient_connection_error(exc)
            ):
                try:
                    direct = deepseek_runtime.enable_direct_dns(base_url)
                    self._deepseek_direct_mode = True
                    result = deepseek_runtime.preflight(
                        client=self.get_client(),
                        api_key=self._get_runtime_api_key(),
                        base_url=base_url,
                        required_models=[
                            self.get_model_name(),
                            self.get_review_model_name(),
                        ],
                    )
                    result["network_route"] = "direct_public_dns"
                    result["network_host"] = direct["host"]
                except Exception as fallback_exc:
                    exc = fallback_exc
                else:
                    exc = None
            if exc is None:
                pass
            else:
                self._deepseek_preflight_cache = {
                    "status": "FAIL",
                    "message": str(exc),
                    "checked_at_epoch": now,
                }
                self._provider_preflight_cache = dict(
                    getattr(self, "_provider_preflight_cache", {}) or {}
                )
                self._provider_preflight_cache[cache_key] = dict(
                    self._deepseek_preflight_cache
                )
                raise exc
        result["provider"] = PROVIDER_DEEPSEEK
        result["models"] = [self.get_model_name(), self.get_review_model_name()]
        result["checked_at_epoch"] = now
        result["checked_at"] = datetime.now().isoformat(timespec="seconds")
        self._deepseek_preflight_cache = result
        self._provider_preflight_cache = dict(
            getattr(self, "_provider_preflight_cache", {}) or {}
        )
        self._provider_preflight_cache[cache_key] = result
        return result

    def _run_codex_preflight(self, force=False):
        if self.get_model_provider() != PROVIDER_CODEX_SOL:
            raise ModelProviderConfigError("当前项目未选择 Codex GPT-5.6-sol")
        now = time.time()
        cache_key = self._provider_preflight_cache_key(PROVIDER_CODEX_SOL)
        cache = (getattr(self, "_provider_preflight_cache", {}) or {}).get(
            cache_key, {}
        )
        ttl = max(
            30,
            int(
                self.config.get(
                    "provider_preflight_cache_seconds",
                    self.config.get("deepseek_preflight_cache_seconds", 300),
                )
                or 300
            ),
        )
        if (
            not force
            and cache.get("status") == "PASS"
            and now - float(cache.get("checked_at_epoch") or 0) <= ttl
            and self._codex_preflight_evidence_valid(cache)
        ):
            return cache
        executable = self._resolve_codex_executable()
        with open(executable, "rb") as handle:
            executable_sha256 = hashlib.sha256(handle.read()).hexdigest()
        timeout_seconds = max(
            30, int(self.config.get("codex_timeout_seconds", 900) or 900)
        )
        try:
            result = codex_sol_runtime.preflight(
                executable=executable,
                evidence_dir=self._codex_evidence_dir("preflight"),
                timeout_seconds=min(timeout_seconds, 300),
            )
            if not isinstance(result, dict):
                raise RuntimeError("Codex 预检未返回结构化结果")
            if str(result.get("provider") or "") != str(
                getattr(codex_sol_runtime, "PROVIDER", "openai_codex_cli")
            ):
                raise RuntimeError("Codex 预检提供方证据不匹配")
            if str(result.get("model") or "") != CODEX_SOL_MODEL:
                raise RuntimeError("Codex 预检模型证据不是 exact gpt-5.6-sol")
            result = dict(result)
            result.update({
                "status": "PASS",
                "message": "Codex CLI、登录态、exact gpt-5.6-sol 与证据链可用",
                "executable": os.path.realpath(executable),
                "executable_sha256": executable_sha256,
                "checked_at_epoch": now,
                "checked_at": datetime.now().isoformat(timespec="seconds"),
            })
        except Exception as exc:
            result = {
                "status": "FAIL",
                "message": str(exc),
                "provider": PROVIDER_CODEX_SOL,
                "model": CODEX_SOL_MODEL,
                "checked_at_epoch": now,
            }
            self._provider_preflight_cache = dict(
                getattr(self, "_provider_preflight_cache", {}) or {}
            )
            self._provider_preflight_cache[cache_key] = result
            raise
        self._provider_preflight_cache = dict(
            getattr(self, "_provider_preflight_cache", {}) or {}
        )
        self._provider_preflight_cache[cache_key] = result
        return result

    def _run_provider_preflight(self, force=False):
        provider = self.get_model_provider()
        if provider == PROVIDER_DEEPSEEK:
            return self._run_deepseek_preflight(force=force)
        if provider == PROVIDER_CODEX_SOL:
            return self._run_codex_preflight(force=force)
        raise ModelProviderConfigError(f"不支持的模型提供方：{provider}")

    def _deepseek_request_kwargs(
        self,
        *,
        model,
        messages,
        max_tokens,
        temperature=None,
        review=False,
        stream=False,
        thinking_override=None,
    ):
        if thinking_override is None:
            thinking = bool(
                self.config.get(
                    "deepseek_review_thinking" if review else "deepseek_generation_thinking",
                    True if review else False,
                )
            )
        else:
            thinking = bool(thinking_override)
        # DeepSeek counts hidden reasoning against max_tokens.  Short structured
        # audits previously spent the entire allowance on reasoning and returned
        # an empty answer, which then looked like a quality failure and caused an
        # unnecessary chapter rewrite.  Keep thinking for genuinely roomy review
        # calls, while making compact JSON/checklist gates deterministic.
        if review and thinking and int(max_tokens) < max(
            1,
            int(self.config.get("deepseek_review_thinking_min_tokens", 6000) or 6000),
        ):
            thinking = False
        return deepseek_runtime.build_chat_kwargs(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            thinking=thinking,
            temperature=temperature,
            reasoning_effort=str(
                self.config.get("deepseek_review_reasoning_effort", "high") or "high"
            ),
            stream=stream,
        )

    def clean_think_tags(self, text):
        return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    def strip_markdown_artifacts(self, text):
        text = self.clean_think_tags(text)
        text = re.sub(r'\*\*(.*?)\*\*', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'__(.*?)__', r'\1', text, flags=re.DOTALL)
        text = re.sub(r'`([^`]+)`', r'\1', text)
        cleaned_lines = []
        for idx, line in enumerate(text.splitlines()):
            line = re.sub(r'^\s*#{1,6}\s*', '', line)
            if re.match(r'^\s*[-*]\s+', line):
                line = re.sub(r'^\s*[-*]\s+', '', line)
            cleaned_lines.append(line)
        return "\n".join(cleaned_lines).strip()

    def _ensure_chapter_title(self, content, chap_num, chapter_outline=""):
        """Add or normalize the first-line chapter title without spending an LLM retry."""
        content = (content or "").strip()
        if not content:
            return content, False
        lines = content.splitlines()
        first = lines[0].strip().lstrip("#").strip() if lines else ""
        title_match = re.match(rf'^第\s*{chap_num}\s*章(?:\s+.+)?$', first)
        if title_match:
            normalized = re.sub(r'^第\s*' + str(chap_num) + r'\s*章', f'第{chap_num}章', first)
            if normalized != lines[0]:
                lines[0] = normalized
                return "\n".join(lines).strip(), True
            return content, False

        outline_title = ""
        if chapter_outline:
            outline_first = chapter_outline.strip().splitlines()[0].strip()
            m = re.match(rf'^第\s*{chap_num}\s*章\s*([^\n：:]+)', outline_first)
            if m:
                outline_title = m.group(1).strip()
        title = outline_title or "本章"
        return f"第{chap_num}章 {title}\n\n{content}", True

    # ============================================================
    # UI 构建
    # ============================================================
    def create_widgets(self):
        style = ttk.Style(self.root)
        style.configure("OneClick.TButton", font=("微软雅黑", 17, "bold"), padding=(18, 16))
        style.configure("Stop.TButton", font=("微软雅黑", 11, "bold"), padding=(10, 8))

        shell = ttk.Frame(self.root, padding=(22, 18))
        shell.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(shell)
        header.pack(fill=tk.X)
        ttk.Label(header, text="一键生成整本小说", font=("微软雅黑", 22, "bold")).pack(anchor=tk.W)
        ttk.Label(
            header,
            text="所选模型自动完成大纲、逐章写作、质检、记忆、商业审稿与发布稿整理",
            font=("微软雅黑", 10),
            foreground="#555555",
        ).pack(anchor=tk.W, pady=(3, 0))

        book_card = ttk.LabelFrame(shell, text=" 当前这本书 ", padding=(14, 10))
        book_card.pack(fill=tk.X, pady=(16, 10))
        self.book_summary_lbl = ttk.Label(book_card, text="尚未设置新书", font=("微软雅黑", 12, "bold"))
        self.book_summary_lbl.pack(anchor=tk.W)
        status_row = ttk.Frame(book_card)
        status_row.pack(fill=tk.X, pady=(7, 0))
        self.info_lbl = ttk.Label(status_row, text=self.get_status_text(), font=("微软雅黑", 9))
        self.info_lbl.pack(side=tk.LEFT)
        self.readiness_lbl = ttk.Label(status_row, text="正在检查", font=("微软雅黑", 9, "bold"))
        self.readiness_lbl.pack(side=tk.LEFT, padx=(18, 0))
        self.word_count_lbl = ttk.Label(status_row, text="最新正文: 0字", font=("微软雅黑", 9))
        self.word_count_lbl.pack(side=tk.RIGHT)
        self.usage_lbl = ttk.Label(book_card, text="模型调用：尚未开始", font=("微软雅黑", 8), foreground="#666666")
        self.usage_lbl.pack(anchor=tk.W, pady=(5, 0))
        self.folder_lbl = ttk.Label(book_card, text="未选择", font=("微软雅黑", 8), foreground="#777777")
        self.folder_lbl.pack(anchor=tk.W, pady=(2, 0))

        action = ttk.Frame(shell)
        action.pack(fill=tk.X, pady=(2, 10))
        self.btn_full_book = ttk.Button(
            action,
            text="一键生成整本小说",
            command=self.one_click_generate_full_book,
            style="OneClick.TButton",
        )
        self.btn_full_book.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.btn_stop = ttk.Button(
            action,
            text="暂停",
            command=self.stop_batch,
            state=tk.DISABLED,
            style="Stop.TButton",
        )
        self.btn_stop.pack(side=tk.LEFT, padx=(10, 0))

        helper_row = ttk.Frame(shell)
        helper_row.pack(fill=tk.X, pady=(0, 10))
        self.btn_setup = ttk.Button(helper_row, text="修改本书", command=self.open_one_click_setup)
        self.btn_setup.pack(side=tk.LEFT)
        self.btn_new_book = ttk.Button(
            helper_row,
            text="新建一本",
            command=lambda: self.open_one_click_setup(force_new=True),
        )
        self.btn_new_book.pack(side=tk.LEFT, padx=(8, 0))
        self.btn_switch_project = ttk.Button(helper_row, text="打开已有项目", command=self.select_project_folder)
        self.btn_switch_project.pack(side=tk.LEFT, padx=(8, 0))
        self.btn_open_output = ttk.Button(helper_row, text="打开发布稿文件夹", command=self.open_publish_folder)
        self.btn_open_output.pack(side=tk.LEFT, padx=(8, 0))
        self.btn_export_result = ttk.Button(
            helper_row, text="导出当前结果", command=self.export_current_result
        )
        self.btn_export_result.pack(side=tk.LEFT, padx=(8, 0))
        self.btn_health = ttk.Button(helper_row, text="查看详细体检", command=self.health_check)
        self.btn_health.pack(side=tk.RIGHT)

        notebook = ttk.Notebook(shell)
        notebook.pack(fill=tk.BOTH, expand=True)
        progress_tab = ttk.Frame(notebook, padding=5)
        chapter_tab = ttk.Frame(notebook, padding=5)
        copy_tab = ttk.Frame(notebook, padding=5)
        notebook.add(progress_tab, text="自动生成进度")
        notebook.add(chapter_tab, text="最新生成正文")
        notebook.add(copy_tab, text="单章复制")
        self.progress_text = scrolledtext.ScrolledText(progress_tab, wrap=tk.WORD, font=("微软雅黑", 10))
        self.progress_text.pack(fill=tk.BOTH, expand=True)
        self.progress_text.insert(tk.END, "等待开始。首次使用只需点上方按钮并填写一次新书设置。\n")
        self.result_text = scrolledtext.ScrolledText(chapter_tab, wrap=tk.WORD, font=("微软雅黑", 11))
        self.result_text.pack(fill=tk.BOTH, expand=True)
        self.result_text.bind("<KeyRelease>", lambda e: self.update_word_count())
        self.create_context_menu(self.result_text)
        self.create_context_menu(self.progress_text)

        reader_toolbar = ttk.Frame(copy_tab)
        reader_toolbar.pack(fill=tk.X, pady=(0, 7))
        ttk.Label(reader_toolbar, text="稿件：").pack(side=tk.LEFT)
        self.reader_source_var = tk.StringVar(value="最新稿（优先5.6-sol）")
        self.reader_source_combo = ttk.Combobox(
            reader_toolbar,
            textvariable=self.reader_source_var,
            state="readonly",
            width=20,
            values=("最新稿（优先5.6-sol）", "5.6-sol待审稿", "正式稿"),
        )
        self.reader_source_combo.pack(side=tk.LEFT, padx=(0, 8))
        self.reader_source_combo.bind(
            "<<ComboboxSelected>>", self.reader_change_source
        )
        ttk.Label(reader_toolbar, text="选择章节：").pack(side=tk.LEFT)
        self.reader_pick_var = tk.StringVar()
        self.reader_pick_combo = ttk.Combobox(
            reader_toolbar,
            textvariable=self.reader_pick_var,
            state="readonly",
            width=24,
        )
        self.reader_pick_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.reader_pick_combo.bind("<<ComboboxSelected>>", self.reader_jump_to_selected)
        ttk.Button(reader_toolbar, text="上一章", command=self.reader_prev).pack(side=tk.LEFT, padx=(7, 0))
        ttk.Button(reader_toolbar, text="下一章", command=self.reader_next).pack(side=tk.LEFT, padx=(5, 0))
        ttk.Button(reader_toolbar, text="刷新", command=self.reader_refresh_current).pack(side=tk.LEFT, padx=(5, 0))
        ttk.Button(
            reader_toolbar,
            text="复制整章（含标题）",
            command=self.reader_copy_full_chapter,
        ).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Button(
            reader_toolbar,
            text="只复制正文",
            command=self.reader_copy_all,
        ).pack(side=tk.LEFT, padx=(5, 0))
        self.reader_lbl = ttk.Label(copy_tab, text="暂无章节", foreground="#666666")
        self.reader_lbl.pack(fill=tk.X, pady=(0, 5))
        self.reader_text = scrolledtext.ScrolledText(
            copy_tab,
            wrap=tk.WORD,
            font=("微软雅黑", 11),
        )
        self.reader_text.pack(fill=tk.BOTH, expand=True)
        self.create_context_menu(self.reader_text)
        self.reader_chapter_idx = 0
        self.reader_chapter_files = []
        # Display labels are kept separate from file paths.  A project can
        # contain chapters in nested volume folders, so using only basename
        # values in the combobox makes it impossible to tell duplicate entries
        # apart (and selecting one of them used to jump to the first match).
        self.reader_chapter_choices = []

        # 后台链路沿用这些控件/变量，但不再暴露给用户逐项操作。
        hidden = ttk.Frame(self.root)
        self.left_scrollable = hidden
        self.prompt_text = tk.Text(hidden, height=1)
        self.chars_frame = ttk.Frame(hidden)
        self.world_frame = ttk.Frame(hidden)
        self.plot_frame = ttk.Frame(hidden)
        self.chars_vars = {}
        self.world_vars = {}
        self.plot_vars = {}
        self.btn_new = ttk.Button(hidden, command=self.generate_new_chapter)
        self.btn_continue = ttk.Button(hidden, command=self.continue_chapter)
        self.btn_batch = ttk.Button(hidden, command=self.batch_generate)
        self.btn_save_new = ttk.Button(hidden, command=self.save_new_chapter, state=tk.DISABLED)
        self.btn_save_append = ttk.Button(hidden, command=self.save_append_chapter, state=tk.DISABLED)
        self._busy_sensitive_widgets.extend([
            self.btn_full_book,
            self.btn_setup,
            self.btn_new_book,
            self.btn_switch_project,
            self.btn_open_output,
            self.btn_export_result,
            self.btn_health,
            self.btn_new,
            self.btn_continue,
            self.btn_batch,
        ])

    # ============================================================
    # 划线精修右键菜单
    # ============================================================
    def create_context_menu(self, text_widget):
        context_menu = tk.Menu(text_widget, tearoff=0, font=("微软雅黑", 10))
        context_menu.add_command(label="剪切", command=lambda: text_widget.event_generate("<<Cut>>"))
        context_menu.add_command(label="复制", command=lambda: text_widget.event_generate("<<Copy>>"))
        context_menu.add_command(label="粘贴", command=lambda: text_widget.event_generate("<<Paste>>"))

        def show_context_menu(event):
            try:
                context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                context_menu.grab_release()
        text_widget.bind("<Button-3>", show_context_menu)



    # ============================================================
    # 设置面板 (Fix Bug5: config_path 使用 _exe_dir)
    # ============================================================
    def open_publish_folder(self):
        publish_dir = generator.DIRS.get("publish") or self.project_dir
        os.makedirs(publish_dir, exist_ok=True)
        try:
            os.startfile(publish_dir)
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    def open_one_click_setup(self, force_new=False):
        """一次收齐开书所需信息；后续细项全部由后台托管。"""
        if self._is_busy():
            messagebox.showwarning("暂时不能修改", "整本生成或后台准备正在运行，请先安全停止。")
            return
        win = tk.Toplevel(self.root)
        win.title("首次设置：这本书写什么")
        win.geometry("650x690")
        win.minsize(580, 580)
        win.transient(self.root)
        win.grab_set()

        body = ttk.Frame(win, padding=18)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="只设置一次，剩下交给系统", font=("微软雅黑", 16, "bold")).grid(row=0, column=0, columnspan=2, sticky=tk.W)
        ttk.Label(
            body,
            text="系统会自动做：番茄规则、大纲、逐章细纲、写作、质检、记忆、审稿和发布稿。",
            foreground="#555555",
        ).grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=(4, 14))

        def add_entry(row, label, initial, show=None):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky=tk.NW, pady=6)
            entry = ttk.Entry(body, show=show)
            entry.grid(row=row, column=1, sticky=tk.EW, padx=(12, 0), pady=6)
            entry.insert(0, initial)
            return entry

        current_title = "" if self._project_needs_setup() else str(self.config.get("book_title") or "")
        title_entry = add_entry(2, "书名", current_title)
        genre_entry = add_entry(3, "题材类型", str(self.config.get("genre_template") or ""))

        ttk.Label(body, text="小说主题 / 核心卖点\n（可写禁用套路）").grid(row=4, column=0, sticky=tk.NW, pady=6)
        brief_text = tk.Text(body, height=8, wrap=tk.WORD, font=("微软雅黑", 10))
        brief_text.grid(row=4, column=1, sticky=tk.NSEW, padx=(12, 0), pady=6)
        if not self._project_needs_setup():
            brief_text.insert("1.0", str(self.config.get("book_brief") or ""))

        numbers = ttk.Frame(body)
        numbers.grid(row=5, column=1, sticky=tk.W, padx=(12, 0), pady=6)
        ttk.Label(body, text="篇幅").grid(row=5, column=0, sticky=tk.W, pady=6)
        total_entry = ttk.Entry(numbers, width=8)
        total_entry.pack(side=tk.LEFT)
        total_entry.insert(0, str(int(self.config.get("target_total_chapters", 300) or 300)))
        ttk.Label(numbers, text="章；每卷").pack(side=tk.LEFT, padx=(8, 5))
        volume_entry = ttk.Entry(numbers, width=6)
        volume_entry.pack(side=tk.LEFT)
        current_ranges = self._get_volume_ranges()
        default_volume_size = (current_ranges[0][1] - current_ranges[0][0] + 1) if current_ranges else 50
        volume_entry.insert(0, str(default_volume_size))
        ttk.Label(numbers, text="章").pack(side=tk.LEFT, padx=(5, 0))

        ttk.Label(body, text="模型提供方").grid(row=6, column=0, sticky=tk.W, pady=6)
        try:
            current_provider = normalize_model_provider(
                self.config.get("model_provider")
            )
        except ModelProviderConfigError:
            # The read-only list is the recovery path for a bad persisted id;
            # no model call occurs until the user saves a supported value.
            current_provider = PROVIDER_DEEPSEEK
        provider_var = tk.StringVar(
            value=PROVIDER_PRESET_NAMES[current_provider]
        )
        provider_combo = ttk.Combobox(
            body,
            textvariable=provider_var,
            values=list(MODEL_PRESETS),
            state="readonly",
        )
        provider_combo.grid(row=6, column=1, sticky=tk.EW, padx=(12, 0), pady=6)

        api_entry = add_entry(7, "DeepSeek API Key（仅 DeepSeek 必需）", "", show="*")
        env_active = bool((os.environ.get("DEEPSEEK_API_KEY") or "").strip())
        if env_active:
            deepseek_key_note = "已检测到系统环境变量密钥；这里留空即可，也不会把它写进配置文件。"
        elif self._has_saved_api_key():
            deepseek_key_note = "本机已保存密钥；这里留空表示继续使用。"
        else:
            deepseek_key_note = "尚未配置。密钥只保存在本机工具目录的 config.json。"
        key_note_var = tk.StringVar()

        def refresh_provider_note(*_args):
            selected = MODEL_PRESETS.get(provider_var.get()) or {}
            if selected.get("provider") == PROVIDER_CODEX_SOL:
                key_note_var.set(
                    "Codex 使用本机已批准的项目内 CLI 与现有登录态，不读取也不要求 DeepSeek Key。"
                )
            else:
                key_note_var.set(deepseek_key_note)

        provider_combo.bind("<<ComboboxSelected>>", refresh_provider_note)
        refresh_provider_note()
        ttk.Label(
            body,
            textvariable=key_note_var,
            foreground="#666666",
            font=("微软雅黑", 8),
        ).grid(row=8, column=1, sticky=tk.W, padx=(12, 0))

        clear_key_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(body, text="清除本机已保存的 DeepSeek 密钥", variable=clear_key_var).grid(row=9, column=1, sticky=tk.W, padx=(12, 0), pady=(4, 0))
        budget_entry = add_entry(
            10,
            "本书模型费用保险丝（本地估算¥）",
            f"{float(self.config.get('max_estimated_cost_cny', 100.0) or 100.0):g}",
        )
        cost_var = tk.BooleanVar(value=bool(self.config.get("unattended_cost_confirmed", False)))
        ttk.Checkbutton(
            body,
            text="我知道整本生成会持续调用所选模型；调用次数保险丝始终生效",
            variable=cost_var,
        ).grid(row=11, column=0, columnspan=2, sticky=tk.W, pady=(14, 4))
        ttk.Label(
            body,
            text=(
                f"系统设有每章最多 {max(4, min(20, int(self.config.get('max_model_calls_per_chapter', 16) or 16)))} "
                "次模型调用的硬上限；DeepSeek 保留计价止损，Codex 套餐调用记 token/次数、"
                "本地费用估算为 0。"
            ),
            foreground="#8a5a00",
            font=("微软雅黑", 8),
        ).grid(row=12, column=0, columnspan=2, sticky=tk.W)

        body.columnconfigure(1, weight=1)
        body.rowconfigure(4, weight=1)

        def save(start_now):
            title = title_entry.get().strip()
            genre = genre_entry.get().strip()
            brief = brief_text.get("1.0", tk.END).strip()
            selected_preset = MODEL_PRESETS.get(provider_var.get())
            if not selected_preset:
                messagebox.showerror("模型设置有误", "请选择受支持的模型提供方。", parent=win)
                return
            selected_provider = normalize_model_provider(
                selected_preset.get("provider"),
                missing_defaults_to_deepseek=False,
            )
            try:
                total = int(total_entry.get().strip())
                volume_size = int(volume_entry.get().strip())
                max_estimated_cost = float(budget_entry.get().strip())
            except ValueError:
                messagebox.showerror(
                    "设置有误",
                    "总章数、每卷章数和费用止损必须是有效数字。",
                    parent=win,
                )
                return
            if not title or not genre or not brief:
                messagebox.showerror("设置未完成", "书名、题材类型和小说主题都要填写。", parent=win)
                return
            if not (3 <= total <= 2000) or not (10 <= volume_size <= 200):
                messagebox.showerror("篇幅有误", "总章数需为 3-2000，每卷章数需为 10-200。", parent=win)
                return
            if not (0.1 <= max_estimated_cost <= 100000):
                messagebox.showerror(
                    "费用止损有误",
                    "本书费用止损需在 0.1-100000 元之间。",
                    parent=win,
                )
                return

            new_key = api_entry.get().strip()
            if clear_key_var.get():
                self.config["api_key"] = "YOUR_DEEPSEEK_API_KEY_HERE"
            elif new_key:
                if new_key == "YOUR_DEEPSEEK_API_KEY_HERE":
                    messagebox.showerror("密钥无效", "请填写真实的 DeepSeek API Key。", parent=win)
                    return
                self.config["api_key"] = new_key
            self.config["model_provider"] = selected_provider
            self._active_preset_name = PROVIDER_PRESET_NAMES[selected_provider]
            self._persist_global_config()

            if (
                start_now
                and selected_provider == PROVIDER_DEEPSEEK
                and not self._has_configured_api_key()
            ):
                messagebox.showerror("还不能开始", "请填写 DeepSeek API Key，或先设置系统环境变量 DEEPSEEK_API_KEY。", parent=win)
                return
            if start_now and not cost_var.get():
                messagebox.showerror("请确认模型调用", "勾选模型调用确认后，才能无人值守生成整本。", parent=win)
                return
            try:
                self._configure_book_project(
                    title, genre, brief, total, volume_size,
                    force_new=bool(force_new),
                )
                pconfig_path = os.path.join(self.project_dir, "project_config.json")
                with open(pconfig_path, "r", encoding="utf-8") as f:
                    pconfig = json.load(f)
                pconfig["model_provider"] = selected_provider
                pconfig["unattended_cost_confirmed"] = bool(cost_var.get())
                pconfig["max_model_calls_per_chapter"] = max(
                    4,
                    min(20, int(pconfig.get("max_model_calls_per_chapter", 16) or 16)),
                )
                pconfig["max_estimated_cost_cny"] = round(max_estimated_cost, 2)
                _atomic_write_text(pconfig_path, json.dumps(pconfig, ensure_ascii=False, indent=2))
                self.config.update(pconfig)
                self._load_project_config(self.project_dir)
                if self._project_config_error:
                    raise RuntimeError(self._project_config_error)
            except Exception as exc:
                messagebox.showerror("保存失败", str(exc), parent=win)
                return

            win.destroy()
            self._ui_progress_append(
                f"\n[OK] 已保存《{title}》：{genre}，共 {total} 章。\n"
                f"本书模型费用保险丝：本地估算¥{max_estimated_cost:.2f}。\n"
                "接下来所有准备与写作步骤都会自动运行。\n",
                clear=True,
            )
            if start_now:
                self.root.after(150, self.one_click_generate_full_book)

        buttons = ttk.Frame(body)
        buttons.grid(row=13, column=0, columnspan=2, sticky=tk.E, pady=(18, 0))
        ttk.Button(buttons, text="只保存", command=lambda: save(False)).pack(side=tk.LEFT, padx=5)
        ttk.Button(buttons, text="保存并开始整本生成", command=lambda: save(True)).pack(side=tk.LEFT, padx=5)

    def open_settings(self):
        self.open_one_click_setup()
        return
        if self._is_busy():
            messagebox.showwarning("无法修改设置", "后台任务运行期间不能修改模型或 API 设置。")
            return
        win = tk.Toplevel(self.root)
        win.title(" 系统设置")
        win.geometry("520x540")
        win.grab_set()

        ttk.Label(win, text="DeepSeek API Key:", font=("微软雅黑", 10)).pack(anchor=tk.W, padx=15, pady=(15, 3))
        api_entry = ttk.Entry(win, width=60, show="*")
        api_entry.pack(padx=15, fill=tk.X)
        api_entry.insert(0, self.config.get("api_key", ""))
        ttk.Label(
            win,
            text="可优先设置系统环境变量 DEEPSEEK_API_KEY；手动保存时密钥会写入本机 config.json。",
            foreground="#9a6700",
            font=("微软雅黑", 8),
        ).pack(anchor=tk.W, padx=15, pady=(2, 0))

        ttk.Label(win, text="API Base URL:", font=("微软雅黑", 10)).pack(anchor=tk.W, padx=15, pady=(10, 3))
        url_entry = ttk.Entry(win, width=60)
        url_entry.pack(padx=15, fill=tk.X)
        url_entry.insert(0, self.config.get("base_url", "https://api.deepseek.com"))

        ttk.Label(win, text="正文生成模型:", font=("微软雅黑", 10)).pack(anchor=tk.W, padx=15, pady=(10, 3))
        model_entry = ttk.Entry(win, width=60)
        model_entry.pack(padx=15, fill=tk.X)
        model_entry.insert(0, self.config.get("model", "deepseek-v4-flash"))

        ttk.Label(win, text="质检/阶段审稿模型:", font=("微软雅黑", 10)).pack(anchor=tk.W, padx=15, pady=(10, 3))
        review_model_entry = ttk.Entry(win, width=60)
        review_model_entry.pack(padx=15, fill=tk.X)
        review_model_entry.insert(0, self.config.get("review_model", "deepseek-v4-pro"))

        ttk.Label(win, text="Temperature (创意度 0.0-1.5):", font=("微软雅黑", 10)).pack(anchor=tk.W, padx=15, pady=(10, 3))
        temp_entry = ttk.Entry(win, width=20)
        temp_entry.pack(anchor=tk.W, padx=15)
        temp_entry.insert(0, str(self.config.get("temperature", 0.8)))

        connection_status = tk.StringVar(value="尚未测试连接")
        ttk.Label(
            win,
            textvariable=connection_status,
            foreground="#555555",
            font=("微软雅黑", 9),
        ).pack(anchor=tk.W, padx=15, pady=(12, 2))

        def test_connection():
            api_key = api_entry.get().strip()
            if not api_key or api_key == "YOUR_DEEPSEEK_API_KEY_HERE":
                connection_status.set("测试失败：请先填写有效 API Key")
                return
            base_url = url_entry.get().strip() or "https://api.deepseek.com"
            models = list(dict.fromkeys(filter(None, (
                model_entry.get().strip(),
                review_model_entry.get().strip(),
            ))))
            if not models:
                connection_status.set("测试失败：请填写正文和审稿模型名")
                return
            test_btn.config(state=tk.DISABLED)
            connection_status.set("正在验证 Key、模型和余额（不生成内容、不计模型 token）...")

            def worker():
                try:
                    client = OpenAI(api_key=api_key, base_url=base_url, timeout=30)
                    checked = deepseek_runtime.preflight(
                        client=client,
                        api_key=api_key,
                        base_url=base_url,
                        required_models=models,
                    )
                    balances = "/".join(
                        f"{item.get('total_balance', '?')}{item.get('currency', '')}"
                        for item in checked.get("balance_infos", [])
                    )
                    result = "连接成功：Key、模型和余额均可用"
                    if balances:
                        result += f"；余额 {balances}"
                except Exception as exc:
                    result = f"连接失败：{str(exc)[:180]}"

                def finish():
                    if win.winfo_exists():
                        connection_status.set(result)
                        test_btn.config(state=tk.NORMAL)

                self.root.after(0, finish)

            threading.Thread(target=worker, daemon=True).start()

        def save_settings():
            # Fix Bug5+自审: 空值不覆盖已有 Key
            new_api_key = api_entry.get().strip()
            if new_api_key:
                self.config["api_key"] = new_api_key
            self.config["base_url"] = url_entry.get().strip()
            self.config["model"] = model_entry.get().strip()
            self.config["review_model"] = review_model_entry.get().strip()
            try:
                self.config["temperature"] = float(temp_entry.get().strip())
            except Exception:
                self.config["temperature"] = 0.8
            # Fix Bug5: 使用 _exe_dir 而非 __file__
            config_path = os.path.join(_exe_dir, "config.json")
            _atomic_write_text(
                config_path,
                json.dumps(self.config, ensure_ascii=False, indent=2),
            )
            # 同步 generator.config
            generator.config = self.config
            messagebox.showinfo("成功", "设置已保存！")
            win.destroy()

        button_row = ttk.Frame(win)
        button_row.pack(pady=18)
        test_btn = ttk.Button(button_row, text="测试两模型", command=test_connection)
        test_btn.pack(side=tk.LEFT, padx=6)
        ttk.Button(button_row, text="保存设置", command=save_settings).pack(side=tk.LEFT, padx=6)

    # ============================================================
    # 知识库加载
    # ============================================================
    def _recovered_staging_chapters(self):
        """Return chapter numbers visible in staging without treating them as official."""
        root_dir = self._reader_staging_dir()
        if not root_dir or not os.path.isdir(root_dir):
            return []
        chapters = set()
        excluded_dirs = {".backup", "archive", "superseded"}
        for current, dirs, files in os.walk(root_dir):
            dirs[:] = [
                name for name in dirs if name.lower() not in excluded_dirs
            ]
            for filename in files:
                if not self._is_official_chapter_file(filename):
                    continue
                chapter = self._extract_chap_num_from_path(filename)
                if chapter > 0:
                    chapters.add(chapter)
        return sorted(chapters)

    def _recovered_staging_range_text(self):
        """Describe only a contiguous recovered draft range starting at chapter 1."""
        chapters = self._recovered_staging_chapters()
        if not chapters or chapters != list(range(1, chapters[-1] + 1)):
            return ""
        return f"1—{chapters[-1]}章"

    def _staging_recovery_notice(self):
        """Show recovery status only while the formal output remains empty."""
        if int(getattr(self, "latest_chap", 0) or 0) > 0:
            return ""
        chapter_range = self._recovered_staging_range_text()
        if not chapter_range:
            return ""
        return f"已恢复5.6-sol待审稿{chapter_range}"

    def get_status_text(self):
        msg = f"进度：第 {self.current_vol} 卷"
        if self.latest_chap > 0:
            msg += f" | 已写至：第 {self.latest_chap} 章 | 下一章：第 {self.next_chap} 章"
        else:
            recovery_notice = self._staging_recovery_notice()
            if recovery_notice:
                msg += (
                    f" | 正式稿尚未重建 | {recovery_notice}"
                    " | 到单章复制查看"
                )
            else:
                msg += " | 尚未开始，准备写第一章"
        return msg

    def update_word_count(self):
        text = self.result_text.get(1.0, tk.END).strip()
        count = len(re.findall(r'[\u4e00-\u9fff]', text))
        self.word_count_lbl.config(text=f"最新正文: {count}字")

    def _refresh_book_summary(self):
        if not hasattr(self, "book_summary_lbl"):
            return
        if self._project_needs_setup():
            self.book_summary_lbl.config(text="尚未设置新书")
            self.folder_lbl.config(text="首次点击主按钮后，只需填写一次书名、题材和主题")
            return
        title = str(self.config.get("book_title") or "未命名")
        genre = str(self.config.get("genre_template") or "通用")
        total = self._get_story_planned_end_chapter()
        self.book_summary_lbl.config(text=f"《{title}》 · {genre} · 计划 {total} 章")
        self.folder_lbl.config(text=f"项目位置：{self.project_dir}")

    def refresh_status(self):
        self.current_vol, self.next_chap, self.filepath, self.latest_chap, self.latest_filepath = generator.get_latest_chapter_info()
        self._sync_project_status_file()
        self.info_lbl.config(text=self.get_status_text())
        self._refresh_book_summary()
        self._refresh_readiness_status()

    def _sync_project_status_file(self):
        """Refresh the informational status file from formal chapter folders."""
        if not self.project_dir:
            return
        try:
            path = os.path.join(self.project_dir, "当前状态.md")
            content = project_status.render_status(self.project_dir, self.config)
            previous = ""
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as handle:
                    previous = handle.read()
            if previous != content:
                _atomic_write_text(path, content)
        except Exception:
            # A status-note failure must never block generation or committing a
            # chapter; output/ and publish/ remain the formal source of truth.
            pass

    def _refresh_live_progress_label(self):
        """Update only the lightweight chapter line during a long batch."""
        status_text = self.get_status_text()
        self._ui(
            lambda status_text=status_text: self.info_lbl.config(text=status_text)
        )

    def _initialization_needed(self):
        if self._commercial_blueprint_needed():
            return True
        plot_dir = generator.DIRS["plot"]
        required_texts = (
            os.path.join(generator.DIRS["world"], "自动生成_世界观.txt"),
            os.path.join(generator.DIRS["chars"], "自动生成_核心角色组.txt"),
            os.path.join(plot_dir, "全书大纲.txt"),
            os.path.join(plot_dir, "基调铁律.txt"),
            os.path.join(plot_dir, "唯一真相设定表.md"),
        )
        if any(
            not book_initializer.has_meaningful_content(generator.read_text_safe(path))
            for path in required_texts
        ):
            return True
        reveal_path = os.path.join(plot_dir, "reveal_rules.json")
        try:
            with open(reveal_path, "r", encoding="utf-8") as f:
                if not (json.load(f).get("topics") or []):
                    return True
        except Exception:
            return True
        semantic_config = semantic_consistency_guard.load_semantic_invariants(
            self.project_dir or os.path.dirname(plot_dir)
        )
        if not semantic_config.get("valid"):
            return True
        if any(
            item.get("status") == "FAIL"
            for item in story_architect.audit_story_bible(plot_dir, self.config)
        ):
            return True
        if not continuity_guard.load_canon(plot_dir):
            return True
        return False

    def _commercial_blueprint_path(self):
        return os.path.join(generator.DIRS["plot"], "commercial_blueprint.json")

    def _load_commercial_blueprint(self):
        path = self._commercial_blueprint_path()
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _commercial_blueprint_needed(self):
        if not self.config.get("commercial_project_gate_enabled", True):
            return False
        # 已经存在正式正文的旧项目不允许自动重做立项，以免反向污染正史。
        if self._project_has_official_chapters():
            return False
        min_score = max(60, min(95, int(self.config.get("commercial_project_min_score", 78) or 78)))
        return not commercial_planner.blueprint_matches(
            self._load_commercial_blueprint(),
            str(self.config.get("book_title") or ""),
            str(self.config.get("genre_template") or ""),
            str(self.config.get("book_brief") or ""),
            self._get_story_planned_end_chapter(),
            min_score=min_score,
        )

    def _commercial_contract_context(self, max_chars=7000):
        parts = []
        # Active correction debt comes first so a long blueprint cannot push
        # it past the prompt budget.
        if self.config.get("revision_debt_enabled", True):
            debt_path = os.path.join(generator.DIRS["plot"], "revision_debt.json")
            debt_context = commercial_reviewer.render_revision_debt(
                commercial_reviewer.load_revision_debt(debt_path),
                current_chapter=int(getattr(self, "latest_chap", 0) or 0),
                max_chars=2600,
            )
            if debt_context:
                parts.append(debt_context)
        action_path = os.path.join(generator.DIRS["plot"], "商业审稿行动单.md")
        action_text = generator.read_text_safe(action_path).strip()
        if action_text:
            parts.append("# 最近阶段审稿行动单\n" + action_text[:2600])
        blueprint = self._load_commercial_blueprint()
        if blueprint:
            parts.append(commercial_planner.render_blueprint(blueprint, max_chars=max_chars))
        return "\n\n".join(parts)[:max_chars]

    def _ensure_commercial_blueprint(self, title, genre, total):
        """Automatically create and enforce the pre-writing commercial gate."""
        if not self.config.get("commercial_project_gate_enabled", True):
            return {}
        brief = str(self.config.get("book_brief") or "").strip()
        min_score = max(60, min(95, int(self.config.get("commercial_project_min_score", 78) or 78)))
        existing = self._load_commercial_blueprint()
        if commercial_planner.blueprint_matches(
            existing, title, genre, brief, total, min_score=min_score
        ):
            return existing
        if self._project_has_official_chapters():
            # Legacy/in-progress books must be audited, not silently re-conceived.
            return {}

        if self._stop_event.is_set():
            raise RuntimeError("用户已停止自动准备")
        self._ui_progress_append("  正在读取番茄官方榜单证据并进行商业立项...\n")
        evidence = commercial_planner.fetch_fanqie_market_evidence(timeout=8)
        _atomic_write_text(
            os.path.join(generator.DIRS["plot"], "market_evidence.json"),
            json.dumps(evidence, ensure_ascii=False, indent=2),
        )
        rules_context = publishing_rules.render_rules_document(
            self._build_effective_project_rules()
        )
        base_system, base_user = commercial_planner.build_planning_prompts(
            title, genre, brief, total, evidence, publishing_context=rules_context
        )
        attempts = max(1, min(3, int(self.config.get("commercial_project_max_attempts", 2) or 2)))
        best = {}
        best_score = -1
        issues = ["尚未生成方案"]
        for attempt in range(attempts):
            if self._stop_event.is_set():
                raise RuntimeError("用户已停止自动准备")
            if attempt:
                system_prompt, user_prompt = commercial_planner.build_revision_prompts(
                    base_system, base_user, best, issues, min_score
                )
            else:
                system_prompt, user_prompt = base_system, base_user
            raw = self.call_llm_review(
                system_prompt, user_prompt, temp=0.25, max_tokens=5200
            )
            candidate = commercial_planner.normalize_blueprint(
                commercial_planner.parse_json_object(raw),
                title,
                genre,
                brief,
                total,
                evidence,
            )
            if self._stop_event.is_set():
                raise RuntimeError("用户已停止自动准备")
            audit_system, audit_user = commercial_planner.build_gate_review_prompts(
                title, genre, brief, total, candidate, evidence
            )
            audit_raw = self.call_llm_review(
                audit_system, audit_user, temp=0.1, max_tokens=2200
            )
            candidate = commercial_planner.apply_gate_review(
                candidate, commercial_planner.parse_json_object(audit_raw)
            )
            candidate["attempt"] = attempt + 1
            score = int(candidate.get("commercial_score") or 0)
            if score > best_score:
                best = candidate
                best_score = score
            issues = commercial_planner.validate_blueprint(
                candidate, min_score=min_score, require_gate=True
            )
            self._append_batch_audit({
                "event": "commercial_project_gate_attempt",
                "attempt": attempt + 1,
                "score": score,
                "decision": candidate.get("decision"),
                "issues": issues[:20],
                "market_evidence_quality": evidence.get("quality"),
            })
            if not issues:
                best = candidate
                break

        final_issues = commercial_planner.validate_blueprint(
            best, min_score=min_score, require_gate=True
        )
        best["gate_status"] = "PASS" if not final_issues else "BLOCKED"
        best["gate_issues"] = final_issues
        final_title = str(best.get("final_title") or title).strip() or title
        if not final_issues and final_title != title:
            # 立项阶段尚无正文，可自动采用更清晰的商业书名。
            self._save_project_config_patch(book_title=final_title)
            best["project_fingerprint"] = commercial_planner.project_fingerprint(
                final_title, genre, brief, total
            )
        _atomic_write_text(
            self._commercial_blueprint_path(),
            json.dumps(best, ensure_ascii=False, indent=2),
        )
        _atomic_write_text(
            os.path.join(generator.DIRS["plot"], "商业立项执行合同.md"),
            commercial_planner.render_blueprint(best) + "\n",
        )
        if final_issues:
            raise RuntimeError(
                "商业立项未过关：" + "；".join(final_issues[:5])
            )
        self._ui_progress_append(
            f"  [OK] 商业立项通过：{best.get('commercial_score')}分；"
            f"目标读者、核心机制和黄金三章合同已锁定。\n"
        )
        return best

    def _refresh_readiness_status(self):
        if not hasattr(self, "readiness_lbl"):
            return
        if self._active_task_name:
            self.readiness_lbl.config(text=f"运行中：{self._active_task_name}", foreground="#9a6700")
            return
        if self._project_needs_setup():
            self.readiness_lbl.config(text="等待首次设置", foreground="#555555")
            self.btn_full_book.config(text="设置新书并开始")
            return
        if getattr(self, "_project_config_error", ""):
            self.readiness_lbl.config(text="项目模型配置无效", foreground="#b42318")
            self.btn_full_book.config(text="修复本书设置")
            return
        recovery_notice = self._staging_recovery_notice()
        if recovery_notice:
            if not self._has_configured_model_provider():
                provider = self.get_model_provider()
                self.readiness_lbl.config(
                    text=(
                        f"{recovery_notice}；"
                        + (
                            "需密钥才可重建正式链"
                            if provider == PROVIDER_DEEPSEEK
                            else "Codex CLI 尚不可用"
                        )
                    ),
                    foreground="#9a6700",
                )
                self.btn_full_book.config(
                    text=(
                        "填写密钥后重建正式链"
                        if provider == PROVIDER_DEEPSEEK
                        else "修复 Codex CLI 后重建正式链"
                    )
                )
            else:
                self.readiness_lbl.config(
                    text=f"{recovery_notice}；正式链仍需重建并过硬审",
                    foreground="#9a6700",
                )
                self.btn_full_book.config(text="重建正式链（需硬审）")
            return
        if not self._has_configured_model_provider():
            provider = self.get_model_provider()
            self.readiness_lbl.config(
                text=(
                    "需要 DeepSeek API Key"
                    if provider == PROVIDER_DEEPSEEK
                    else "Codex CLI 或登录态不可用"
                ),
                foreground="#b42318",
            )
            self.btn_full_book.config(
                text=(
                    "填写密钥并开始"
                    if provider == PROVIDER_DEEPSEEK
                    else "检查 Codex 后开始"
                )
            )
            return
        if self._initialization_needed():
            self.readiness_lbl.config(text="点击后自动准备并开始", foreground="#9a6700")
            self.btn_full_book.config(text="一键准备并生成整本小说")
            return
        trial_button_text = ""
        if self._trial_mode_pending():
            stop = self._get_trial_stop_chapter()
            _, _, _, latest_chap, _ = generator.get_latest_chapter_info()
            if latest_chap >= stop:
                if self._trial_gate_ready():
                    self.readiness_lbl.config(
                        text=f"第{stop}章试写已通过", foreground="#16794b"
                    )
                    self.btn_full_book.config(text="继续生成整本小说")
                else:
                    self.readiness_lbl.config(
                        text=f"第{stop}章硬闸门未通过", foreground="#b42318"
                    )
                    self.btn_full_book.config(text=f"第{stop}章闸门未通过")
                return
            trial_button_text = (
                f"继续生成至第{stop}章" if latest_chap > 0 else f"生成第1-{stop}章试写"
            )
        try:
            results = self._run_health_check_internal(
                future_window=int(self.config.get("health_check_future_chapters", 30) or 30)
            )
            fail_count = sum(1 for _, status in results if status == "FAIL")
            warn_count = sum(1 for _, status in results if status == "WARN")
        except Exception as exc:
            self.readiness_lbl.config(text=f"状态检查异常：{str(exc)[:28]}", foreground="#b42318")
            return
        if fail_count:
            self.readiness_lbl.config(text=f"阻止写入：{fail_count}项", foreground="#b42318")
        elif warn_count:
            self.readiness_lbl.config(text=f"可写（{warn_count}项提醒）", foreground="#9a6700")
        else:
            self.readiness_lbl.config(text="可写", foreground="#16794b")
        self.btn_full_book.config(text=trial_button_text or "一键生成整本小说")

    def toggle_all(self, var_dict, state):
        for var in var_dict.values():
            var.set(state)

    def refresh_knowledge_base(self):
        for widget in self.chars_frame.winfo_children():
            widget.destroy()
        for widget in self.world_frame.winfo_children():
            widget.destroy()
        for widget in self.plot_frame.winfo_children():
            widget.destroy()

        self.chars_map = generator.list_files_in_dir(generator.DIRS["chars"])
        self.chars_vars.clear()
        for name in self.chars_map:
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(self.chars_frame, text=name, variable=var).pack(anchor=tk.W)
            self.chars_vars[name] = var

        self.world_map = generator.list_files_in_dir(generator.DIRS["world"])
        self.world_vars.clear()
        for name in self.world_map:
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(self.world_frame, text=name, variable=var).pack(anchor=tk.W)
            self.world_vars[name] = var

        all_plot_files = generator.list_files_in_dir(generator.DIRS["plot"])
        internal_markers = ("草案", "_待审", "待审核", "备份", "自动修复前", "快速上手指南")
        self.plot_map = {
            name: path for name, path in all_plot_files.items()
            if not any(marker in name for marker in internal_markers)
        }
        self.plot_vars.clear()

        # Plot 区默认采用"最小安全集"：
        # 1) 只默认勾状态/约束类文件
        # 2) 只自动勾当前章节所属的活动细纲段
        # 3) 历史段、未来段、总纲、方法论、审查报告默认关闭
        safe_default_files = {
            "基调铁律",
            "核心设定",
            "全书大纲",
        }
        current_chap = getattr(self, "next_chap", None)

        def _is_current_activity_outline(fname):
            if current_chap is None:
                return False
            volume_name = self._get_story_volume_name(current_chap)
            if volume_name and volume_name in fname and "细纲" in fname:
                return True
            return False

        for name in self.plot_map:
            default_on = name in safe_default_files or _is_current_activity_outline(name)
            var = tk.BooleanVar(value=default_on)
            ttk.Checkbutton(self.plot_frame, text=name, variable=var).pack(anchor=tk.W)
            self.plot_vars[name] = var

        self.refresh_reader_files()

    # ============================================================
    # 章节阅读器 (Fix2: os.walk 递归 + 按章节号数字排序)
    # ============================================================
    def _extract_chap_num_from_path(self, fpath):
        basename = os.path.basename(fpath)
        m = re.search(r'第(\d+)章', basename)
        return int(m.group(1)) if m else 0

    def _is_official_chapter_file(self, filename):
        if not (filename.endswith(".txt") and filename.startswith("第") and "章" in filename):
            return False
        if "_待审" in filename or "_托管检查" in filename:
            return False
        return re.match(r'^第\d{1,5}章\.txt$', filename) is not None

    def _reader_requested_source(self):
        try:
            value = self.reader_source_var.get().strip()
        except (AttributeError, tk.TclError):
            return "official"
        if value == "5.6-sol待审稿":
            return "staging"
        if value == "正式稿":
            return "official"
        return "latest"

    def _reader_staging_dir(self):
        project_dir = getattr(self, "project_dir", "") or ""
        return os.path.join(project_dir, "rewrite_staging") if project_dir else ""

    def _reader_staged_chapter_ready(self, filepath):
        """Only expose complete, deterministic-gate-passing staged chapters."""
        chapter = self._extract_chap_num_from_path(filepath)
        project_dir = getattr(self, "project_dir", "") or ""
        if chapter <= 0 or not project_dir:
            return False
        try:
            stat = os.stat(filepath)
            semantic_path = os.path.join(
                project_dir, "plot", semantic_consistency_guard.CONFIG_FILENAME
            )
            semantic_mtime = os.path.getmtime(semantic_path)
            minimum = int(self.config.get("chapter_char_min", 3200) or 3200)
            maximum = int(
                self.config.get("chapter_char_target_max", 3800) or 3800
            )
            key = (
                os.path.abspath(filepath),
                stat.st_mtime_ns,
                stat.st_size,
                semantic_mtime,
                minimum,
                maximum,
            )
            cache = getattr(self, "_reader_staging_validation_cache", None)
            if not isinstance(cache, dict):
                cache = {}
                self._reader_staging_validation_cache = cache
            if key in cache:
                return bool(cache[key])
            text = generator.read_text_safe(filepath)
            result = chapter_validator.validate_candidate(
                project_dir,
                chapter,
                text,
                {"min": minimum, "max": maximum},
                expected_title_required=True,
            )
            ready = bool(result.get("passed"))
            if len(cache) > 500:
                cache.clear()
            cache[key] = ready
            return ready
        except (OSError, TypeError, ValueError):
            return False

    def _collect_reader_chapter_files(self, source_mode=None):
        """Collect one clearly separated reader source without promoting drafts."""
        mode = source_mode or self._reader_requested_source()

        def collect(root_dir, *, staged=False):
            paths = []
            if not root_dir or not os.path.isdir(root_dir):
                return paths
            for current, dirs, files in os.walk(root_dir):
                dirs[:] = [
                    name for name in dirs
                    if name not in {".backup", "archive", "superseded"}
                ]
                for filename in files:
                    if not self._is_official_chapter_file(filename):
                        continue
                    path = os.path.join(current, filename)
                    if staged and not self._reader_staged_chapter_ready(path):
                        continue
                    paths.append(path)
            return sorted(
                paths,
                key=lambda path: (self._extract_chap_num_from_path(path), path),
            )

        staging = collect(self._reader_staging_dir(), staged=True)
        official = collect(generator.DIRS.get("out", "output"))
        if mode == "staging":
            return staging, "staging"
        if mode == "official":
            return official, "official"
        # "latest" is a per-chapter view: a validated staged rewrite replaces
        # only the official chapter with the same number.  Other official
        # chapters stay visible, and path-based labels still identify each
        # row's real source without promoting a staged file to official.
        latest_by_chapter = {
            self._extract_chap_num_from_path(path): path for path in official
        }
        latest_by_chapter.update({
            self._extract_chap_num_from_path(path): path for path in staging
        })
        latest = sorted(
            latest_by_chapter.values(),
            key=lambda path: (self._extract_chap_num_from_path(path), path),
        )
        return latest, "latest"

    def _reader_source_label(self, filepath):
        staging_dir = self._reader_staging_dir()
        if staging_dir:
            try:
                if os.path.commonpath(
                    [os.path.abspath(filepath), os.path.abspath(staging_dir)]
                ) == os.path.abspath(staging_dir):
                    return "5.6-sol待审稿"
            except (OSError, ValueError):
                pass
        return "正式稿"

    def refresh_reader_files(self):
        (
            self.reader_chapter_files,
            self.reader_active_source,
        ) = self._collect_reader_chapter_files()
        if self.reader_chapter_files:
            self.reader_chapter_idx = len(self.reader_chapter_files) - 1
        else:
            self.reader_chapter_idx = 0
        self._update_reader_selector()
        if self.reader_chapter_files:
            self.reader_load()

    def reader_load(self):
        if not self.reader_chapter_files:
            self.reader_lbl.config(text="暂无章节")
            return
        idx = self.reader_chapter_idx
        filepath = self.reader_chapter_files[idx]
        filename = os.path.basename(filepath)
        choices = getattr(self, "reader_chapter_choices", [])
        label = choices[idx] if idx < len(choices) else self._reader_choice_label(filepath)
        source = self._reader_source_label(filepath)
        self.reader_lbl.config(
            text=f"{source} · {filename}  ({idx + 1}/{len(self.reader_chapter_files)})"
        )
        self.reader_pick_var.set(label)
        content = generator.read_text_safe(filepath)
        # Bind the visible editor to the exact disk snapshot it was loaded
        # from.  The reader intentionally remains editable for the user's
        # paste workflow, but an edit must never inherit the source file's
        # review label by accident.
        self._reader_loaded_path = os.path.abspath(filepath)
        self._reader_loaded_content = content
        self._reader_copy_provenance_state = "loaded"
        self.reader_text.delete(1.0, tk.END)
        self.reader_text.insert(tk.END, content)
        self.reader_text.see(1.0)

    def reader_refresh_current(self):
        self.refresh_reader_files_silent()
        if self.reader_chapter_files:
            self.reader_load()
        else:
            self.reader_lbl.config(text="暂无章节")
            self.reader_text.delete(1.0, tk.END)

    def reader_change_source(self, event=None):
        self.reader_chapter_idx = 0
        self.refresh_reader_files()
        if not self.reader_chapter_files:
            requested = self._reader_requested_source()
            label = "5.6-sol待审稿" if requested == "staging" else "正式稿"
            self.reader_lbl.config(text=f"{label}暂无章节")
            self.reader_text.delete(1.0, tk.END)

    def reader_prev(self):
        self.refresh_reader_files_silent()
        if self.reader_chapter_files and self.reader_chapter_idx > 0:
            self.reader_chapter_idx -= 1
            self.reader_load()

    def reader_next(self):
        self.refresh_reader_files_silent()
        if self.reader_chapter_files and self.reader_chapter_idx < len(self.reader_chapter_files) - 1:
            self.reader_chapter_idx += 1
            self.reader_load()

    def refresh_reader_files_silent(self):
        # Preserve the chapter currently being viewed when a new chapter is
        # committed.  Index-only preservation can select a different chapter
        # when files are added or removed before the current one.
        old_idx = self.reader_chapter_idx
        old_path = ""
        old_chapter = None
        if self.reader_chapter_files:
            try:
                old_path = os.path.abspath(
                    self.reader_chapter_files[
                        min(max(0, old_idx), len(self.reader_chapter_files) - 1)
                    ]
                )
                old_chapter = self._extract_chap_num_from_path(
                    self.reader_chapter_files[
                        min(max(0, old_idx), len(self.reader_chapter_files) - 1)
                    ]
                )
            except Exception:
                old_chapter = None
        (
            self.reader_chapter_files,
            self.reader_active_source,
        ) = self._collect_reader_chapter_files()
        exact = [
            index for index, path in enumerate(self.reader_chapter_files)
            if old_path and os.path.abspath(path) == old_path
        ]
        if exact:
            self.reader_chapter_idx = exact[0]
        elif old_chapter is not None:
            matching = [
                i
                for i, path in enumerate(self.reader_chapter_files)
                if self._extract_chap_num_from_path(path) == old_chapter
            ]
            if matching:
                self.reader_chapter_idx = matching[0]
            else:
                self.reader_chapter_idx = min(
                    old_idx, max(0, len(self.reader_chapter_files) - 1)
                )
        else:
            self.reader_chapter_idx = min(old_idx, max(0, len(self.reader_chapter_files) - 1))
        self._update_reader_selector()

    def _reader_choice_label(self, filepath):
        """Build a readable, unique-ish label for the chapter picker.

        The on-disk filename remains the canonical chapter identifier.  The
        label adds the title and (when applicable) the volume folder so the
        user can select a chapter without opening files one by one.
        """
        filename = os.path.basename(filepath)
        title = ""
        try:
            # Do not load a potentially 4k+ chapter just to build the picker.
            # Reading the first physical line is enough to recover its title.
            with open(filepath, "r", encoding="utf-8-sig", errors="replace") as handle:
                first_line = handle.readline().strip()
            if re.match(r"^第\s*\d+\s*章", first_line):
                title = re.sub(r"^第\s*\d+\s*章\s*", "", first_line).strip()
        except Exception:
            title = ""
        # Keep combobox rows compact; long generated titles are still visible
        # in the reading area and status label after selection.
        title = re.sub(r"\s+", " ", title)
        if len(title) > 22:
            title = title[:22] + "…"
        source = self._reader_source_label(filepath)
        label = f"[{source}] {filename} · {title}" if title else f"[{source}] {filename}"
        base_dir = (
            self._reader_staging_dir()
            if source == "5.6-sol待审稿"
            else generator.DIRS.get("out", "output")
        )
        try:
            relative_dir = os.path.relpath(os.path.dirname(filepath), base_dir)
        except (TypeError, ValueError):
            relative_dir = "."
        if relative_dir not in (".", ""):
            label += f" · {relative_dir}"
        return label

    def _update_reader_selector(self):
        values = []
        used = set()
        for filepath in self.reader_chapter_files:
            label = self._reader_choice_label(filepath)
            # The chapter number should normally be unique.  If a legacy
            # project contains duplicates, keep every entry selectable.
            if label in used:
                try:
                    relative = os.path.relpath(
                        os.path.dirname(filepath),
                        self._reader_staging_dir()
                        if self._reader_source_label(filepath) == "5.6-sol待审稿"
                        else generator.DIRS.get("out", "output"),
                    )
                except (TypeError, ValueError):
                    relative = os.path.dirname(filepath)
                label = f"{label} · {relative}"
            used.add(label)
            values.append(label)
        self.reader_chapter_choices = values
        self.reader_pick_combo["values"] = values
        if not values:
            self.reader_pick_var.set("")
        else:
            idx = min(max(0, self.reader_chapter_idx), len(values) - 1)
            self.reader_pick_var.set(values[idx])

    def reader_jump_to_selected(self, event=None):
        selected = self.reader_pick_var.get().strip()
        if not selected or not self.reader_chapter_files:
            return
        values = getattr(self, "reader_chapter_choices", [])
        if selected in values:
            self.reader_chapter_idx = values.index(selected)
            self.reader_load()

    def _extract_chapter_body_for_copy(self, content):
        if not content:
            return ""
        lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if lines:
            lines[0] = lines[0].lstrip("\ufeff")
        while lines and not lines[0].strip():
            lines = lines[1:]
        if lines and re.match(r"^第\s*\d+\s*章", lines[0].strip()):
            lines = lines[1:]
            while lines and not lines[0].strip():
                lines = lines[1:]
        return "\n".join(lines).strip()

    def _chapter_copy_text(self, content, include_title=True):
        """Return one clean, self-contained chapter for the clipboard."""
        normalized = (content or "").replace("\r\n", "\n").replace("\r", "\n")
        normalized = normalized.lstrip("\ufeff\n").strip()
        if include_title:
            return normalized
        return self._extract_chapter_body_for_copy(normalized)

    def _current_reader_content(self):
        # Copy exactly what is visible in the reader.  This matters when the
        # user makes a small manual edit before pasting a chapter to a
        # publishing platform; previously the clipboard silently used the
        # unchanged disk file instead.
        try:
            visible = self.reader_text.get(1.0, tk.END).strip()
        except (AttributeError, tk.TclError):
            visible = ""
        if visible:
            loaded_path = str(getattr(self, "_reader_loaded_path", "") or "")
            loaded_content = str(
                getattr(self, "_reader_loaded_content", "") or ""
            )
            visible_normalized = self._chapter_copy_text(
                visible, include_title=True
            )
            loaded_normalized = self._chapter_copy_text(
                loaded_content, include_title=True
            )
            if not loaded_path or visible_normalized != loaded_normalized:
                self._reader_copy_provenance_state = "local_edit_unreviewed"
            elif self._reader_source_label(loaded_path) == "5.6-sol待审稿":
                self._reader_copy_provenance_state = "staging_unpromoted"
            else:
                current_disk = generator.read_text_safe(loaded_path)
                if self._chapter_copy_text(
                    current_disk, include_title=True
                ) != loaded_normalized:
                    self._reader_copy_provenance_state = "disk_changed"
                else:
                    self._reader_copy_provenance_state = "official_disk_exact"
            return visible
        if self.reader_chapter_files:
            idx = min(max(0, self.reader_chapter_idx), len(self.reader_chapter_files) - 1)
            filepath = self.reader_chapter_files[idx]
            content = generator.read_text_safe(filepath)
            if content:
                self._reader_loaded_path = os.path.abspath(filepath)
                self._reader_loaded_content = content
                self._reader_copy_provenance_state = (
                    "staging_unpromoted"
                    if self._reader_source_label(filepath) == "5.6-sol待审稿"
                    else "official_disk_exact"
                )
                return content
        self._reader_copy_provenance_state = "unavailable"
        return ""

    def _reader_copy_provenance_label(self):
        state = str(
            getattr(self, "_reader_copy_provenance_state", "") or ""
        )
        return {
            "local_edit_unreviewed": "[注意]本地编辑未审查",
            "staging_unpromoted": "[待审]5.6-sol稿未正式入库",
            "disk_changed": "[注意]磁盘已变化，请刷新后再确认",
            "official_disk_exact": "[正式稿·与磁盘一致]",
        }.get(state, "[来源未确认]")

    def _copy_reader_content(self, include_title):
        content = self._chapter_copy_text(
            self._current_reader_content(),
            include_title=include_title,
        )
        if not content:
            self.reader_lbl.config(text="暂无可复制章节")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        base_text = re.split(r" \[OK\]已复制", self.reader_lbl.cget("text"), maxsplit=1)[0]
        suffix = "整章（含标题）" if include_title else "纯正文"
        provenance = self._reader_copy_provenance_label()
        self.reader_lbl.config(
            text=f"{base_text} [OK]已复制{suffix} {provenance}"
        )

    def reader_copy_full_chapter(self):
        self._copy_reader_content(include_title=True)

    def reader_copy_all(self):
        self._copy_reader_content(include_title=False)

    # ============================================================
    # 构建 System Prompt (含规则13: 反代词堆叠)
    # ============================================================
    def _estimate_token_count(self, text):
        if not text:
            return 0
        if tiktoken is not None:
            try:
                enc = tiktoken.get_encoding("cl100k_base")
                return len(enc.encode(text))
            except Exception:
                pass
        cjk = len(re.findall(r'[\u4e00-\u9fff]', text))
        other = max(0, len(text) - cjk)
        return int(cjk * 1.2 + other * 0.35) + 16

    def _truncate_keep_ends(self, text, max_chars, marker="\n[...已为控制上下文长度截断...]\n"):
        if not text or len(text) <= max_chars:
            return text
        if max_chars <= len(marker) + 40:
            return text[:max_chars]
        head_chars = int((max_chars - len(marker)) * 0.75)
        tail_chars = max_chars - len(marker) - head_chars
        return text[:head_chars].rstrip() + marker + text[-tail_chars:].lstrip()

    def _truncate_tag_block(self, prompt_text, tag_name, inner_limit):
        pattern = re.compile(rf'(<{tag_name}>\n?)(.*?)(\n?</{tag_name}>)', re.S)

        def repl(match):
            inner = match.group(2)
            if len(inner) <= inner_limit:
                return match.group(0)
            trimmed = self._truncate_keep_ends(inner, inner_limit)
            return f"{match.group(1)}{trimmed}{match.group(3)}"

        return pattern.sub(repl, prompt_text, count=1)

    def _apply_generation_prompt_budget(self, system_prompt, user_prompt):
        original_stats = {
            "system_chars": len(system_prompt),
            "user_chars": len(user_prompt),
        }
        original_stats["input_tokens"] = (
            self._estimate_token_count(system_prompt)
            + self._estimate_token_count(user_prompt)
            + 64
        )

        system_prompt = self._truncate_tag_block(system_prompt, "source_of_truth", 9000)
        system_prompt = self._truncate_tag_block(system_prompt, "plot_and_memo", 7000)
        system_prompt = self._truncate_tag_block(system_prompt, "world_building_rules", 5000)
        system_prompt = self._truncate_tag_block(system_prompt, "character_profiles", 4000)
        system_prompt = self._truncate_tag_block(system_prompt, "tone_rules", 1800)
        system_prompt = self._truncate_tag_block(system_prompt, "reveal_budget", 2500)

        if len(system_prompt) > self.GENERATION_SYSTEM_CHAR_CAP:
            system_prompt = self._truncate_keep_ends(system_prompt, self.GENERATION_SYSTEM_CHAR_CAP)
        if len(user_prompt) > self.GENERATION_USER_CHAR_CAP:
            user_prompt = self._truncate_keep_ends(user_prompt, self.GENERATION_USER_CHAR_CAP)

        est_tokens = self._estimate_token_count(system_prompt) + self._estimate_token_count(user_prompt) + 64
        while est_tokens > self.GENERATION_INPUT_TOKEN_BUDGET:
            changed = False
            if len(system_prompt) > self.GENERATION_MIN_SYSTEM_CHAR_CAP:
                next_cap = max(self.GENERATION_MIN_SYSTEM_CHAR_CAP, len(system_prompt) - 4000)
                new_system = self._truncate_keep_ends(system_prompt, next_cap)
                if len(new_system) < len(system_prompt):
                    system_prompt = new_system
                    changed = True
            if est_tokens > self.GENERATION_INPUT_TOKEN_BUDGET and len(user_prompt) > self.GENERATION_MIN_USER_CHAR_CAP:
                next_cap = max(self.GENERATION_MIN_USER_CHAR_CAP, len(user_prompt) - 1500)
                new_user = self._truncate_keep_ends(user_prompt, next_cap)
                if len(new_user) < len(user_prompt):
                    user_prompt = new_user
                    changed = True
            if not changed:
                break
            est_tokens = self._estimate_token_count(system_prompt) + self._estimate_token_count(user_prompt) + 64

        final_stats = {
            "system_chars": len(system_prompt),
            "user_chars": len(user_prompt),
            "input_tokens": est_tokens,
        }
        return system_prompt, user_prompt, {
            "trimmed": final_stats != original_stats,
            "original": original_stats,
            "final": final_stats,
        }

    def build_system_prompt_gui(self, current_prompt=""):
        prompt_parts = []
        prompt_parts.append(
            "你是一名成熟的网络小说作者，正在连载一部长篇作品。\n"
            "请严格遵守提供的数据库设定，你的目标是输出极具吸引力、行文流畅的【小说正文】。\n"
            "【绝对规则】：\n"
            "1. 只输出小说正文和章节标题！严禁用任何形式与读者互动、不准加注释、不准写摘要。\n"
            "2. 严禁使用任何Markdown格式！不准用#号标题、不准用**加粗、不准用*斜体，输出纯文本。禁止在标题前加#号！\n"
            "3. 不要使用过于翻译腔或播音腔的词汇，需符合网文阅读爽感与节奏。\n"
            "4. 如当前写作要求与既有设定冲突，以【时间线锚点/唯一真相设定/当前卷大纲/全局备忘录/伏笔表】为最高优先级；当前写作要求只能补充表现方式，不能推翻既有真相。\n"
            "5. 以下是你的全部记忆库，请严格遵循相应的设定标签。\n"
            "6. 正文不得用“第X章、本章、全章、上一章/下一章”等作者层编号回指剧情；一律改用具体事件、时间或地点锚点。\n"
            "7. 已建立的职责、权限、伤势与动作限制必须前后一致；若角色被限定只能观察或下令，就不能随后亲自执行已明确交给他人的动作，除非先写清授权变化及原因。\n"
        )

        truth_context = self._load_source_of_truth_context()
        if truth_context:
            prompt_parts.append(
                "<source_of_truth>\n"
                + truth_context +
                "\n【执行铁律】\n"
                "1. 不得改写已经发生过的核心事件顺序；具体事件锚点以唯一真相设定表、时间线锚点和 tool_rules.json 为准。\n"
                "2. 角色'以为'和世界'真实真相'必须分开写，不能把认知误差直接写成客观事实。\n"
                "3. 未铺垫的新势力、新Boss、新机制，禁止在本章空降成既定事实。\n"
                "4. 如果本章涉及高风险设定，宁可写得保守，也不要自创新版答案。\n"
                "</source_of_truth>\n"
            )

        # Reveal Budget 注入 (真相分级放行)
        reveal_budget = self._build_reveal_budget(self.next_chap)
        if reveal_budget:
            prompt_parts.append(reveal_budget)

        tone_rules_path = os.path.join(generator.DIRS["plot"], "基调铁律.txt")
        tone_rules = generator.read_text_safe(tone_rules_path)
        if tone_rules:
            prompt_parts.append(f"<tone_rules>\n{tone_rules}\n</tone_rules>\n")

        publishing_context = publishing_rules.render_rules_document(
            self._build_effective_project_rules()
        )
        if publishing_context:
            prompt_parts.append(
                "<publishing_and_theme_rules>\n"
                + publishing_context[:5200]
                + "</publishing_and_theme_rules>\n"
            )

        commercial_context = self._commercial_contract_context(max_chars=5600)
        if commercial_context:
            prompt_parts.append(
                "<commercial_reader_contract>\n"
                + commercial_context
                + "\n【执行要求】本章必须推进读者承诺或阶段行动单；不能只重复冲突、只埋谜不兑现。\n"
                "</commercial_reader_contract>\n"
            )

        # 批量模式：白名单喂料，只喂核心设定文件
        # 手动模式：仍按用户勾选
        selected_plot = []
        for name, var in self.plot_vars.items():
            if self.is_batch_running:
                # 托管正文已经收到阶段真相、故事契约、事实库和当前章细纲。
                # 不再重复注入原始 plot 文件，避免作者层真相和重复文本污染输入。
                continue
            else:
                if not var.get():
                    continue
            if "唯一真相" in name or "当前卷大纲" in name:
                # 这两类文件由 source_of_truth 转换成阶段可见版本。
                continue
            content = generator.read_text_safe(self.plot_map[name])
            if content:
                # 所有 plot 文件都过 reveal 过滤
                content = self._filter_spoilers_from_text(content, self.next_chap)
                # 截断防 token 爆炸
                if len(content) > 3000:
                    content = content[:3000] + "\n[...已截断...]"
                selected_plot.append(f"【{name}】\n{content}")
        if selected_plot:
            prompt_parts.append("<plot_and_memo>\n" + "\n\n".join(selected_plot) + "\n</plot_and_memo>\n")

        rag = rag_engine.SimpleLocalRAG()
        doc_idx = 0
        selected_char_names = (
            self._batch_selected_chars
            if self.is_batch_running
            else {name for name, var in self.chars_vars.items() if var.get()}
        )
        for name, var in self.chars_vars.items():
            if name in selected_char_names:
                content = generator.read_text_safe(self.chars_map[name])
                content = self._filter_spoilers_from_text(content, self.next_chap)
                rag.add_document(f"char_{doc_idx}", self.chars_map[name], name, content)
                doc_idx += 1
        selected_world_names = (
            self._batch_selected_world
            if self.is_batch_running
            else {name for name, var in self.world_vars.items() if var.get()}
        )
        for name, var in self.world_vars.items():
            if name in selected_world_names:
                content = generator.read_text_safe(self.world_map[name])
                content = self._filter_spoilers_from_text(content, self.next_chap)
                rag.add_document(f"world_{doc_idx}", self.world_map[name], name, content)
                doc_idx += 1

        search_query = current_prompt
        if self.latest_filepath and os.path.exists(self.latest_filepath):
            try:
                prev = generator.read_text_safe(self.latest_filepath)
                search_query += "\n" + prev[-500:]
            except Exception:
                pass

        results = rag.search(search_query, top_k=5, threshold=0.01)
        selected_chars = []
        selected_world = []
        for res in results:
            doc_id, score, title, content = res
            if doc_id.startswith("char_"):
                selected_chars.append(f"【{title}】\n{self._truncate_keep_ends(content, self.RAG_DOC_CHAR_LIMIT)}")
            else:
                selected_world.append(f"【{title}】\n{self._truncate_keep_ends(content, self.RAG_DOC_CHAR_LIMIT)}")
        if not results:
            selected_chars = [
                f"【{name}】\n{self._truncate_keep_ends(generator.read_text_safe(self.chars_map[name]), self.RAG_DOC_CHAR_LIMIT)}"
                for name, var in self.chars_vars.items() if var.get()
            ][:3]
            selected_world = [
                f"【{name}】\n{self._truncate_keep_ends(generator.read_text_safe(self.world_map[name]), self.RAG_DOC_CHAR_LIMIT)}"
                for name, var in self.world_vars.items() if var.get()
            ][:4]

        if selected_chars:
            prompt_parts.append("<character_profiles>\n" + "\n\n".join(selected_chars) + "\n</character_profiles>\n")
        if selected_world:
            prompt_parts.append("<world_building_rules>\n" + "\n\n".join(selected_world) + "\n</world_building_rules>\n")

        char_limits = self._get_chapter_char_limits()
        prompt_parts.append(
            "\n\n<chapter_quality_rules>\n"
            "【单章质量控制与多线防丢指令】(极度重要)：\n"
            "1. 格式要求：正文第一行必须是章节标题，格式为 第X章 标题（4-8字直接概括核心事件，绝不能与上一章标题相似）。禁止使用任何Markdown格式，不要在标题前加#号。\n"
            "2. 严禁注水与循环：如果不满字数，必须主动推进大纲的下一个节点！\n"
            "3. 杜绝流水账套路：禁止使用千篇一律的开头。\n"
            "4. 人设绝对锁定(反OOC)：角色的行为逻辑必须严格符合<character_profiles>的设定。\n"
            f"5. 本章正文中文字数目标在{char_limits['target_min']}-{char_limits['target_max']}字之间，硬下限为{char_limits['min']}字。\n"
            "6. 视角统一：同一个场景内请保持主视角统一。\n"
            "7. 严禁说教与强行升华。\n"
            "8. 文笔要求：对话口语化有个性；避免连续三句以上相同句式；段落长短错落。\n"
            "9. 章节节拍：严格服从细纲的章节功能与结尾类型。冲突/高潮章可有2-3次情绪波动；关系、余波和铺垫章允许1-2次完整变化。章末必须形成具体的下一页期待，但不强制每章突发危机。\n"

            "11. 【多线剧情防遗漏】：如果备忘录中有分兵/分头行动的角色，必须简要交代他们的当前处境。\n"
            "12. 逻辑连贯：要时刻思考其他角色在同一时间轴下在做什么。\n"
            "13. 【反代词堆叠与高频词控制】：禁止连续3句以上用'他/她/它'开头；'大脑'一词每章不超过4次（可换脑子/意识/思维）；'角色名+的'结构每章不超过8次。\n"
            "14. 【反AI腔节奏】：禁止把一句完整意思拆成3-5个独立短段来强行制造电影感；章尾悬念最多保留1-2个短锤句，其余必须用正常叙述落地。\n"
            "15. 【反诗化堆词】：慎用'不是……是……''某种……''像是……'和破折号——来硬造神秘感；除对白停顿外，破折号要克制，优先用动作、场景和细节制造压迫感。\n"
            "16. 【禁作者层回指】：标题之外不得出现“第X章、本章、全章、上一章/下一章”等创作层编号；涉及旧事时写明具体事件、时间或地点。\n"
            "17. 【职责动作闭环】：角色的职责、权限、伤势和动作限制必须与后续实际动作一致；发生授权变化时必须先写出现场原因和明确交接。\n"
            "</chapter_quality_rules>\n"
        )
        return "\n".join(prompt_parts)

    def _load_source_of_truth_context(self):
        plot_dir = generator.DIRS["plot"]
        preferred_files = [
            "时间线锚点.txt",
            "全局备忘录.txt",
            "伏笔与因果追踪表.txt",
        ]
        stage_truth = story_architect.build_stage_truth_context(plot_dir, self.next_chap, max_chars=2800)
        stage_story = story_architect.build_story_context(plot_dir, self.next_chap, max_chars=2400)
        chunks = [part for part in (stage_truth, stage_story) if part]
        added = set()

        for fname in preferred_files:
            fpath = os.path.join(plot_dir, fname)
            if os.path.exists(fpath):
                content = generator.read_text_safe(fpath)
                if content:
                    added.add(os.path.normcase(fpath))
                    content = self._filter_spoilers_from_text(content, self.next_chap)
                    chunks.append(f"【{fname}】\n{content[:2500]}")

        # 不再自动拼入全部细纲候选。当前章细纲已在 batch_worker 里
        # 作为 outline_block 强制注入 user_prompt，source_of_truth 只保留
        # 设定类文件，避免未来卷细纲污染输入。
        # (原来的 outline candidates 自动注入已移除)

        return "\n\n".join(chunks)

    def _truth_anchor_health_status(self, filename):
        """Accept structured simple-mode truth sources when legacy text mirrors are absent."""
        plot_dir = generator.DIRS["plot"]
        path = os.path.join(plot_dir, filename)
        if os.path.exists(path) and os.path.getsize(path) > 10:
            return "PASS"
        if self._strict_release_mode():
            return "FAIL"
        fallback_files = (
            "story_bible.json",
            continuity_guard.CANON_FILENAME,
            semantic_consistency_guard.CONFIG_FILENAME,
            "关键事实库.json",
        )
        fallback_ready = all(
            os.path.exists(os.path.join(plot_dir, name))
            and os.path.getsize(os.path.join(plot_dir, name)) > 10
            for name in fallback_files
        )
        return "PASS" if fallback_ready else "WARN"

    # ============================================================
    # 真相分级放行系统 (Reveal Guard v2)
    # ============================================================
    _reveal_rules_cache = None

    def _load_reveal_rules(self):
        """加载 reveal_rules.json，带内存缓存"""
        if self._reveal_rules_cache is not None:
            return self._reveal_rules_cache
        rules_path = os.path.join(generator.DIRS["plot"], "reveal_rules.json")
        if not os.path.exists(rules_path):
            return None
        try:
            with open(rules_path, "r", encoding="utf-8") as f:
                self._reveal_rules_cache = json.load(f)
            return self._reveal_rules_cache
        except Exception:
            return None

    def _get_max_reveal_level(self, topic, chap_num):
        """根据章节号和topic配置，返回允许的最大表达等级(1-5)"""
        if chap_num >= topic.get("earliest_hard", 9999):
            return 5
        elif chap_num >= topic.get("earliest_soft", 9999):
            return topic.get("max_level_before_hard", 3)
        elif chap_num >= topic.get("earliest_hint", 9999):
            return topic.get("max_level_before_soft", 2)
        else:
            return topic.get("max_level_before_hint", 1)

    def _build_reveal_budget(self, chap_num):
        """根据当前章节号生成 <reveal_budget> prompt 块"""
        rules = self._load_reveal_rules()
        if not rules:
            return ""

        level_names = {
            1: "L1（氛围/怀疑）", 2: "L2（碎片证据）", 3: "L3（半坐实）",
            4: "L4（机制解释）", 5: "L5（客观真相）"
        }
        lines = [
            "<reveal_budget>",
            f"【本章真相预算 — 第{chap_num}章】",
            "以下真相在本章只允许写到指定等级，超出即为违规：",
            ""
        ]

        for topic in rules.get("topics", []):
            max_lv = self._get_max_reveal_level(topic, chap_num)
            if max_lv >= 5:
                continue  # 已完全放开，不需要限制

            label = str(topic.get("label") or topic.get("id") or "未公开主题")
            lines.append(f"[{label}] 最高 {level_names.get(max_lv, f'L{max_lv}')}")
            for ex in topic.get("allowed_examples", [])[:2]:
                lines.append(f"  [OK] 可以写：{ex}")
            lines.append("  [FAIL] 不得补全未公开名称、幕后因果、机制解释或客观答案。")
            lines.append("")

        lines.append("【铁律】如果不确定某句话是否超出预算，宁可写得更模糊。")
        lines.append("</reveal_budget>")
        return "\n".join(lines)

    def _filter_spoilers_from_text(self, text, chap_num):
        """过滤文本中超前于当前章节的剧透内容"""
        rules = self._load_reveal_rules()

        # 分级过滤：不是只拦 earliest_hint 前，而是在所有尚未 hard 放开的阶段
        # 都拦截对应的 hard_patterns。这样即使进入 hint/soft 区间，
        # L4/L5 级机制描述也不会从备忘录侧喂给模型。
        blocked_patterns = []
        for topic in (rules or {}).get("topics", []):
            max_lv = self._get_max_reveal_level(topic, chap_num)
            if max_lv < 4:  # 只有 L4+ 才允许机制描述，否则拦截
                blocked_patterns.extend(topic.get("hard_patterns", []))

        blocked_terms = story_architect.blocked_terms_for_chapter(
            generator.DIRS["plot"], chap_num
        )
        if not blocked_patterns and not blocked_terms:
            return text

        filtered_lines = []
        for line in text.split("\n"):
            hit = any(term in line for term in blocked_terms)
            for pat in blocked_patterns:
                if re.search(pat, line):
                    hit = True
                    break
            if hit:
                filtered_lines.append("[已隐藏：涉及后续章节揭示内容]")
            else:
                filtered_lines.append(line)
        return "\n".join(filtered_lines)

    def _run_truth_reveal_guard(self, chapter_text, chap_num):
        """真相分级放行检查：本地粗筛 + LLM语义判级"""
        rules = self._load_reveal_rules()
        if not rules:
            return {"status": "PASS", "summary": "reveal_rules.json 未找到，跳过检查"}

        level_names = {1: "L1", 2: "L2", 3: "L3", 4: "L4", 5: "L5"}

        # Step 1: 本地粗筛
        hits = []
        for topic in rules.get("topics", []):
            max_lv = self._get_max_reveal_level(topic, chap_num)
            if max_lv >= 5:
                continue

            for pat in topic.get("hard_patterns", []):
                # 逐行匹配，防止 .* 跨段误报
                for line_idx, line in enumerate(chapter_text.split("\n")):
                    if not re.search(pat, line):
                        continue
                    # 取上下各一行作为上下文
                    all_lines = chapter_text.split("\n")
                    ctx_start = max(0, line_idx - 1)
                    ctx_end = min(len(all_lines), line_idx + 2)
                    ctx = " ".join(l.strip() for l in all_lines[ctx_start:ctx_end] if l.strip())
                    denial = any(x in ctx for x in [
                        "不信", "不会", "不是", "不知道", "不确定",
                        "如果", "也许", "可能", "假设", "或许"
                    ])
                    hits.append({
                        "topic_id": topic["id"],
                        "label": topic["label"],
                        "pattern": pat,
                        "context": ctx[:120],
                        "max_allowed": max_lv,
                        "has_denial": denial,
                    })

        # Step 1.5: 如果本地粗筛无命中，但有 semantic_triggers，
        # 仍然执行一次轻量 LLM 语义扫描（消费 semantic_triggers）。
        # 这是防止模型换说法绕过固定词表的关键防线。
        if not hits:
            active_triggers = []
            for topic in rules.get("topics", []):
                max_lv = self._get_max_reveal_level(topic, chap_num)
                if max_lv < 3:  # 只有 L1/L2 区间才需要语义扫描
                    for trig in topic.get("semantic_triggers", []):
                        active_triggers.append(f"[{topic['label']}] {trig}")

            if not active_triggers:
                return {"status": "PASS", "summary": "本地粗筛无命中，无活跃语义触发器"}

            # 用 LLM 做语义扫描
            scan_prompt = (
                "你是小说真相揭示节奏的审查员。请检查以下章节正文是否包含任何被禁止的真相揭示。\n\n"
                "【禁止揭示清单】（以下内容在本章不允许被坐实或详细解释）：\n"
                + "\n".join(active_triggers) + "\n\n"
                "判断规则：\n"
                "- 如果正文用近义词/换说法表达了上述禁止内容，仍然算违规\n"
                "- 如果只是模糊暗示、角色怀疑、传闻，不算违规\n"
                "- 如果是角色故意用假设语气（'如果''也许'），不算违规\n\n"
                "输出格式：\n"
                "FINAL: PASS 或 WARN 或 FAIL\n"
                "REASON: 一句话总结\n"
            )
            scan_user = f"当前章节：第{chap_num}章\n\n【正文片段（前3000字）】\n{chapter_text[:3000]}"

            try:
                result = self.call_llm_review(scan_prompt, scan_user, temp=0.1, max_tokens=300)
                final_match = re.search(r'FINAL:\s*(PASS|WARN|FAIL)', result, re.IGNORECASE)
                reason_match = re.search(r'REASON:\s*(.+)', result)
                status = final_match.group(1).upper() if final_match else "PASS"
                summary = reason_match.group(1).strip() if reason_match else "语义扫描完成"
                return {"status": status, "summary": f"[语义扫描] {summary}", "raw": result}
            except Exception:
                return {"status": "WARN", "summary": "语义扫描失败，降级为WARN（strict模式下会拦截）"}

        # Step 2: LLM 语义判级（有硬词命中时）
        hit_descs = []
        for i, h in enumerate(hits[:5]):
            hit_descs.append(
                f"命中{i+1}: [{h['label']}] 关键词「{h['pattern']}」\n"
                f"  上下文: {h['context']}\n"
                f"  当前章节允许最高: {level_names.get(h['max_allowed'], '?')}\n"
                f"  是否有否定/质疑语境: {'是' if h['has_denial'] else '否'}"
            )

        judge_prompt = (
            "你是小说真相揭示节奏的审查员。请判断以下命中片段的实际表达等级。\n\n"
            "表达等级定义：\n"
            "L1=氛围/怀疑（做梦、传闻、不对劲）\n"
            "L2=碎片证据（模糊影像、别人说过但不确定）\n"
            "L3=半坐实（角色推断但正文不背书）\n"
            "L4=机制解释（原理、因果、技术细节）\n"
            "L5=客观真相（旁白/高可信角色定案）\n\n"
            "判断规则：\n"
            "- 如果有明确的否定/质疑语境（'不信''如果''也许'），实际等级降一级\n"
            "- 反派口供如果正文没有反质疑，按原等级算\n"
            "- 出现在角色内心假设中且用了'如果'，最多算L2\n\n"
            "对每个命中，输出一行：\n"
            "HIT_N: ACTUAL_LEVEL=L? VERDICT=PASS/WARN/FAIL\n"
            "最后一行输出：\n"
            "FINAL: PASS 或 WARN 或 FAIL\n"
            "REASON: 一句话总结\n"
        )
        user_prompt = f"当前章节：第{chap_num}章\n\n" + "\n\n".join(hit_descs)

        try:
            result = self.call_llm_review(judge_prompt, user_prompt, temp=0.1, max_tokens=400)
        except Exception:
            hard_hits = [h for h in hits if not h["has_denial"]]
            if hard_hits:
                return {"status": "WARN", "summary": f"LLM判级失败，本地检测{len(hard_hits)}处无否定命中: {hard_hits[0]['pattern']}"}
            return {"status": "PASS", "summary": "LLM判级失败，本地命中均有否定上下文"}

        final_match = re.search(r'FINAL:\s*(PASS|WARN|FAIL)', result, re.IGNORECASE)
        reason_match = re.search(r'REASON:\s*(.+)', result)
        status = final_match.group(1).upper() if final_match else "WARN"
        summary = reason_match.group(1).strip() if reason_match else f"命中{len(hits)}处关键词"

        return {"status": status, "summary": summary, "raw": result, "hits": hits}

    def _run_outline_reveal_guard(self, outline_text, chap_num):
        """对逐章细纲做真相越界检查，防止脏细纲直接喂给模型"""
        if not outline_text or not outline_text.strip():
            return {"status": "PASS", "summary": "逐章细纲为空，跳过检查"}

        result = self._run_truth_reveal_guard(outline_text, chap_num)
        if result.get("status") == "PASS":
            return result

        summary = result.get("summary", "逐章细纲存在超前真相")
        result["summary"] = f"第{chap_num}章细纲存在超前真相/机制信息：{summary}"
        return result

    def _run_quality_gate(self, chapter_content, chapter_outline="", prev_content=""):
        """调用 quality_gate.json 进行质检，返回 PASS/FAIL 结果字符串"""
        try:
            import skill_engine
        except ImportError:
            return "FAIL [质检裸奔] skill_engine 模块缺失，质检未执行"

        skills_dir = os.path.join(_exe_dir, "skills")
        if not os.path.isdir(skills_dir):
            skills_dir = os.path.join(self.project_dir, "skills")
        if not os.path.isdir(skills_dir):
            return "FAIL [质检裸奔] skills 目录不存在，质检未执行"

        gate_path = os.path.join(skills_dir, "quality_gate.json")
        if not os.path.exists(gate_path):
            return "FAIL [质检裸奔] quality_gate.json 不存在，质检未执行"

        try:
            with open(gate_path, "r", encoding="utf-8") as f:
                gate_config = json.load(f)
        except Exception:
            return "FAIL [质检裸奔] quality_gate.json 解析失败，质检未执行"

        # Build the same continuity context used for generation. Reviewing a
        # chapter against only its outline and 200 characters of prior text
        # misses inventory, injury, realm, and information-provenance errors.
        extra_parts = [
            "【通用能力与信息边界】角色只能使用正文或项目设定中已经取得的知识、技能、"
            "权限、物品和资源；不得凭空掌握新能力、知道未知事实或无代价消除既定限制。"
            "如果项目设定存在能力、资源或世界规则代价，必须完整保留。"
        ]
        char_limits = self._get_chapter_char_limits()
        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", chapter_content or ""))
        style_counts = {
            "像是/仿佛/似乎": sum((chapter_content or "").count(word) for word in ("像是", "仿佛", "似乎")),
            "深吸一口气": (chapter_content or "").count("深吸一口气"),
        }
        extra_parts.append(
            "【审核裁决边界（必须服从）】\n"
            f"- 程序实测中文字符数为 {chinese_chars}，项目硬下限为 {char_limits['min']}；"
            "已达到硬下限且完成核心事件时，不得仅因节奏或心理描写偏多判定‘内容空洞’。\n"
            "- 截止时间、上级要求提交结论、舆论与职业责任均属于外部压力；"
            "‘压力下触发’不等于必须被他人强迫接触。\n"
            "- ‘右臂’与‘右前臂’的范围措辞若没有改变能力代价、伤势等级或后续行动能力，"
            "只能给 WARN；只有明确扩大伤势并破坏后续状态时才可 FAIL。\n"
            f"- 程序实测风格计数：{json.dumps(style_counts, ensure_ascii=False)}。"
            "阈值写‘超过4次’时，4次本身不触发；不得自行把阈值改成‘达到4次’。\n"
            "- 对细纲按因果结果核验，不要求正文逐字照抄；只有核心事件缺失、"
            "不可逆状态相反或后续承接被破坏时才可 FAIL。\n"
            "- 专业观察、尸检证据和逻辑推断可以在能力触发前出现；只要没有提前出现超自然感知、"
            "能力效果或指定的核心联想，就不属于‘能力提前触发’。\n"
            "- 章末钩子允许在主角确认异常的瞬间直接收章；若正文先明确写某人离开，后写异常发生，"
            "就已满足先后顺序，不得要求逐字出现‘离开后’。悬疑钩子不需要在同章解释原因，"
            "也不得把未解释的物品异常擅自归因于主角能力。人物离开与异常发现之间允许出现缝合、"
            "整理器械、摘手套等不改变因果的日常收尾动作；‘在其离开后发现’不等于‘必须离开后立刻发现’，"
            "只要离开发生在前、异常发现发生在后，就不得因此判定顺序失败。"
        )
        policy_context = publishing_rules.render_rules_document(
            self._build_effective_project_rules()
        )
        commercial_context = self._commercial_contract_context(max_chars=5200)
        if policy_context:
            extra_parts.append(f"【发布与题材规则】\n{policy_context[:5000]}")
        if chapter_outline:
            extra_parts.append(f"【本章细纲】\n{chapter_outline}")
        if prev_content:
            extra_parts.append(f"【上一章结尾】\n{prev_content[-600:]}")
        chapter_match = re.match(r"\s*第\s*(\d+)\s*章", chapter_content or "")
        review_chapter = int(chapter_match.group(1)) if chapter_match else 0
        first_line = (chapter_content or "").strip().splitlines()[0].strip() if chapter_content else ""
        if review_chapter and re.match(
            rf"^第\s*{review_chapter}\s*章\s+\S+",
            first_line,
        ):
            extra_parts.append(
                "【机械格式核验】正文第一行已经通过程序校验："
                f"{first_line}。不得再以缺少章节标题、章节号或标题空格为由判 FAIL；"
                "仍须独立检查正文内容与细纲。"
            )
        plot_dir = generator.DIRS.get("plot", "")
        if plot_dir:
            fact_context = knowledge_manager.build_generation_context(
                plot_dir, review_chapter, max_chars=2600
            )
            if fact_context:
                extra_parts.append(f"【关键事实库】\n{fact_context}")
            canon_context = continuity_guard.build_canon_context(plot_dir, max_chars=2200)
            if canon_context:
                extra_parts.append(canon_context)
            story_context = story_architect.build_story_context(
                plot_dir, review_chapter, max_chars=1800
            )
            if story_context:
                extra_parts.append(story_context)
        chars_dir = generator.DIRS.get("chars", "")
        main_char_file = self.config.get("main_character_file", "")
        char_candidates = []
        if main_char_file:
            char_candidates.append(os.path.join(chars_dir, main_char_file))
        char_candidates.extend(sorted(glob.glob(os.path.join(chars_dir, "*.txt"))))
        for main_char_path in char_candidates:
            if os.path.exists(main_char_path):
                char_card = generator.read_text_safe(main_char_path)
                if char_card:
                    extra_parts.append(f"【角色卡】\n{char_card[:1000]}")
                    break
        extra_context = "\n\n".join(extra_parts)

        def llm_fn(sys_p, user_p, temp):
            return self.call_llm_review(sys_p, user_p, temp=temp, max_tokens=1200)

        result = skill_engine.execute_skill(
            gate_config, chapter_content, llm_fn, extra_context=extra_context
        )
        return result.strip()

    def _run_release_guard(self, chapter_text, chap_num, prev_content=""):
        issues = generator.run_consistency_check(chapter_text, chap_num)
        forbidden = [i for i in issues if i["level"] == "[BAN] 禁止"]
        tool_rules = self._load_tool_rules()
        high_risk_words = set(tool_rules.get("high_risk_watch_keywords") or self.HIGH_RISK_WATCH_KEYWORDS)
        style_risk_words = set(tool_rules.get("style_risk_keywords") or self.STYLE_RISK_KEYWORDS)
        high_risk = [i for i in issues if i["keyword"] in high_risk_words]
        style_risk = [i for i in issues if i["keyword"] in style_risk_words]

        if not forbidden and not high_risk and not style_risk:
            return {"status": "PASS", "summary": "未触发高风险设定拦截。", "issues": issues}

        # 简单入库模式不再为了本地扫描提示逐章调用模型。硬禁项和
        # 高风险设定继续阻断；纯风格问题只提示，由阶段审稿统一判断。
        if not self._strict_release_mode():
            blocking = forbidden + high_risk
            if blocking:
                summary = "；".join(
                    f"{item['keyword']}：{item['reason']}"
                    for item in blocking[:6]
                )
                return {
                    "status": "FAIL",
                    "summary": summary or "触发发布硬禁项",
                    "issues": issues,
                }
            summary = "；".join(
                f"{item['keyword']}：{item['reason']}"
                for item in style_risk[:6]
            )
            return {
                "status": "WARN",
                "summary": summary or "检测到风格风险",
                "issues": issues,
            }

        truth_context = self._load_source_of_truth_context()
        gate_prompt = (
            "你是这本小说的'发布前设定总校'。你的职责不是润色，而是判断当前章节是否会"
            "改写既有真相、破坏时间线、把角色误判写成客观事实，或把同一核心事件重复写成新发生一次。\n"
            "请重点检查：\n"
            "1. 是否提前坐实唯一真相设定表或 reveal_rules.json 中未到章节的真相\n"
            "2. 是否把角色误判、传闻、伪线索写成客观事实\n"
            "3. 是否让已经发生的核心事件回档、重复发生或改写顺序\n"
            "4. 是否空降旧名、未授权组织、新Boss、新机制或违反 tool_rules.json 的硬禁项\n"
            "5. 章尾是否出现连续短句堆叠、破折号滥用、模糊词硬造氛围等明显AI腔\n"
            "6. 是否出现“第X章、本章、全章”等作者层回指，或角色职责、权限、伤势、动作限制前后冲突\n\n"
            "输出格式严格如下：\n"
            "GATE: PASS 或 WARN 或 FAIL\n"
            "REASON: 一句话总结\n"
            "DETAILS:\n"
            "- 列出具体问题，若无则写 无\n"
        )
        issue_lines = []
        for item in forbidden + high_risk:
            issue_lines.append(f"- {item['level']} [{item['keyword']}] {item['reason']} / {item['context']}")
        issue_text = "\n".join(issue_lines) if issue_lines else "无"
        user_prompt = (
            f"【章节号】第{chap_num}章\n"
            f"【上一章结尾】\n{prev_content[-600:] if prev_content else '无'}\n\n"
            f"【来源真相库】\n{truth_context if truth_context else '无'}\n\n"
            f"【本地关键词扫描】\n{issue_text}\n\n"
            f"【待发布章节】\n{chapter_text}"
        )
        try:
            result = self.call_llm_review(gate_prompt, user_prompt, max_tokens=1500)
        except Exception as e:
            return {"status": "WARN", "summary": f"设定总校调用失败：{str(e)[:80]}", "issues": issues}

        status_match = re.search(r'GATE:\s*(PASS|WARN|FAIL)', result, re.IGNORECASE)
        reason_match = re.search(r'REASON:\s*(.+)', result)
        status = status_match.group(1).upper() if status_match else "WARN"
        summary = reason_match.group(1).strip() if reason_match else result.splitlines()[0][:120]
        return {"status": status, "summary": summary, "issues": issues, "raw": result}

    # ============================================================
    # 细纲提取 (Section 9.1)
    # ============================================================
    def _extract_chapter_outline(self, chap_num):
        """从逐章细纲中提取当前章节对应的段落"""
        outline_files = self._get_outline_candidate_files(chap_num)
        cn_chap = _num_to_cn_chapter(chap_num)

        for ofile in outline_files:
            if not os.path.exists(ofile):
                continue
            content = generator.read_text_safe(ofile)
            if not book_initializer.has_meaningful_content(content):
                continue
            structured = story_architect.split_outline_sections(content)
            if chap_num in structured:
                return structured[chap_num]

            # Legacy outlines may use Chinese chapter numerals or chapter
            # ranges. Heading matches stay anchored so references such as
            # "第10章已完成" cannot reset extraction mid-section.
            lines = content.split("\n")
            capturing = False
            result_lines = []
            body_started = False
            for line in lines:
                is_match = False
                stripped = line.strip()
                if re.match(rf'^第\s*(?:{re.escape(cn_chap)}|{chap_num})\s*章(?:\s|$)', stripped):
                    is_match = True
                else:
                    range_match = re.match(r'^第\s*(\d+)\s*章?\s*[-—至]\s*第?\s*(\d+)\s*章', stripped)
                    if range_match:
                        start_c = int(range_match.group(1))
                        end_c = int(range_match.group(2))
                        if start_c <= chap_num <= end_c:
                            is_match = True
                            
                if is_match:
                    capturing = True
                    result_lines = [line]
                    body_started = False
                    continue
                    
                if capturing:
                    next_chap_match = re.match(r'^第\s*(\d+)\s*章', stripped)
                    if next_chap_match:
                        c_val = int(next_chap_match.group(1))
                        if c_val > chap_num:
                            break
                    if re.match(r'^={3,}', line):
                        if body_started:
                            continue
                        else:
                            continue
                    if line.strip():
                        body_started = True
                    result_lines.append(line)
            if result_lines:
                outline = "\n".join(result_lines).strip()
                if book_initializer.has_meaningful_content(outline):
                    return outline
        return ""

    def _auto_generate_outline(self, chap_num, prev_content):
        """批量模式：无细纲时根据卷大纲+备忘录+上文自动展开本章细纲"""
        if not self._has_configured_model_provider():
            return ""

        plot_dir = generator.DIRS["plot"]
        volume_name = self._get_story_volume_name(chap_num) or ""
        volume_outline = self._extract_volume_outline_from_master(volume_name)
        if not volume_outline:
            volume_outline = generator.read_text_safe(os.path.join(plot_dir, "全书大纲.txt"))
        memo = generator.read_text_safe(os.path.join(plot_dir, "全局备忘录.txt"))
        volume_outline = self._filter_spoilers_from_text(volume_outline, chap_num)
        memo = self._filter_spoilers_from_text(memo, chap_num)
        ledger_context = ""
        if self.config.get("structured_state_enabled", True):
            ledger_context, _ = state_ledger.render_context(
                state_ledger.load_state(plot_dir),
                chap_num,
                "",
                prev_content,
                max_chars=3600,
                stale_warn_chapters=max(3, int(self.config.get("subplot_stale_warn_chapters", 8) or 8)),
            )
            if self.config.get("temporal_memory_enabled", True):
                memory_hits = temporal_memory.retrieve(
                    plot_dir,
                    f"{volume_outline}\n{prev_content}",
                    before_chapter=chap_num,
                    max_hits=16,
                    max_chars=2200,
                )
                memory_text = temporal_memory.render_retrieval(memory_hits, max_chars=2200)
                if memory_text:
                    ledger_context += "\n\n" + memory_text

        sys_prompt = (
            "你是专业小说细纲规划师。根据阶段契约、备忘录和上一章结尾，"
            "为指定章节生成可执行的十四字段细纲。只输出细纲，不要解释。"
        )
        user_prompt = f"""请为第 {chap_num} 章生成细纲。

【卷大纲参考】：
{volume_outline[:2000]}

【全局备忘录】：
{memo[:800] if memo else "暂无"}

【经证据校验的正史状态、知情边界与待推进支线】：
{ledger_context if ledger_context else "暂无；禁止自行补全未知状态"}

【上一章结尾】：
{prev_content[-400:] if prev_content else "无"}

【硬性格式】
第{chap_num}章 标题：
章节功能：从冲突推进/调查发现/兑现回收/关系转折/代价余波/铺垫蓄力/高潮决断中选一项
核心事件：...
出场人物：...
承接锚点：...
人物选择与代价：...
反方行动：写对手、制度、环境或利益方的主动动作，不得只写“主角遇到阻力”
冲突/爽点：...
兑现类型：从能力/知识/关系/身份/资源/情绪/谜底/失败代价中选一项
伏笔/禁出内容：...
状态落点：境界=...；地点=...；伤势=...
不可逆变化：...
结尾类型：从新问题/代价落地/关系变化/阶段兑现/信息反差/危机逼近/情绪余震中选一项
章末钩子：...
下一章承接关键词：用/分隔2-4个实体或事件

请输出第{chap_num}章细纲："""

        try:
            outline = self.call_llm_non_stream(
                sys_prompt,
                user_prompt,
                temp=0.7,
                max_tokens=900,
                task_type="single_outline_generation",
            )
            outline = self.strip_markdown_artifacts(outline).strip()
            if outline:
                self._ui_progress_append(f"     自动展开完成 ({len(outline)}字)\n")
            return outline
        except Exception as e:
            if self._is_codex_fatal_transport_error(e):
                raise
            self._ui_progress_append(f"    [FAIL] 自动展开失败: {str(e)[:60]}\n")
            return ""

    def _rolling_outline_path(self):
        return os.path.join(generator.DIRS["plot"], "AI滚动逐章细纲.txt")

    def _auto_generate_outline_batch(self, start_chap, count, prev_content):
        """一次性生成一批逐章细纲，并落到滚动细纲文件中供后续章节复用。"""
        if not self._has_configured_model_provider():
            return ""

        plot_dir = generator.DIRS["plot"]
        end_chap = start_chap + count - 1
        volume_name = self._get_story_volume_name(start_chap) or ""
        volume_outline = self._extract_volume_outline_from_master(volume_name)
        if not volume_outline:
            volume_outline = generator.read_text_safe(os.path.join(plot_dir, "全书大纲.txt"))
        memo = generator.read_text_safe(os.path.join(plot_dir, "全局备忘录.txt"))
        fact_context = knowledge_manager.build_generation_context(plot_dir, start_chap, max_chars=2400)
        canon_context = continuity_guard.build_canon_context(plot_dir, max_chars=1800)
        story_context = story_architect.build_story_context(plot_dir, start_chap, max_chars=2600)
        ledger_context = ""
        if self.config.get("structured_state_enabled", True):
            ledger_context, _ = state_ledger.render_context(
                state_ledger.load_state(plot_dir),
                start_chap,
                "",
                prev_content,
                max_chars=4200,
                stale_warn_chapters=max(3, int(self.config.get("subplot_stale_warn_chapters", 8) or 8)),
            )
            if self.config.get("temporal_memory_enabled", True):
                memory_hits = temporal_memory.retrieve(
                    plot_dir,
                    f"{volume_outline}\n{prev_content}",
                    before_chapter=start_chap,
                    max_hits=20,
                    max_chars=2600,
                )
                memory_text = temporal_memory.render_retrieval(memory_hits, max_chars=2600)
                if memory_text:
                    ledger_context += "\n\n" + memory_text
        policy_context = publishing_rules.render_rules_document(
            self._build_effective_project_rules()
        )
        commercial_context = self._commercial_contract_context(max_chars=5200)
        volume_outline = self._filter_spoilers_from_text(volume_outline, start_chap)
        memo = self._filter_spoilers_from_text(memo, start_chap)
        fact_context = self._filter_spoilers_from_text(fact_context, start_chap)

        sys_prompt = (
            "你是长篇网文的卷纲与逐章细纲架构师。你的任务是为托管写作生成可直接执行的逐章细纲，"
            "必须控制真相节奏、人物状态和事件递进。只输出细纲，不要解释。"
        )
        user_prompt = f"""请为第 {start_chap} 章到第 {end_chap} 章生成连续逐章细纲。

【硬性格式】
第X章 标题：
章节功能：从冲突推进/调查发现/兑现回收/关系转折/代价余波/铺垫蓄力/高潮决断中选一项
核心事件：...
出场人物：...
承接锚点：必须承接的上一章具体人/物/危险
人物选择与代价：谁面临什么两难、采取什么行动、支付什么代价
反方行动：对手、制度、环境或利益方主动改变了什么
冲突/爽点：...
兑现类型：从能力/知识/关系/身份/资源/情绪/谜底/失败代价中选一项
伏笔/禁出内容：...
状态落点：境界=...；地点=...；伤势=...
不可逆变化：本章完成后禁止重演的一件事
结尾类型：从新问题/代价落地/关系变化/阶段兑现/信息反差/危机逼近/情绪余震中选一项
章末钩子：...
下一章承接关键词：用/分隔2-4个下一章必须出现的实体或事件

【生成要求】
1. 每章必须包含上述14个字段，不能省略。
2. 每章只规划1-2个核心事件，避免一章塞太多。
3. 不要提前揭示当前章节不该知道的真相；不确定时只写怀疑或误判。
4. 不要让已经完成的事件重复发生。
5. 标题简洁，不要和前文标题重复。
6. 正史状态优先级高于旧细纲和旧摘要；境界、地点、伤势不得回档。
7. 每章状态落点必须是本章结束时的确定状态；不能把猜测写成事实。
8. 每10章至少推进一次书名卖点，不能只换敌人重复追杀和升级。
9. 角色只能使用已经取得的知识、能力、权限、物品和资源；禁止凭空新增技能、机制、组织或证据，项目设定中的限制与代价必须保留。
10. 必须遵守项目题材简介、番茄发布规则与题材禁区；不得靠无意义重复、改名换皮或无关段落填充章节。
11. 必须执行商业立项中的读者承诺与最近阶段审稿行动单；黄金三章、每3/10/30章节奏不得只埋谜不兑现。
12. 细纲不得让角色违反同章已写明的职责、权限、伤势或动作限制；若限制发生变化，必须先规划明确的现场原因和交接动作。
13. 连续三章不得使用同一种章节功能、兑现类型或结尾类型；同一批至少轮换调查、冲突、兑现、关系和余波中的三类。
14. 章末只需形成明确的下一页期待，不必每章突发更大危机；过渡章允许用代价落地、关系变化、阶段兑现或情绪余震自然收束。

【卷/全书大纲参考】
{volume_outline[:2600] if volume_outline else "暂无"}

【全局备忘录】
{memo[:1200] if memo else "暂无"}

【关键事实库】
{fact_context if fact_context else "暂无"}

【正史硬约束】
{canon_context if canon_context else "暂无"}

【证据正史账本、角色知情边界、事件冷却与停滞支线】
{ledger_context if ledger_context else "暂无；禁止自行补全未知状态"}

【故事圣经与阶段契约】
{story_context if story_context else "暂无"}

【番茄发布与项目题材规则】
{policy_context[:4200] if policy_context else "暂无"}

【商业立项与最近审稿行动】
{commercial_context if commercial_context else "暂无"}

【上一章结尾】
{prev_content[-500:] if prev_content else "无"}

请输出第{start_chap}章到第{end_chap}章细纲："""

        try:
            last_summary = ""
            for attempt in range(2):
                prompt = user_prompt
                if last_summary:
                    prompt += (
                        "\n\n【上一版细纲未通过正史检查】\n"
                        + last_summary
                        + "\n请完整重写这一批细纲，不要只解释。"
                    )
                outline_text = self.call_llm_non_stream(sys_prompt, prompt, temp=0.5, max_tokens=4200)
                outline_text = (outline_text or "").strip()
                check = continuity_guard.validate_outline(
                    outline_text,
                    start_chap,
                    end_chap,
                    plot_dir,
                    require_extended=True,
                )
                if check["status"] == "PASS":
                    return outline_text
                last_summary = check["summary"] or "章节覆盖或正史状态不合格"
                self._ui_progress_append(f"    [WARN] 滚动细纲正史检查未通过：{last_summary}\n")
            self._append_batch_audit({
                "event": "rolling_outline_rejected",
                "start_chapter": start_chap,
                "end_chapter": end_chap,
                "reason": last_summary,
            })
            return ""
        except Exception as e:
            if self._is_codex_fatal_transport_error(e):
                raise
            self._ui_progress_append(f"    [FAIL] 滚动细纲生成失败: {str(e)[:80]}\n")
            return ""

    def _append_rolling_outline_batch(self, start_chap, end_chap, outline_text):
        outline_text = (outline_text or "").strip()
        if not outline_text:
            return ""
        path = self._rolling_outline_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        existing = generator.read_text_safe(path)
        batch_block = (
            f"===== 自动滚动细纲 第{start_chap}-{end_chap}章 · {stamp} =====\n"
            f"{outline_text}\n"
        )
        updated = (existing.rstrip() + "\n\n" if existing else "") + batch_block
        temp_path = path + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(updated)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)

        # Keep each consumed batch independently auditable even if the active
        # rolling window is later cleaned or rebuilt.
        archive_dir = os.path.join(generator.DIRS["hist"], "outline_archive")
        os.makedirs(archive_dir, exist_ok=True)
        archive_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = os.path.join(
            archive_dir,
            f"outline_{start_chap:04d}_{end_chap:04d}_{archive_stamp}.txt",
        )
        with open(archive_path, "w", encoding="utf-8") as f:
            f.write(batch_block)
        return path

    def _ensure_rolling_outline_batch(self, chap_num, prev_content):
        """自动补足当前窗口的逐章细纲。已有人工细纲优先，不覆盖。"""
        if not self.config.get("auto_outline_from_volume", False):
            return False
        planned_end = self._get_story_planned_end_chapter()
        if planned_end and chap_num > planned_end:
            return False

        batch_size = max(1, int(self.config.get("rolling_outline_batch_size", 10) or 10))
        lookahead = max(1, int(self.config.get("rolling_outline_trigger_lookahead", 3) or 3))
        window_end = chap_num + lookahead - 1
        if planned_end:
            window_end = min(window_end, planned_end)

        missing = []
        for c in range(chap_num, window_end + 1):
            if self._get_story_volume_name(c) and not self._extract_chapter_outline(c):
                missing.append(c)

        if not missing:
            return False

        start_chap = missing[0]
        volume_end = self._get_story_volume_end(start_chap)
        end_chap = min(
            start_chap + batch_size - 1,
            planned_end or (start_chap + batch_size - 1),
            volume_end or (start_chap + batch_size - 1),
        )
        end_chap = story_architect.clamp_outline_batch_end(
            generator.DIRS["plot"], start_chap, end_chap
        )
        count = end_chap - start_chap + 1
        self._ui_progress_append(f"   自动补第 {start_chap}-{end_chap} 章滚动细纲...\n")
        outline_text = self._auto_generate_outline_batch(start_chap, count, prev_content)
        if not outline_text:
            return False
        path = self._append_rolling_outline_batch(start_chap, end_chap, outline_text)
        self._append_batch_audit({
            "event": "rolling_outline_generated",
            "start_chapter": start_chap,
            "end_chapter": end_chap,
            "path": path,
        })
        self._ui_progress_append(f"    [OK] 已写入 {os.path.basename(path)}\n")
        return True

    def _regenerate_rejected_outline(self, chap_num, prev_content, reason):
        """Replace a rejected outline window instead of pausing full-book mode."""
        planned_end = self._get_story_planned_end_chapter()
        batch_size = max(1, int(self.config.get("rolling_outline_batch_size", 10) or 10))
        volume_end = self._get_story_volume_end(chap_num)
        bounded_end = min(planned_end or chap_num, volume_end or (planned_end or chap_num))
        bounded_end = story_architect.clamp_outline_batch_end(
            generator.DIRS["plot"], chap_num, bounded_end
        )
        count = min(batch_size, max(1, bounded_end - chap_num + 1))
        end_chap = chap_num + count - 1
        for attempt in range(2):
            outline_text = self._auto_generate_outline_batch(chap_num, count, prev_content)
            if not outline_text:
                continue
            sections = story_architect.split_outline_sections(outline_text)
            reveal_failures = []
            for current in range(chap_num, end_chap + 1):
                section = sections.get(current, "")
                guard = self._run_outline_reveal_guard(section, current)
                if guard["status"] != "PASS":
                    reveal_failures.append(f"第{current}章：{guard.get('summary', '')}")
            if reveal_failures:
                reason = "；".join(reveal_failures[:4])
                self._ui_progress_append(f"    [WARN] 重建细纲仍触发真相护栏：{reason[:180]}\n")
                continue
            self._append_rolling_outline_batch(chap_num, end_chap, outline_text)
            self._append_batch_audit({
                "event": "rolling_outline_auto_repaired",
                "start_chapter": chap_num,
                "end_chapter": end_chap,
                "attempt": attempt + 1,
                "previous_reason": reason[:500],
            })
            return sections.get(chap_num, "")
        return ""

    # ============================================================
    # LLM 调用 (含 max_tokens Fix + None guard)
    # ============================================================
    def _model_usage_summary_path(self):
        log_dir = generator.DIRS.get("logs", generator.DIRS.get("out", "logs"))
        return os.path.join(log_dir, "model_usage_summary.json")

    def _mark_model_usage_persistence_fault(self, error, *, unpersisted_cost=0.0):
        """Latch a project-scoped accounting fault until the project changes."""
        with self._model_call_lock:
            if not getattr(self, "_model_usage_persistence_fault", ""):
                self._model_usage_persistence_fault = str(error or "费用账本不可用")[:300]
            self._model_usage_unpersisted_cost_cny = round(
                max(0.0, getattr(self, "_model_usage_unpersisted_cost_cny", 0.0))
                + max(0.0, float(unpersisted_cost or 0.0)),
                8,
            )

    def _load_model_usage_summary(self):
        summary_path = self._model_usage_summary_path()
        try:
            with open(summary_path, "r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            # A genuinely new project has neither file.  A non-empty ledger
            # without its summary means an earlier call was recorded but its
            # aggregate was not safely committed; after restart this must not
            # be mistaken for zero spend.
            ledger_path = os.path.join(
                os.path.dirname(summary_path), "model_usage.jsonl"
            )
            try:
                ledger_size = os.path.getsize(ledger_path)
            except FileNotFoundError:
                ledger_size = 0
            except OSError as exc:
                self._mark_model_usage_persistence_fault(exc)
                raise RuntimeError("本书费用明细存在但无法核验") from exc
            if ledger_size > 0:
                exc = RuntimeError("费用明细非空但费用汇总缺失")
                self._mark_model_usage_persistence_fault(exc)
                raise RuntimeError("本书费用明细非空但费用汇总缺失") from exc
            return {}
        except Exception as exc:
            self._mark_model_usage_persistence_fault(exc)
            raise RuntimeError("本书费用汇总损坏或不可读") from exc
        if not isinstance(payload, dict):
            exc = ValueError("费用汇总根节点不是对象")
            self._mark_model_usage_persistence_fault(exc)
            raise RuntimeError("本书费用汇总损坏或不可读") from exc
        try:
            estimated_cost = float(payload.get("estimated_cost_cny") or 0.0)
        except (TypeError, ValueError) as exc:
            self._mark_model_usage_persistence_fault(exc)
            raise RuntimeError("本书费用汇总损坏或不可读") from exc
        if estimated_cost < 0 or not math.isfinite(estimated_cost):
            exc = ValueError("费用汇总中的估算费用无效")
            self._mark_model_usage_persistence_fault(exc)
            raise RuntimeError("本书费用汇总损坏或不可读") from exc
        ledger_path = os.path.join(
            os.path.dirname(summary_path), "model_usage.jsonl"
        )
        try:
            ledger_size = os.path.getsize(ledger_path)
        except FileNotFoundError:
            ledger_size = 0
        except OSError as exc:
            self._mark_model_usage_persistence_fault(exc)
            raise RuntimeError("本书费用明细存在但无法核验") from exc
        if ledger_size > 0:
            try:
                ledger_calls = 0
                with open(ledger_path, "r", encoding="utf-8-sig") as handle:
                    for line_number, line in enumerate(handle, 1):
                        if not line.strip():
                            continue
                        record = json.loads(line)
                        if not isinstance(record, dict):
                            raise ValueError(
                                f"费用明细第{line_number}条不是对象"
                            )
                        ledger_calls += 1
                summary_calls = int(payload.get("calls_with_usage") or 0)
            except Exception as exc:
                self._mark_model_usage_persistence_fault(exc)
                raise RuntimeError("本书费用明细损坏或不可读") from exc
            if ledger_calls != summary_calls:
                exc = RuntimeError(
                    f"费用明细记录数{ledger_calls}与汇总调用数{summary_calls}不一致"
                )
                self._mark_model_usage_persistence_fault(exc)
                raise RuntimeError("本书费用明细与汇总不一致") from exc
        return payload

    def _configured_cost_limit_cny(self):
        try:
            return max(0.0, float(self.config.get("max_estimated_cost_cny") or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _recovered_external_cost_cny(self):
        """Return a hash-bound carry-forward cost or fail closed.

        Recovered/external model work is not present in the normal DeepSeek
        usage ledger.  A project may carry that spend forward only when the
        configured amount is backed by an immutable in-project evidence file
        whose hash and recorded amount both match.
        """
        try:
            amount = float(self.config.get("recovered_external_cost_cny") or 0.0)
        except (TypeError, ValueError) as exc:
            self._mark_model_usage_persistence_fault(exc)
            raise RuntimeError("恢复费用基线无效") from exc
        if amount < 0 or not math.isfinite(amount):
            exc = ValueError("恢复费用基线必须是有限的非负数")
            self._mark_model_usage_persistence_fault(exc)
            raise RuntimeError("恢复费用基线无效") from exc
        if amount == 0:
            return 0.0

        relative = str(
            self.config.get("recovered_external_cost_evidence") or ""
        ).strip()
        expected_hash = str(
            self.config.get("recovered_external_cost_evidence_sha256") or ""
        ).strip().lower()
        if not relative or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
            exc = ValueError("恢复费用缺少证据路径或SHA-256")
            self._mark_model_usage_persistence_fault(exc)
            raise RuntimeError("恢复费用基线缺少可核验证据") from exc

        project_root = os.path.realpath(str(getattr(self, "project_dir", "") or ""))
        evidence_path = os.path.realpath(os.path.join(project_root, relative))
        try:
            inside = os.path.commonpath([project_root, evidence_path]) == project_root
        except (ValueError, OSError):
            inside = False
        if not project_root or not inside or not os.path.isfile(evidence_path):
            exc = ValueError("恢复费用证据不在当前项目内或文件不存在")
            self._mark_model_usage_persistence_fault(exc)
            raise RuntimeError("恢复费用基线证据不可用") from exc

        try:
            with open(evidence_path, "rb") as handle:
                actual_hash = hashlib.sha256(handle.read()).hexdigest()
            with open(evidence_path, "r", encoding="utf-8-sig") as handle:
                evidence_text = handle.read()
        except OSError as exc:
            self._mark_model_usage_persistence_fault(exc)
            raise RuntimeError("恢复费用基线证据不可读") from exc
        if actual_hash != expected_hash:
            exc = ValueError("恢复费用证据SHA-256不匹配")
            self._mark_model_usage_persistence_fault(exc)
            raise RuntimeError("恢复费用基线证据已变化") from exc

        recorded = [
            float(value)
            for value in re.findall(r"(?:¥|￥)\s*(\d+(?:\.\d+)?)", evidence_text)
        ]
        if not any(abs(value - amount) <= 0.00000001 for value in recorded):
            exc = ValueError("恢复费用证据未记录配置金额")
            self._mark_model_usage_persistence_fault(exc)
            raise RuntimeError("恢复费用基线金额与证据不一致") from exc
        return amount

    def _project_estimated_cost_cny(self):
        baseline = 0.0
        try:
            persisted = max(
                0.0,
                float(self._load_model_usage_summary().get("estimated_cost_cny") or 0.0),
            )
            baseline = self._recovered_external_cost_cny()
        except (RuntimeError, TypeError, ValueError):
            persisted = 0.0
        with self._model_call_lock:
            unpersisted = max(
                0.0,
                float(getattr(self, "_model_usage_unpersisted_cost_cny", 0.0) or 0.0),
            )
        # Successful calls are already in the on-disk summary and must not be
        # added from the session total a second time.  Only failed persistence
        # attempts remain in this project-scoped pending amount.
        return baseline + persisted + unpersisted

    def _estimate_model_call_reserve_cny(
        self, model, system_prompt, user_prompt, max_tokens
    ):
        if self.get_model_provider() == PROVIDER_CODEX_SOL:
            # ChatGPT/Codex-plan billing has no locally verifiable per-token
            # price.  Calls and tokens are still counted by the hard budgets.
            return 0.0
        prompt_tokens = self._estimate_token_count(system_prompt) + self._estimate_token_count(
            user_prompt
        )
        completion_tokens = max(0, int(max_tokens or 0))
        usage = {
            "prompt_tokens": prompt_tokens,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "reasoning_tokens": 0,
            "total_tokens": prompt_tokens + completion_tokens,
        }
        reserve = float(deepseek_runtime.estimate_cost_cny(str(model or ""), usage) or 0.0)
        if (prompt_tokens or completion_tokens) and reserve <= 0:
            raise RuntimeError("当前模型缺少费用估算规则，已停止发起调用")
        return reserve

    @staticmethod
    def _normalize_codex_usage(usage):
        if isinstance(usage, dict):
            source = dict(usage)
        elif usage is None:
            source = {}
        else:
            source = {
                key: getattr(usage, key, 0)
                for key in (
                    "prompt_tokens", "prompt_cache_hit_tokens",
                    "prompt_cache_miss_tokens", "completion_tokens",
                    "reasoning_tokens", "total_tokens",
                )
            }
        prompt = int(source.get("prompt_tokens") or source.get("input_tokens") or 0)
        completion = int(
            source.get("completion_tokens") or source.get("output_tokens") or 0
        )
        reasoning = int(source.get("reasoning_tokens") or 0)
        cache_hit = int(source.get("prompt_cache_hit_tokens") or 0)
        cache_miss = int(
            source.get("prompt_cache_miss_tokens")
            or max(0, prompt - cache_hit)
        )
        total = int(source.get("total_tokens") or (prompt + completion))
        return {
            "prompt_tokens": max(0, prompt),
            "prompt_cache_hit_tokens": max(0, cache_hit),
            "prompt_cache_miss_tokens": max(0, cache_miss),
            "completion_tokens": max(0, completion),
            "reasoning_tokens": max(0, reasoning),
            "total_tokens": max(0, total),
        }

    def _record_model_usage(
        self, usage, model, thinking, task_type, *, provider=None, evidence=None
    ):
        provider = normalize_model_provider(
            provider or self.get_model_provider(),
            missing_defaults_to_deepseek=False,
        )
        if provider == PROVIDER_CODEX_SOL:
            normalized = self._normalize_codex_usage(usage)
            estimated_cost = 0.0
            pricing_note = (
                "Codex 套餐调用；无本地可核验 token 单价，费用估算记为 0"
            )
        else:
            normalized = deepseek_runtime.normalize_usage(usage)
            estimated_cost = deepseek_runtime.estimate_cost_cny(model, normalized)
            pricing_note = "按工具内置单价估算；最终扣费以 DeepSeek 账户账单为准"
        if not normalized.get("total_tokens") and provider != PROVIDER_CODEX_SOL:
            return
        with self._model_call_lock:
            self._usage_prompt_tokens = getattr(self, "_usage_prompt_tokens", 0) + normalized["prompt_tokens"]
            self._usage_completion_tokens = getattr(self, "_usage_completion_tokens", 0) + normalized["completion_tokens"]
            self._usage_reasoning_tokens = getattr(self, "_usage_reasoning_tokens", 0) + normalized["reasoning_tokens"]
            self._usage_estimated_cost_cny = (
                getattr(self, "_usage_estimated_cost_cny", 0.0) + estimated_cost
            )
        if (
            self.config.get("model_usage_ledger_enabled", True)
            or self._configured_cost_limit_cny() > 0
        ):
            payload = {
                "time": datetime.now().isoformat(timespec="seconds"),
                "job_id": getattr(self, "_current_batch_job_id", ""),
                "chapter": int(getattr(self, "_chapter_model_call_number", 0) or 0),
                "task_type": task_type or "unknown",
                "provider": provider,
                "model": model,
                "thinking": bool(thinking),
                **normalized,
                "estimated_cost_cny": round(estimated_cost, 8),
                "pricing_note": pricing_note,
            }
            if isinstance(evidence, dict):
                for key in (
                    "evidence_path", "evidence_sha256", "provenance_basis"
                ):
                    value = str(evidence.get(key) or "").strip()
                    if value:
                        payload[key] = value
            try:
                log_dir = generator.DIRS.get("logs", generator.DIRS.get("out", "logs"))
                os.makedirs(log_dir, exist_ok=True)
                summary_path = self._model_usage_summary_path()
                # Read the prior aggregate before appending this call.  Once
                # the ledger append succeeds, a missing summary intentionally
                # represents an incomplete prior commit and must fail closed.
                summary = self._load_model_usage_summary()
                ledger_path = os.path.join(log_dir, "model_usage.jsonl")
                with open(ledger_path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                for key in (
                    "prompt_tokens", "prompt_cache_hit_tokens",
                    "prompt_cache_miss_tokens", "completion_tokens",
                    "reasoning_tokens", "total_tokens",
                ):
                    summary[key] = int(summary.get(key) or 0) + int(normalized.get(key) or 0)
                summary["calls_with_usage"] = int(summary.get("calls_with_usage") or 0) + 1
                provider_calls = summary.get("provider_calls")
                if not isinstance(provider_calls, dict):
                    provider_calls = {}
                provider_calls[provider] = int(provider_calls.get(provider) or 0) + 1
                summary["provider_calls"] = provider_calls
                summary["estimated_cost_cny"] = round(
                    float(summary.get("estimated_cost_cny") or 0.0) + estimated_cost, 8
                )
                summary["updated_at"] = payload["time"]
                summary["pricing_note"] = payload["pricing_note"]
                _atomic_write_text(
                    summary_path, json.dumps(summary, ensure_ascii=False, indent=2)
                )
            except Exception as exc:
                self._mark_model_usage_persistence_fault(
                    exc, unpersisted_cost=estimated_cost
                )
                if hasattr(self, "_append_batch_audit"):
                    self._append_batch_audit({
                        "event": "model_usage_ledger_failed",
                        "error": str(exc)[:300],
                    })
        self._ui(self._update_model_usage_label)

    def _update_model_usage_label(self):
        if not hasattr(self, "usage_lbl"):
            return
        with self._model_call_lock:
            count = self._model_call_count
            limit = self._model_call_limit
            chapter = getattr(self, "_chapter_model_call_number", 0)
            chapter_count = getattr(self, "_chapter_model_call_count", 0)
            chapter_limit = getattr(self, "_chapter_model_call_limit", 0)
            tokens = (
                getattr(self, "_usage_prompt_tokens", 0)
                + getattr(self, "_usage_completion_tokens", 0)
            )
            cost = getattr(self, "_usage_estimated_cost_cny", 0.0)
        project_cost = self._project_estimated_cost_cny()
        cost_limit = self._configured_cost_limit_cny()
        text = f"本次模型调用：{count}/{limit}" if limit else f"本次模型调用：{count}"
        if chapter and chapter_limit:
            text += f" · 第{chapter}章 {chapter_count}/{chapter_limit}"
        if tokens:
            text += f" · {tokens:,} tokens"
        if self.get_model_provider() == PROVIDER_CODEX_SOL:
            text += " · Codex套餐调用/本地估算¥0"
        elif tokens:
            text += f" · 估算¥{cost:.4f}"
        if cost_limit > 0 and self.get_model_provider() == PROVIDER_DEEPSEEK:
            text += f" · 本书¥{project_cost:.2f}/¥{cost_limit:.2f}"
        self.usage_lbl.config(text=text)

    def _before_model_call(self, reserved_cost_cny=0.0):
        """Enforce both whole-job and real per-chapter model-call budgets."""
        cost_limit = self._configured_cost_limit_cny()
        provider = self.get_model_provider()
        if (
            self.config.get("model_usage_ledger_enabled", True)
            or cost_limit > 0
        ):
            # Reading the aggregate is also the integrity check: corrupt or
            # mismatched ledger files latch a fault even when Codex pricing is
            # locally zero.
            self._project_estimated_cost_cny()
            with self._model_call_lock:
                persistence_fault = getattr(
                    self, "_model_usage_persistence_fault", ""
                )
            if persistence_fault:
                raise RuntimeError(
                    "本书费用账本损坏、不可读或写入失败，模型累计调用无法安全核验；"
                    "已停止发起新的模型调用。请修复账本或切换项目后再继续"
                )
        if cost_limit > 0 and provider == PROVIDER_DEEPSEEK:
            spent = self._project_estimated_cost_cny()
            try:
                reserved = max(0.0, float(reserved_cost_cny or 0.0))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("本次模型调用费用预留无效") from exc
            if spent >= cost_limit:
                raise RuntimeError(
                    f"本书累计估算 API 费用 ¥{spent:.2f} 已达到止损 ¥{cost_limit:.2f}；"
                    "已停止发起新的模型调用。可在“修改本书”中提高止损后继续"
                )
            if reserved and spent + reserved > cost_limit:
                raise RuntimeError(
                    f"本书当前估算费用 ¥{spent:.2f}，本次调用最坏预留 ¥{reserved:.4f}，"
                    f"可能超过止损 ¥{cost_limit:.2f}；已在调用前停止"
                )
        with self._model_call_lock:
            if self._model_call_limit and self._model_call_count >= self._model_call_limit:
                raise RuntimeError(
                    f"本次模型调用已达到硬上限 {self._model_call_limit} 次，已停止继续调用"
                )
            chapter_limit = getattr(self, "_chapter_model_call_limit", 0)
            chapter_count = getattr(self, "_chapter_model_call_count", 0)
            chapter_number = getattr(self, "_chapter_model_call_number", 0)
            ledger_call = bool(getattr(self, "_state_ledger_call_active", False))
            commercial_call = bool(
                getattr(self, "_commercial_review_call_active", False)
            )
            commercial_remaining = int(
                getattr(self, "_commercial_review_stage_remaining", 0) or 0
            )
            commercial_fallback = bool(
                getattr(self, "_commercial_review_fallback_call_active", False)
            )
            commercial_fallback_eligible = int(
                getattr(self, "_commercial_review_fallback_eligible", 0) or 0
            )
            if commercial_call:
                if commercial_fallback and commercial_fallback_eligible <= 0:
                    raise RuntimeError("商业硬闸门阶段没有可用的思考模式回退额度")
                if not commercial_fallback and commercial_remaining <= 0:
                    raise RuntimeError("商业硬闸门阶段调用已达到本次分块预算")
            ledger_reserve = 0
            if (
                chapter_limit
                and self.config.get("structured_state_enabled", True)
                and not ledger_call
                and not commercial_call
            ):
                attempts = max(1, int(self.config.get("state_delta_max_attempts", 2) or 2))
                audit_enabled = bool(
                    self.config.get("state_delta_independent_audit", True)
                    or self.config.get("pre_save_continuity_audit_enabled", True)
                )
                calls_per_attempt = 2 if audit_enabled else 1
                ledger_reserve = min(max(0, chapter_limit - 4), attempts * calls_per_attempt)
            effective_limit = chapter_limit if ledger_call else chapter_limit - ledger_reserve
            if chapter_limit and not commercial_call and chapter_count >= effective_limit:
                if ledger_reserve:
                    raise RuntimeError(
                        f"第 {chapter_number} 章普通调用已达到 {effective_limit} 次；"
                        f"单章硬上限 {chapter_limit} 次中已为正史证据记账预留 {ledger_reserve} 次，"
                        "已停止普通调用，避免正文保存后没有额度更新长期记忆"
                    )
                raise RuntimeError(
                    f"第 {chapter_number} 章模型调用已达到单章硬上限 {chapter_limit} 次，"
                    "已停止继续调用"
                )
            self._model_call_count += 1
            if chapter_limit:
                self._chapter_model_call_count = chapter_count + 1
            if commercial_call:
                if commercial_fallback:
                    self._commercial_review_fallback_eligible = (
                        commercial_fallback_eligible - 1
                    )
                else:
                    self._commercial_review_stage_remaining = commercial_remaining - 1
                    # DeepSeek may spend the whole thinking allowance without
                    # returning structured content.  One physical no-thinking
                    # retry is allowed for each already-authorized logical map
                    # or reduce call, but it cannot create another logical call.
                    self._commercial_review_fallback_eligible = (
                        commercial_fallback_eligible + 1
                    )
        self._ui(self._update_model_usage_label)

    def _begin_chapter_model_budget(self, chap_num):
        per_chapter = max(
            4,
            min(20, int(self.config.get("max_model_calls_per_chapter", 16) or 16)),
        )
        with self._model_call_lock:
            self._chapter_model_call_number = int(chap_num)
            self._chapter_model_call_count = 0
            self._chapter_model_call_limit = per_chapter
        self._ui(self._update_model_usage_label)

    def disable_buttons(self):
        self._set_busy_widgets(True)
        self.btn_save_new.config(state=tk.DISABLED)
        self.btn_save_append.config(state=tk.DISABLED)

    def enable_buttons(self, is_new=True):
        if self._is_busy():
            return
        self._set_busy_widgets(False)
        if is_new:
            self.btn_save_new.config(state=tk.NORMAL)
        else:
            self.btn_save_append.config(state=tk.NORMAL)

    @staticmethod
    def _codex_exception_class_names(exc):
        return {cls.__name__ for cls in type(exc).__mro__}

    @classmethod
    def _is_codex_timeout_error(cls, exc):
        return "CodexSolTimeoutError" in cls._codex_exception_class_names(exc)

    @classmethod
    def _is_codex_fatal_transport_error(cls, exc):
        names = cls._codex_exception_class_names(exc)
        if "CodexSolTimeoutError" in names:
            return False
        return bool(names.intersection({
            "CodexSolExecutableError",
            "CodexSolAccessError",
            "CodexSolProtocolError",
            "CodexSolEvidenceError",
            "CodexSolRuntimeError",
        }))

    def _call_codex_text(
        self,
        system_prompt,
        user_prompt,
        *,
        token_limit,
        task_type,
    ):
        if codex_sol_runtime is None:
            raise ModelProviderConfigError("Codex GPT-5.6-sol 运行层缺失")
        executable = self._resolve_codex_executable()
        safe_task = re.sub(
            r"[^a-zA-Z0-9_.-]+", "_", str(task_type or "generation")
        ).strip("_.-") or "generation"
        safe_task = safe_task[:64]
        result = codex_sol_runtime.call_text(
            system_prompt,
            user_prompt,
            executable=executable,
            evidence_dir=self._codex_evidence_dir(safe_task),
            timeout_seconds=max(
                30, int(self.config.get("codex_timeout_seconds", 900) or 900)
            ),
            max_output_chars=max(512, int(token_limit or 8192) * 4),
            task_type=safe_task,
        )
        if not isinstance(result, dict):
            raise codex_sol_runtime.CodexSolRuntimeError(
                "Codex 运行层未返回结构化结果"
            )
        if str(result.get("provider") or "") != str(
            getattr(codex_sol_runtime, "PROVIDER", "openai_codex_cli")
        ):
            raise codex_sol_runtime.CodexSolRuntimeError(
                "Codex 调用提供方证据不匹配"
            )
        if str(result.get("model") or "") != CODEX_SOL_MODEL:
            raise codex_sol_runtime.CodexSolRuntimeError(
                "Codex 调用模型证据不是 exact gpt-5.6-sol"
            )
        content = str(result.get("text") or "")
        if not content.strip():
            raise codex_sol_runtime.CodexSolRuntimeError("Codex 未返回可用正文")
        self._record_model_usage(
            result.get("usage"),
            CODEX_SOL_MODEL,
            False,
            safe_task,
            provider=PROVIDER_CODEX_SOL,
            evidence=result,
        )
        return content

    def _call_generation_text(
        self,
        system_prompt,
        user_prompt,
        *,
        model_name,
        token_limit,
        task_type,
        on_chunk=None,
    ):
        """Single provider-neutral entry for manual and batch chapter prose."""
        provider = self.get_model_provider()
        if provider == PROVIDER_CODEX_SOL:
            content = self._call_codex_text(
                system_prompt,
                user_prompt,
                token_limit=token_limit,
                task_type=task_type,
            )
            # Codex is presented only after the complete response has passed
            # runtime evidence verification.  A stop request discards it.
            if not self._stop_event.is_set() and on_chunk is not None:
                on_chunk(content)
            return content

        client = self.get_client()
        if client is None:
            raise RuntimeError("未配置 DeepSeek API Key")
        stream = client.chat.completions.create(**self._deepseek_request_kwargs(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=token_limit,
            temperature=self.config.get("temperature", 0.8),
            review=False,
            stream=True,
        ))
        content = ""
        stream_usage = None
        for chunk in stream:
            if getattr(chunk, "usage", None):
                stream_usage = chunk.usage
            if self._stop_event.is_set():
                break
            if chunk.choices and chunk.choices[0].delta.content:
                text_chunk = chunk.choices[0].delta.content
                content += text_chunk
                if on_chunk is not None:
                    on_chunk(text_chunk)
        self._record_model_usage(
            stream_usage,
            model_name,
            bool(self.config.get("deepseek_generation_thinking", False)),
            task_type,
            provider=PROVIDER_DEEPSEEK,
        )
        return content

    def stream_call_llm(self, system_prompt, final_user_prompt, is_new=True):
        self.is_generating = True
        self._ui_clear(" 正在燃烧算力生成中，请稍候...\n\n")

        try:
            system_prompt, final_user_prompt, prompt_stats = self._apply_generation_prompt_budget(
                system_prompt, final_user_prompt
            )
            provider = self.get_model_provider()
            model_name = self.get_model_name()
            token_limit = int(self.config.get("max_tokens", 8192) or 8192)
            reserve = self._estimate_model_call_reserve_cny(
                model_name, system_prompt, final_user_prompt, token_limit
            )
            self._before_model_call(reserved_cost_cny=reserve)
            self._ui_clear()
            self.generated_content = self._call_generation_text(
                system_prompt,
                final_user_prompt,
                model_name=model_name,
                token_limit=token_limit,
                task_type="manual_chapter_generation",
                on_chunk=self._ui_append,
            )
            if self._stop_event.is_set():
                self.generated_content = ""
                stop_note = (
                    "Codex 完整返回已整份丢弃"
                    if provider == PROVIDER_CODEX_SOL
                    else "DeepSeek 未完成流式返回已丢弃"
                )
                self._ui_clear(f"[PAUSE] 已停止；{stop_note}，未进入正文。\n")
                return
            cleaned = self.strip_markdown_artifacts(self.generated_content)
            if cleaned != self.generated_content:
                self.generated_content = cleaned
                self._ui_clear(cleaned)
        except Exception as e:
            err_msg = str(e)
            if "maximum context length" in err_msg.lower():
                detail = ""
                try:
                    detail = (
                        f"\n\n输入估算：{prompt_stats['final']['input_tokens']} tokens"
                        f"\nSystem: {prompt_stats['final']['system_chars']} chars"
                        f"\nUser: {prompt_stats['final']['user_chars']} chars"
                    )
                except Exception:
                    pass
                self._ui(lambda: messagebox.showerror("生成失败", f"上下文超限。{detail}\n\n{err_msg}"))
            else:
                self._ui(lambda: messagebox.showerror("生成失败", f"API调用错误: {err_msg}"))
        finally:
            self.is_generating = False
            self._finish_project_task()
            self._ui(lambda: self.enable_buttons(is_new))
            self._ui(self.update_word_count)

    def start_generation_thread(self, system_prompt, final_user_prompt, is_new):
        if not self._begin_project_task("写新章" if is_new else "续写章节"):
            return
        self.disable_buttons()
        thread = threading.Thread(target=self.stream_call_llm, args=(system_prompt, final_user_prompt, is_new))
        thread.daemon = True
        thread.start()

    def call_llm_non_stream(
        self,
        system_prompt,
        user_prompt,
        temp=0.3,
        max_tokens=None,
        model_name=None,
        review=False,
        task_type="generation",
    ):
        provider = self.get_model_provider()
        token_limit = int(self.config.get("max_tokens", 8192) or 8192)
        if max_tokens is not None:
            requested_limit = int(max_tokens)
            token_limit = min(token_limit, requested_limit)
            if (
                review
                and getattr(self, "_commercial_review_call_active", False)
                and self.config.get("deepseek_review_thinking", True)
            ):
                thinking_min = max(
                    1,
                    int(
                        self.config.get(
                            "deepseek_review_thinking_min_tokens", 6000
                        )
                        or 6000
                    ),
                )
                # A hard-gate review needs room for hidden reasoning plus the
                # same 2600-token structured verdict allowance.  This review-
                # specific floor may exceed the normal generation cap.
                token_limit = max(token_limit, thinking_min + 2600)
        selected_model = (
            CODEX_SOL_MODEL
            if provider == PROVIDER_CODEX_SOL
            else (model_name or self.get_model_name())
        )
        reserve = self._estimate_model_call_reserve_cny(
            selected_model, system_prompt, user_prompt, token_limit
        )
        self._before_model_call(reserved_cost_cny=reserve)
        if provider == PROVIDER_CODEX_SOL:
            content = self._call_codex_text(
                system_prompt,
                user_prompt,
                token_limit=token_limit,
                task_type=task_type,
            )
            return self.strip_markdown_artifacts(content)

        client = self.get_client()
        if client is None:
            raise RuntimeError("未配置 DeepSeek API Key")
        request_kwargs = self._deepseek_request_kwargs(
            model=selected_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=token_limit,
            temperature=temp,
            review=review,
            stream=False,
        )
        response = client.chat.completions.create(**request_kwargs)
        actual_thinking = (
            request_kwargs.get("extra_body", {})
            .get("thinking", {})
            .get("type") == "enabled"
        )
        self._record_model_usage(
            getattr(response, "usage", None),
            selected_model,
            actual_thinking,
            task_type,
            provider=PROVIDER_DEEPSEEK,
        )

        def _response_content(payload):
            choices = getattr(payload, "choices", None) or []
            if not choices:
                raise RuntimeError("模型未返回候选内容")
            message = getattr(choices[0], "message", None)
            content = getattr(message, "content", None) if message is not None else None
            if not str(content or "").strip():
                raise RuntimeError("模型仅返回推理过程，未返回可解析正文")
            return str(content)

        try:
            content = _response_content(response)
        except RuntimeError as exc:
            if not (review and actual_thinking):
                raise
            if hasattr(self, "_append_batch_audit"):
                self._append_batch_audit({
                    "event": "review_thinking_empty_fallback",
                    "model": selected_model,
                    "error": str(exc)[:300],
                })
            commercial_fallback = bool(
                getattr(self, "_commercial_review_call_active", False)
            )
            if commercial_fallback:
                self._commercial_review_fallback_call_active = True
            try:
                self._before_model_call()
            finally:
                if commercial_fallback:
                    self._commercial_review_fallback_call_active = False
            fallback_kwargs = self._deepseek_request_kwargs(
                model=selected_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=token_limit,
                temperature=temp,
                review=review,
                stream=False,
                thinking_override=False,
            )
            response = client.chat.completions.create(**fallback_kwargs)
            self._record_model_usage(
                getattr(response, "usage", None),
                selected_model,
                False,
                f"{task_type}_thinking_fallback",
                provider=PROVIDER_DEEPSEEK,
            )
            content = _response_content(response)
        return self.strip_markdown_artifacts(content)

    def call_llm_review(self, system_prompt, user_prompt, temp=0.15, max_tokens=1200):
        """用于质检/审核的 LLM 调用"""
        return self.call_llm_non_stream(
            system_prompt,
            user_prompt,
            temp=temp,
            max_tokens=max_tokens,
            model_name=self.get_review_model_name(),
            review=True,
            task_type="review_or_audit",
        )

    def _state_ledger_source_paths(self):
        plot_dir = generator.DIRS["plot"]
        names = {
            "commercial_blueprint": "commercial_blueprint.json",
            "story_bible": "story_bible.json",
            "canon_state": continuity_guard.CANON_FILENAME,
            "tool_rules": "tool_rules.json",
            "reveal_rules": "reveal_rules.json",
            "master_outline": "全书大纲.txt",
            "current_volume_outline": "当前卷大纲.txt",
            "timeline_anchors": "时间线锚点.txt",
            "foreshadow_tracker": "伏笔与因果追踪表.txt",
        }
        return {label: os.path.join(plot_dir, name) for label, name in names.items()}

    def _compile_structured_chapter_context(self, chap_num, chapter_outline, prev_content):
        if not self.config.get("structured_state_enabled", True):
            return {"context_text": "", "selection": {}}
        cooldowns = None if self.config.get("event_cooldown_enabled", True) else {
            key: 0 for key in state_ledger.DEFAULT_EVENT_COOLDOWNS
        }
        temporal_hits = []
        temporal_context = ""
        if self.config.get("temporal_memory_enabled", True):
            temporal_hits = temporal_memory.retrieve(
                generator.DIRS["plot"],
                f"{chapter_outline or ''}\n{prev_content or ''}",
                before_chapter=chap_num,
                max_hits=max(4, int(self.config.get("temporal_memory_max_hits", 24) or 24)),
                max_chars=max(800, int(self.config.get("temporal_memory_max_chars", 2800) or 2800)),
            )
            temporal_context = temporal_memory.render_retrieval(
                temporal_hits,
                max_chars=max(800, int(self.config.get("temporal_memory_max_chars", 2800) or 2800)),
            )
        manifest = state_ledger.compile_chapter_context(
            generator.DIRS["plot"],
            chap_num,
            chapter_outline or "",
            prev_content or "",
            source_paths=self._state_ledger_source_paths(),
            max_chars=max(2400, int(self.config.get("state_context_max_chars", 5200) or 5200)),
            stale_warn_chapters=max(3, int(self.config.get("subplot_stale_warn_chapters", 8) or 8)),
            cooldowns=cooldowns,
            write_trace=bool(self.config.get("context_trace_enabled", True)),
            temporal_context=temporal_context,
            temporal_retrieval=temporal_hits,
        )
        self._append_batch_audit({
            "event": "chapter_context_compiled",
            "chapter": chap_num,
            "state_chapter": manifest.get("state_chapter"),
            "trace_path": manifest.get("trace_path", ""),
            "selection": manifest.get("selection", {}),
            "temporal_hits": len(temporal_hits),
            "source_hashes": manifest.get("source_hashes", {}),
        })
        return manifest

    def _run_semantic_consistency_guard(self, chapter_content, chap_num):
        """Run project invariants against every official chapter plus candidate."""
        rows = []
        for number, path in sorted(self._official_chapter_paths_by_number().items()):
            if int(number) == int(chap_num):
                continue
            text = generator.read_text_safe(path)
            if text.strip():
                rows.append((int(number), text))
        rows.append((int(chap_num), chapter_content or ""))
        rows.sort(key=lambda item: item[0])
        project_root = self.project_dir or os.path.dirname(generator.DIRS["plot"])
        report = semantic_consistency_guard.scan_chapters(project_root, rows)
        self._append_batch_audit({
            "event": "semantic_consistency_guard",
            "chapter": int(chap_num),
            "status": report.get("status"),
            "summary": report.get("summary", {}),
            "issues": (report.get("issues") or [])[:12],
        })
        return report

    @staticmethod
    def _semantic_guard_message(report, limit=8):
        rendered = []
        for issue in (report or {}).get("issues") or []:
            evidence = (issue.get("evidence") or [{}])[0]
            location = ""
            if evidence.get("chapter") not in (None, ""):
                location += f"第{evidence.get('chapter')}章"
            if evidence.get("line"):
                location += f"第{evidence.get('line')}行"
            prefix = f"{location}：" if location else ""
            rendered.append(prefix + str(issue.get("message") or issue.get("code") or "未知问题"))
            if len(rendered) >= int(limit):
                break
        return "；".join(rendered) or "语义守卫不可用或配置无效"

    def _run_narrative_quality_guard(
        self,
        chap_num,
        chapter_content,
        chapter_outline="",
    ):
        """Require an independently evidenced PASS before official save."""
        if not self.config.get("narrative_guard_enabled", True):
            raise narrative_guard.NarrativeGuardError(
                "逐章主线与可读性硬审已关闭，拒绝无人值守保存"
            )
        local_issues = narrative_guard.deterministic_issues(chapter_content)
        if local_issues:
            raise narrative_guard.NarrativeGuardError("；".join(local_issues[:8]))

        plot_dir = generator.DIRS["plot"]
        system_prompt, user_prompt, requirements = narrative_guard.build_audit_prompts(
            chapter_number=chap_num,
            chapter_text=chapter_content,
            chapter_outline=chapter_outline,
            book_contract=self._commercial_contract_context(max_chars=7000),
            story_context=story_architect.build_story_context(
                plot_dir, chap_num, max_chars=4500
            ),
            canon_context=continuity_guard.build_canon_context(
                plot_dir, max_chars=4500
            ),
            recent_context=knowledge_manager.build_generation_context(
                plot_dir, chap_num, max_chars=2600, include_sources=False
            ),
        )
        if not requirements:
            raise narrative_guard.NarrativeGuardError(
                "本章细纲没有可逐项核验的核心事件、状态落点或章末钩子，"
                "拒绝在空合同上审核正文"
            )
        trigger_markers = self.config.get("narrative_trigger_markers") or []
        if isinstance(trigger_markers, str):
            trigger_markers = [trigger_markers]
        trigger_positions = []
        total_chinese = len(re.findall(r"[\u4e00-\u9fff]", chapter_content or ""))
        for marker in trigger_markers:
            marker = str(marker or "").strip()
            marker_pos = (chapter_content or "").find(marker)
            if marker and marker_pos >= 0 and total_chinese:
                before = len(re.findall(
                    r"[\u4e00-\u9fff]", (chapter_content or "")[:marker_pos]
                ))
                trigger_positions.append({
                    "marker": marker,
                    "before_chinese": before,
                    "total_chinese": total_chinese,
                    "percent": round(before * 100.0 / total_chinese, 1),
                })
        if trigger_positions:
            user_prompt += (
                "\n\n【程序机械定位（权威数据，不得自行目测重算）】\n"
                + json.dumps(trigger_positions, ensure_ascii=False)
                + "\n若细纲要求关键触发点位于某百分比之前，必须以上述中文字符计数裁决。"
            )
        review_user_prompt = user_prompt
        raw = self.call_llm_review(
            system_prompt,
            user_prompt,
            temp=0.0,
            max_tokens=2600,
        )
        try:
            payload = narrative_guard.parse_json_object(raw)
        except Exception as exc:
            raise narrative_guard.NarrativeGuardError(
                f"逐章叙事审计输出无法解析：{exc}"
            ) from exc
        normalized, issues = narrative_guard.validate_audit(
            payload,
            chapter_text=chapter_content,
            requirements=requirements,
            min_score=int(self.config.get("narrative_guard_min_score", 75) or 75),
        )
        # A reviewer occasionally returns a FAIL whose own repair instruction
        # explicitly says the cited requirement was met and the status was a
        # misjudgment.  Never silently override it, but allow exactly one fresh
        # evidence-bound adjudication before rewriting an otherwise valid chapter.
        admits_self_error = narrative_guard.audit_admits_self_error(payload)
        needs_evidence_retry = narrative_guard.audit_needs_evidence_readjudication(
            payload, issues
        )
        audit_text = json.dumps(payload, ensure_ascii=False)
        mechanical_position_conflict = bool(
            trigger_positions
            and all(float(item.get("percent", 100.0)) <= 60.0 for item in trigger_positions)
            and any(marker in audit_text for marker in (
                "前移", "前60%", "60%以内", "65%", "70%", "触发过晚",
            ))
        )
        if issues and (
            admits_self_error or needs_evidence_retry or mechanical_position_conflict
        ):
            correction_prompt = (
                user_prompt
                + "\n\n【上轮审计需要一次最终复核】\n"
                + (
                    "上轮 JSON 的理由明确承认某项要求实际已满足、却仍标为缺失或冲突。"
                    if admits_self_error else
                    (
                        "上轮对关键触发点的位置作了错误目测；程序中文字符计数是唯一有效的位置证据。"
                        if mechanical_position_conflict else
                        "上轮把所有细纲项都标为 MET，但提供的引文不是正文中的连续原句，程序无法定位。"
                    )
                )
                + "请重新阅读候选正文与细纲，逐项重做审计。不得因为上轮承认误判就自动放行；"
                + "若正文证据确实存在，必须把对应项改为 MET，并据全文事实重新评分和给出 verdict；"
                + "每个 draft_quote 和 progress_evidence_quote 必须从正文逐字复制一段连续原句，"
                + "不得复制细纲、不得概括、不得用省略号拼接不连续句子。"
                + "若证据不存在，保留 FAIL 并给出可定位的真实缺口。只输出完整 JSON。\n"
                + f"【上轮 JSON】\n{json.dumps(payload, ensure_ascii=False)[:7000]}\n"
                + f"【上轮程序校验问题】\n{'；'.join(issues[:12])}"
            )
            corrected_raw = self.call_llm_review(
                system_prompt,
                correction_prompt,
                temp=0.0,
                max_tokens=2600,
            )
            try:
                corrected_payload = narrative_guard.parse_json_object(corrected_raw)
                corrected_normalized, corrected_issues = narrative_guard.validate_audit(
                    corrected_payload,
                    chapter_text=chapter_content,
                    requirements=requirements,
                    min_score=int(self.config.get("narrative_guard_min_score", 75) or 75),
                )
            except Exception as exc:
                issues.append(f"叙事审计自相矛盾复核无法解析：{exc}")
            else:
                normalized, issues = corrected_normalized, corrected_issues
                raw = corrected_raw
                review_user_prompt = correction_prompt
                self._append_batch_audit({
                    "event": "narrative_guard_self_conflict_readjudicated",
                    "chapter": chap_num,
                    "status": normalized.get("verdict", ""),
                    "issues": issues[:6],
                })
        window = max(
            3, int(self.config.get("narrative_guard_drift_window", 5) or 5)
        )
        previous = narrative_guard.load_recent_audits(
            plot_dir,
            before_chapter=chap_num,
            limit=window - 1,
        )
        issues.extend(narrative_guard.evaluate_rolling_drift(
            normalized,
            previous,
            marginal_score=int(
                self.config.get("narrative_guard_marginal_score", 82) or 82
            ),
            max_consecutive_marginal=int(
                self.config.get(
                    "narrative_guard_max_consecutive_marginal", 2
                ) or 2
            ),
        ))
        if issues:
            self._append_batch_audit({
                "event": "narrative_guard_rejected_detail",
                "chapter": chap_num,
                "issues": issues[:12],
                "audit": normalized,
            })
            raise narrative_guard.NarrativeGuardError("；".join(issues[:12]))
        review_model = self.get_review_model_name()
        normalized["review_raw_response"] = raw
        normalized.update({
            "chapter": int(chap_num),
            "chapter_sha256": state_ledger.chapter_sha256(chapter_content),
            "review_model": review_model,
            "review_receipt": narrative_guard.build_review_receipt(
                chapter_text=chapter_content,
                system_prompt=system_prompt,
                user_prompt=review_user_prompt,
                raw_response=raw,
                review_model=review_model,
                decision_payload=normalized,
            ),
        })
        self._append_batch_audit({
            "event": "narrative_guard_passed",
            "chapter": chap_num,
            "scores": normalized.get("scores", {}),
            "summary": normalized.get("summary", "")[:500],
        })
        return normalized

    def _persist_narrative_audit_result(
        self, result, chap_num, chapter_content
    ):
        if not result:
            raise narrative_guard.NarrativeGuardError(
                "正式章节缺少逐章叙事审计结果"
            )
        if result.get("chapter_sha256") != state_ledger.chapter_sha256(chapter_content):
            raise narrative_guard.NarrativeGuardError(
                "逐章叙事审计对应的正文哈希不一致"
            )
        return narrative_guard.persist_audit(
            generator.DIRS["plot"],
            chap_num,
            chapter_content,
            result,
            model_name=self._require_automatic_review_model(
                result.get("review_model"), f"第{int(chap_num)}章叙事审计结果"
            ),
            min_score=int(self.config.get("narrative_guard_min_score", 75) or 75),
        )

    def _call_state_review(self, system_prompt, user_prompt, *, chapter, attempt, phase, temp):
        """Expose actual request boundaries; a heartbeat is not model progress."""
        started = time.monotonic()
        details = {"chapter": int(chapter), "stage": "structured_state",
                   "attempt": attempt, "phase": phase}
        self._formal_suffix_emit_progress("state_model_call_started", **details)
        try:
            result = self.call_llm_review(system_prompt, user_prompt, temp=temp, max_tokens=3600)
        except Exception:
            self._formal_suffix_emit_progress("state_model_call_failed", **details,
                                              elapsed_seconds=round(time.monotonic() - started, 1))
            raise
        self._formal_suffix_emit_progress("state_model_call_completed", **details,
                                          elapsed_seconds=round(time.monotonic() - started, 1))
        return result

    def _prepare_structured_state_delta(
        self,
        chap_num,
        chapter_content,
        chapter_outline="",
        state_override=None,
        repair_context=None,
        rejection_callback=None,
    ):
        """Prepare and audit a delta before the chapter is allowed to become official."""
        if not self.config.get("structured_state_enabled", True):
            return None
        if getattr(self, "_state_repair_persistence_fault", ""):
            raise state_ledger.StateExtractionError(self._state_repair_persistence_fault)
        plot_dir = generator.DIRS["plot"]
        state = state_override if isinstance(state_override, dict) else state_ledger.load_state(plot_dir)
        chapter_hash = state_ledger.chapter_sha256(chapter_content)
        if state.get("applied_chapters", {}).get(str(chap_num)) == chapter_hash:
            return {
                "chapter": int(chap_num),
                "chapter_sha256": chapter_hash,
                "already_committed": True,
                "delta": None,
            }

        prior_context, _manifest = state_ledger.render_context(
            state,
            chap_num,
            chapter_outline or "",
            "",
            max_chars=7000,
            stale_warn_chapters=max(3, int(self.config.get("subplot_stale_warn_chapters", 8) or 8)),
        )
        if self.config.get("temporal_memory_enabled", True):
            audit_memory_hits = temporal_memory.retrieve(
                plot_dir,
                f"{chapter_outline or ''}\n{chapter_content}",
                before_chapter=chap_num,
                max_hits=max(4, int(self.config.get("temporal_memory_max_hits", 24) or 24)),
                max_chars=max(800, int(self.config.get("temporal_memory_max_chars", 2800) or 2800)),
            )
            audit_memory_context = temporal_memory.render_retrieval(
                audit_memory_hits,
                max_chars=max(800, int(self.config.get("temporal_memory_max_chars", 2800) or 2800)),
            )
            if audit_memory_context:
                prior_context += "\n\n" + audit_memory_context
        system_prompt, base_prompt = state_ledger.build_extraction_prompts(
            chap_num,
            chapter_content,
            chapter_outline or "",
            prior_context,
            progression_context=continuity_guard.load_canon(plot_dir) or {},
        )
        attempts = max(1, min(3, int(self.config.get("state_delta_max_attempts", 2) or 2)))
        verified_source_context = state_ledger.build_verified_prose_context(
            state, chap_num, self._official_chapter_paths_by_number()
        )
        last_issues = []
        previous_raw = ""
        repair_history = []
        # A rejected output is repair input only, never a prepared delta. The
        # caller also binds it to the complete preflight context fingerprint.
        if (
            isinstance(repair_context, dict)
            and repair_context.get("schema_version") == 1
            and repair_context.get("status") == "REJECTED"
            and repair_context.get("chapter") == int(chap_num)
            and repair_context.get("chapter_sha256") == chapter_hash
            and isinstance(repair_context.get("previous_raw"), str)
            and len(repair_context["previous_raw"]) <= 60000
            and isinstance(repair_context.get("issues"), list)
            and repair_context["issues"]
            and all(isinstance(issue, str) and issue for issue in repair_context["issues"])
        ):
            last_issues = repair_context["issues"][:40]
            previous_raw = repair_context["previous_raw"]
            saved_history = repair_context.get("repair_history", [])
            if isinstance(saved_history, list) and all(isinstance(row, dict) for row in saved_history):
                repair_history = saved_history[-12:]
            self._append_batch_audit({
                "event": "structured_state_repair_context_loaded",
                "chapter": int(chap_num),
                "issue_count": len(last_issues),
            })
        self._state_ledger_call_active = True
        try:
            rejection_limit = self.config.get("state_repair_max_rejections", 6) or 6
            stopped = state_repair.stop_reason(repair_history, rejection_limit)
            if stopped:
                raise state_ledger.StateExtractionError(
                    "STATE_REPAIR_STOPPED：" + stopped +
                    "；已保留正文及状态反馈。需检查提取/审计规则，勿原样重跑或改正文碰通过。"
                )
            for attempt in range(1, attempts + 1):
                continuity_reject = False
                repair_block = ""
                repair_base, repair_permissions = None, None
                if last_issues:
                    try:
                        repair_base = state_ledger.parse_json_object(previous_raw)
                        repair_permissions = state_repair.plan(
                            repair_base, last_issues, expected_chapter=chap_num
                        )
                    except Exception:
                        pass
                    repair_block = (
                        "\n\n上次失败事务留下的待修复输出不是正史或通过证据。必须逐条修正，不能通过删除全部事件逃避记账：\n- "
                        + "\n- ".join(last_issues[:40])
                        + "\n保留上一份输出中未被指出问题且仍获原文支持的条目，只修拒绝字段和补遗漏；"
                        + "不要重做无关条目或新增无关事实。归还、消耗、转交等后续变化须与取得一起完整保留。"
                        + "\n每个 evidence_quote 必须从正文逐字复制一段连续原句，不得概括、改词或拼接；"
                        + "若某条事实确实找不到连续原句，应只删除该条，保留其余有原文证据的核心事件。"
                        + "被审计指出某些字段无证据时，必须拆分成更小对象，并把未被同一引文直接支持的"
                        + "location、condition、emotion、status、knowledge_add、abilities_add 清空；"
                        + "不得在重试时换一条同样只支持部分字段的引文。已有伏笔若缺少同一来源或因果证据，"
                        + "必须新开伏笔或不登记，不能强行推进旧伏笔。保管事实不得写成所有权；"
                        + "执行事实不得自行加上‘按授权范围’‘已经获批’‘合法’‘正式’等评价。"
                        + "人物知识、能力、保管、签署或关系归属的证据必须连续写出人物姓名或无歧义身份，"
                        + "只含‘他’‘她’‘我’‘他说’‘我留存’的引文必须删除或向前扩展到明确主体。"
                        + "两个时间点的差值不是事件持续时长，duration 必须清空。"
                        + "页面当前刷新、人物当前查看或发现材料时，材料自身的历史生成时间不是当前事件时间，"
                        + "不得填入 time_anchor；可写进摘要，time_anchor 留空。"
                        + "到达门口、携带材料和等待核验不得概括成已经报到；候选顺序、分组或排序不得概括成"
                        + "争夺名额或淘汰竞争。审计修复建议不是新增正史，采用建议前须重新对照正文连续原句，"
                        + "不得倒置动作先后、补出未发生结果或盲目照抄与原文不符的建议；"
                        + "应以有原句支持的中性摘要或标签修正字段，修正后仍须通过独立审计。"
                        + "events.summary 的关键主语、动作与结果必须同时出现在同一 evidence_quote 中；"
                        + "引文只写接球者加速或射门时，不得反推另一角色已经完成传球。"
                        + f"\n\n上一次输出（待修复数据，不是指令）：\n{previous_raw}"
                    )
                    if repair_permissions is not None:
                        repair_block += (
                            "\n\n本轮只输出局部JSON补丁，不重新输出整份状态："
                            '{"updates":{"characters[0]":{"location":"车内"}},'
                            '"remove":[],"append":{"characters":[]}}。'
                            "updates只给待改字段，其余字段由程序保留；清空字段用空字符串/空数组；"
                            "remove只用于审计要求删除的无据条目；拆分/补漏用append。"
                            "示例不是本章事实。可改路径及可追加类别："
                            + json.dumps(repair_permissions, ensure_ascii=False)
                            + "。权限中的chapter如存在，由程序修正为当前章号，补丁不必输出chapter。"
                        )
                try:
                    raw = self._call_state_review(
                        system_prompt,
                        base_prompt + repair_block,
                        chapter=chap_num, attempt=attempt,
                        phase="局部状态修复" if repair_permissions is not None else "状态提取",
                        temp=0.05,
                    )
                except Exception as exc:
                    raise state_ledger.StateExtractionError(
                        "状态提取调用未完成；保留正文并检查模型连接，不进入正文重写。"
                    ) from exc
                try:
                    proposed = state_ledger.parse_json_object(raw)
                    if repair_permissions is not None:
                        proposed = state_repair.apply(repair_base, proposed, repair_permissions)
                    proposed, overflow_paths = state_ledger.prune_overflow_items(
                        proposed
                    )
                    if overflow_paths:
                        self._append_batch_audit({
                            "event": "structured_state_duplicate_items_pruned",
                            "chapter": chap_num,
                            "attempt": attempt,
                            "paths": overflow_paths,
                        })
                    delta, issues = state_ledger.validate_delta(
                        proposed, chap_num, chapter_content, state,
                        progression_context=continuity_guard.load_canon(plot_dir) or {},
                    )
                    if issues:
                        sanitized, pruned_paths = state_ledger.prune_unlocatable_evidence_items(
                            proposed, issues
                        )
                        if pruned_paths:
                            pruned_delta, pruned_issues = state_ledger.validate_delta(
                                sanitized, chap_num, chapter_content, state,
                                progression_context=continuity_guard.load_canon(plot_dir) or {},
                            )
                            if not pruned_issues:
                                proposed, delta, issues = sanitized, pruned_delta, []
                                self._append_batch_audit({
                                    "event": "structured_state_unlocatable_evidence_pruned",
                                    "chapter": chap_num,
                                    "attempt": attempt,
                                    "paths": pruned_paths,
                                })
                    previous_raw = json.dumps(proposed, ensure_ascii=False)
                except Exception as exc:
                    delta = {}
                    if repair_permissions is not None:
                        # A malformed/out-of-scope patch cannot replace our repair
                        # base or erase the original rejected paths.
                        issues = list(last_issues) + ["局部状态补丁未应用：" + str(exc)]
                    else:
                        previous_raw, issues = raw, [str(exc)]
                audit_enabled = bool(
                    self.config.get("state_delta_independent_audit", True)
                    or self.config.get("pre_save_continuity_audit_enabled", True)
                    or any(item.get("progression") for item in delta.get("characters", []))
                )
                if not issues and audit_enabled:
                    audit_system, audit_prompt = state_ledger.build_delta_audit_prompts(
                        delta,
                        state_context=prior_context,
                        chapter_text=chapter_content,
                        verified_source_context=verified_source_context,
                        progression_context=continuity_guard.load_canon(plot_dir) or {},
                    )
                    try:
                        audit_raw = self._call_state_review(
                            audit_system,
                            audit_prompt,
                            chapter=chap_num, attempt=attempt, phase="独立证据审计", temp=0.0,
                        )
                    except Exception as exc:
                        # An unavailable reviewer made no content judgment.
                        # Stop without spending another extraction attempt or
                        # replacing the last evidence-backed REJECTED hint.
                        error_text = str(exc)
                        diagnostic = {
                            "exception_type": type(exc).__name__,
                            "error_sha256": hashlib.sha256(
                                error_text.encode("utf-8", errors="replace")
                            ).hexdigest(),
                        }
                        for key in ("stdout_sha256", "stderr_sha256"):
                            match = re.search(
                                rf"\b{key}=([a-f0-9]{{64}})(?![a-f0-9])", error_text
                            )
                            if match:
                                diagnostic[key] = match.group(1)
                        match = re.search(
                            r"\bfailure_hint=(unknown|service_capacity|usage_limit|rate_limit|authentication|context_limit|connection)\b",
                            error_text,
                        )
                        if match:
                            diagnostic["failure_hint"] = match.group(1)
                        self._append_batch_audit({
                            "event": "structured_state_audit_unavailable",
                            "chapter": chap_num,
                            "attempt": attempt,
                            "diagnostic": diagnostic,
                        })
                        raise state_ledger.StateExtractionError(
                            "独立证据审计调用未完成："
                            + json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)
                        ) from None
                    try:
                        audit_payload = state_ledger.parse_json_object(audit_raw)
                        verified_draft_rejections = []
                        issues = state_ledger.validate_delta_audit(
                            audit_payload, delta, chapter_text=chapter_content,
                            verified_draft_rejections=verified_draft_rejections,
                        )
                        # Exhausted extraction retries must remain failures.
                        # Deleting audited rows can silently lose irreversible
                        # resource changes while leaving unrelated events valid.
                        continuity_reject = bool(verified_draft_rejections)
                    except Exception as exc:
                        issues = [f"独立证据审计失败：{str(exc)[:180]}"]
                        continuity_reject = False
                if not issues:
                    self._append_batch_audit({
                        "event": "structured_state_prepared",
                        "chapter": chap_num,
                        "attempt": attempt,
                        "delta_counts": {
                            key: len(delta.get(key, [])) for key in state_ledger.MAX_ITEMS
                        },
                    })
                    return {
                        "chapter": int(chap_num),
                        "chapter_sha256": chapter_hash,
                        "already_committed": False,
                        "delta": delta,
                    }
                last_issues = issues
                repair_history = state_repair.record(repair_history, previous_raw, last_issues)
                stopped = state_repair.stop_reason(repair_history, rejection_limit)
                if callable(rejection_callback):
                    try:
                        rejection_callback({
                            "schema_version": 1,
                            "status": "REJECTED",
                            "chapter": int(chap_num),
                            "chapter_sha256": chapter_hash,
                            # Empty/oversized responses still consume failure
                            # budget. Keep bounded repair text, never drop history.
                            "previous_raw": previous_raw if len(previous_raw) <= 60000 else "",
                            "previous_raw_omitted": len(previous_raw) > 60000,
                            "previous_raw_sha256": state_repair.digest(previous_raw),
                            "issues": list(last_issues[:40]),
                            "repair_history": repair_history,
                            "repair_stop_reason": stopped,
                            "repair_scope": "prose" if continuity_reject else "state_only",
                        })
                    except Exception as exc:
                        self._state_repair_persistence_fault = (
                            "STATE_REPAIR_STORAGE_FAILED：状态失败记录无法保存，已停止模型重试；"
                            "保留正文，检查存储后重新进行读写验证。"
                        )
                        self._append_batch_audit({
                            "event": "structured_state_repair_context_store_failed",
                            "chapter": int(chap_num),
                            "error": type(exc).__name__,
                        })
                        raise state_ledger.StateExtractionError(
                            self._state_repair_persistence_fault
                        ) from exc
                self._append_batch_audit({
                    "event": "structured_state_rejected",
                    "chapter": chap_num,
                    "attempt": attempt,
                    "issues": issues[:20],
                })
                if continuity_reject or stopped:
                    # Only a reject explicitly aimed at the draft proves the prose
                    # contradicts canon.  A rejected structured path (even when the
                    # auditor cites prior state) is still an extraction error and
                    # should be retried with the audit's repair instructions.
                    break
            message = "；".join(last_issues[:8]) or "状态增量未通过证据校验"
            if continuity_reject:
                raise state_ledger.StateProseConflictError(message)
            if stopped:
                message = "STATE_REPAIR_STOPPED：" + stopped + "；" + message
            raise state_ledger.StateExtractionError(
                message + "；仅修状态提取，不应改写正文；原样重复失败需检查规则。"
            )
        finally:
            self._state_ledger_call_active = False

    def _commit_prepared_state_delta(self, prepared, chapter_content):
        if not prepared:
            return None
        actual_hash = state_ledger.chapter_sha256(chapter_content)
        if prepared.get("chapter_sha256") != actual_hash:
            raise state_ledger.StateLedgerError("保存前审核后的正文又发生变化，拒绝写入旧状态增量")
        if prepared.get("already_committed"):
            if self.config.get("temporal_memory_enabled", True):
                temporal_memory.ensure_synced(generator.DIRS["plot"])
            return state_ledger.load_state(generator.DIRS["plot"])
        delta = prepared.get("delta")
        if not isinstance(delta, dict):
            raise state_ledger.StateLedgerError("保存前状态增量缺失")
        updated = state_ledger.commit_validated_delta(
            generator.DIRS["plot"], delta, chapter_content
        )
        if self.config.get("temporal_memory_enabled", True):
            temporal_memory.sync_validated_delta(
                generator.DIRS["plot"], delta, actual_hash
            )
        self._append_batch_audit({
            "event": "structured_state_committed",
            "chapter": prepared.get("chapter"),
            "delta_counts": {
                key: len(delta.get(key, [])) for key in state_ledger.MAX_ITEMS
            },
        })
        return updated

    def _persist_commercial_review_result(self, result, chap_num):
        """Persist a reviewed milestone only after its chapter is official."""
        if not result:
            return result
        commercial_reviewer.persist_review(
            self._commercial_review_report_dir(),
            int(chap_num),
            result,
            model_name=self._commercial_review_model_for_result(result),
        )
        self._persist_commercial_review_actions(result, chap_num)
        return result

    def _persist_unbound_commercial_review_actions(
        self, result, chap_num, *, bound_to_commit=False
    ):
        """Durably keep simple-mode revision advice after a safe commit."""
        if not result or bound_to_commit:
            return False
        self._persist_commercial_review_result(result, chap_num)
        self._append_batch_audit({
            "event": "commercial_revision_actions_persisted",
            "chapter": int(chap_num),
            "severity": result.get("severity"),
            "status": result.get("status"),
            "action": result.get("action"),
        })
        return True

    def _publish_committed_chapter(self, chapter_path, chap_num, chapter_status):
        """Resolve the publish step after all canonical metadata is durable."""
        if chapter_status == "正式可用":
            if (
                self._commercial_publish_release_required()
                and not self._commercial_publish_release_ready()
            ):
                self._remove_publish_copy(chap_num)
                return "商业阶段审稿通过前暂不进入发布稿"
            if self._commercial_publish_release_required():
                released = self._sync_approved_chapters_to_publish(chap_num)
                return f"已放行并同步 {released} 章发布稿" if released else "发布稿已同步"
            publish_path = self._copy_to_publish(chapter_path, chap_num)
            return f"已同步发布稿 {os.path.basename(publish_path)}"
        removed = self._remove_publish_copy(chap_num)
        if removed:
            return "本章仍待复核，已撤回旧发布稿"
        return ""

    def _recover_pending_chapter_commit(self):
        """Replay a crash-interrupted post-save commit without another model call."""
        plot_dir = generator.DIRS["plot"]
        pending = chapter_commit.load(plot_dir)
        if not pending:
            return {"status": "NONE", "chapter": 0, "receipt": ""}

        steps = pending.get("steps") or {}
        chapter_path = str(pending.get("chapter_path") or "")
        if not steps.get("chapter_saved"):
            if not chapter_path or not os.path.exists(chapter_path):
                chapter_commit.discard_unwritten(plot_dir)
                return {"status": "DISCARDED", "chapter": int(pending.get("chapter") or 0), "receipt": ""}
            # The process may have died after os.replace but before the journal
            # step was marked.  The hash proves whether that save completed.
            try:
                chapter_commit.verify_saved_chapter(pending)
            except chapter_commit.ChapterCommitError:
                if pending.get("replacement"):
                    chapter_commit.discard_unwritten(plot_dir)
                    return {
                        "status": "DISCARDED",
                        "chapter": int(pending.get("chapter") or 0),
                        "receipt": "",
                    }
                raise
            pending = chapter_commit.mark_step(plot_dir, "chapter_saved")
            steps = pending.get("steps") or {}

        chapter_content = chapter_commit.verify_saved_chapter(pending)
        chap_num = int(pending.get("chapter") or 0)
        outline = str(pending.get("chapter_outline") or "")
        chapter_status = str(pending.get("chapter_status") or "正式可用")
        notes = list(pending.get("chapter_review_notes") or [])
        strict_commit = bool(pending.get("narrative_audit_required", True))
        prepared_state_delta = pending.get("prepared_state_delta")
        narrative_audit_result = pending.get("narrative_audit_result")
        if strict_commit and not narrative_audit_result:
            raise RuntimeError(
                f"第{chap_num}章待恢复事务缺少逐章叙事 PASS 证据，"
                "拒绝自动补成正式章节或发布稿"
            )

        if pending.get("replacement") and not steps.get("state_superseded"):
            if prepared_state_delta:
                state_ledger.supersede_last_chapter_delta(
                    plot_dir,
                    chap_num,
                    expected_chapter_sha256=str(
                        pending.get("previous_chapter_sha256") or ""
                    ),
                )
                if self.config.get("temporal_memory_enabled", True):
                    temporal_memory.rebuild_from_deltas(plot_dir)
            pending = chapter_commit.mark_step(plot_dir, "state_superseded")
            steps = pending.get("steps") or {}

        if pending.get("replacement") and not steps.get("canon_restored"):
            continuity_guard.restore_before_chapter(plot_dir, chap_num)
            pending = chapter_commit.mark_step(plot_dir, "canon_restored")
            steps = pending.get("steps") or {}

        if pending.get("replacement") and not steps.get("narrative_audit_superseded"):
            if narrative_audit_result:
                narrative_guard.supersede_audit(
                    plot_dir,
                    chap_num,
                    expected_chapter_sha256=str(
                        pending.get("previous_chapter_sha256") or ""
                    ),
                )
            pending = chapter_commit.mark_step(
                plot_dir, "narrative_audit_superseded"
            )
            steps = pending.get("steps") or {}

        if not steps.get("canon_updated"):
            try:
                if self.config.get("canon_guard_enabled", True):
                    continuity_guard.update_after_chapter(
                        plot_dir,
                        chap_num,
                        chapter_content,
                        chapter_outline=outline,
                        prepared_state_delta=prepared_state_delta,
                    )
            except Exception as exc:
                if strict_commit:
                    raise
                notes.append(f"正史状态同步失败（不阻塞正文）：{str(exc)[:180]}")
                self._append_batch_audit({
                    "event": "simple_release_canon_warning",
                    "chapter": chap_num,
                    "error": str(exc)[:500],
                })
            pending = chapter_commit.mark_step(plot_dir, "canon_updated")
            steps = pending.get("steps") or {}

        if not steps.get("state_committed"):
            if prepared_state_delta:
                try:
                    self._commit_prepared_state_delta(
                        prepared_state_delta, chapter_content
                    )
                except Exception as exc:
                    if strict_commit:
                        raise
                    notes.append(f"结构化状态同步失败（不阻塞正文）：{str(exc)[:180]}")
                    self._append_batch_audit({
                        "event": "simple_release_state_warning",
                        "chapter": chap_num,
                        "error": str(exc)[:500],
                    })
            pending = chapter_commit.mark_step(plot_dir, "state_committed")
            steps = pending.get("steps") or {}

        if not steps.get("derived_memory_rebuilt"):
            if prepared_state_delta:
                try:
                    knowledge_manager.rebuild_memory_sources_from_structured_state(
                        plot_dir,
                        archive_tag=(
                            f"chapter_{chap_num:04d}_"
                            f"{str(pending.get('previous_chapter_sha256') or '')[:12]}"
                            if pending.get("replacement") else ""
                        ),
                    )
                except Exception as exc:
                    if strict_commit:
                        raise
                    notes.append(f"备忘录重建失败（不阻塞正文）：{str(exc)[:180]}")
                    self._append_batch_audit({
                        "event": "simple_release_memory_warning",
                        "chapter": chap_num,
                        "error": str(exc)[:500],
                    })
            pending = chapter_commit.mark_step(plot_dir, "derived_memory_rebuilt")
            steps = pending.get("steps") or {}

        if not steps.get("facts_updated"):
            try:
                knowledge_manager.update_fact_db(
                    plot_dir,
                    chap_num,
                    chapter_content,
                    chapter_path=chapter_path,
                    chapter_outline=outline,
                )
                writeback_interval = max(
                    1, int(self.config.get("knowledge_writeback_interval", 10) or 10)
                )
                if chap_num == 1 or chap_num % writeback_interval == 0:
                    knowledge_manager.write_knowledge_snapshots(
                        plot_dir,
                        generator.DIRS["chars"],
                        generator.DIRS["world"],
                        chap_num,
                    )
            except Exception as exc:
                if strict_commit:
                    raise
                notes.append(f"事实库同步失败（不阻塞正文）：{str(exc)[:180]}")
                self._append_batch_audit({
                    "event": "simple_release_fact_warning",
                    "chapter": chap_num,
                    "error": str(exc)[:500],
                })
            pending = chapter_commit.mark_step(plot_dir, "facts_updated")
            steps = pending.get("steps") or {}

        if not steps.get("status_recorded"):
            chars = len(re.findall(r'[\u4e00-\u9fff]', chapter_content))
            self._record_chapter_status(
                chap_num,
                chapter_status,
                notes,
                chapter_path,
                chars,
                narrative_audit_result=narrative_audit_result,
            )
            pending = chapter_commit.mark_step(plot_dir, "status_recorded")
            steps = pending.get("steps") or {}

        if (
            "narrative_audit_persisted" in steps
            and not steps.get("narrative_audit_persisted")
        ):
            if narrative_audit_result:
                self._persist_narrative_audit_result(
                    narrative_audit_result,
                    chap_num,
                    chapter_content,
                )
            pending = chapter_commit.mark_step(
                plot_dir,
                "narrative_audit_persisted",
                narrative_audit_result=narrative_audit_result,
            )
            steps = pending.get("steps") or {}

        review_result = pending.get("commercial_review_result")
        if not steps.get("commercial_review_persisted"):
            try:
                self._persist_commercial_review_result(review_result, chap_num)
            except Exception as exc:
                if strict_commit:
                    raise
                notes.append(f"阶段审稿记录同步失败（不阻塞正文）：{str(exc)[:180]}")
                self._append_batch_audit({
                    "event": "simple_release_commercial_warning",
                    "chapter": chap_num,
                    "error": str(exc)[:500],
                })
            pending = chapter_commit.mark_step(
                plot_dir,
                "commercial_review_persisted",
                commercial_review_result=review_result,
            )
            steps = pending.get("steps") or {}

        if not steps.get("publish_resolved"):
            try:
                self._publish_committed_chapter(chapter_path, chap_num, chapter_status)
            except Exception as exc:
                if strict_commit:
                    raise
                notes.append(f"发布稿同步失败（正式正文已保留）：{str(exc)[:180]}")
                self._append_batch_audit({
                    "event": "simple_release_publish_warning",
                    "chapter": chap_num,
                    "error": str(exc)[:500],
                })
            chapter_commit.mark_step(plot_dir, "publish_resolved")

        receipt = chapter_commit.finalize(plot_dir)
        if hasattr(self, "_append_batch_audit"):
            self._append_batch_audit({
                "event": "pending_chapter_commit_recovered",
                "chapter": chap_num,
                "receipt": receipt,
            })
        return {"status": "RECOVERED", "chapter": chap_num, "receipt": receipt}

    def _update_structured_state_ledger(self, chap_num, chapter_content, chapter_outline=""):
        """Compatibility wrapper for manual saves and legacy-book migration."""
        prepared = self._prepare_structured_state_delta(
            chap_num, chapter_content, chapter_outline
        )
        return self._commit_prepared_state_delta(prepared, chapter_content)

    def _official_chapter_paths_by_number(self):
        result = {}
        out_dir = generator.DIRS.get("out", "output")
        if not os.path.isdir(out_dir):
            return result
        for root_dir, dirs, files in os.walk(out_dir):
            dirs[:] = [name for name in dirs if name != ".backup"]
            for name in files:
                if self._is_official_chapter_file(name):
                    path = os.path.join(root_dir, name)
                    result[self._extract_chap_num_from_path(path)] = path
        return result

    def _classify_pending_review_paths(self, next_chap):
        """Return (retryable_current, blocking_other) pending review drafts."""
        pending_reviews = []
        out_dir = generator.DIRS["out"]
        if os.path.isdir(out_dir):
            for root_dir, dirs, fnames in os.walk(out_dir):
                dirs[:] = [name for name in dirs if name != ".backup"]
                for fname in fnames:
                    if fname.endswith("_待审.txt"):
                        pending_reviews.append(os.path.join(root_dir, fname))
        official_numbers = set(self._official_chapter_paths_by_number())
        retryable_current = []
        blocking_other = []
        for path in pending_reviews:
            chapter = self._extract_chap_num_from_path(path)
            if chapter in official_numbers:
                continue
            if chapter == int(next_chap or 0):
                retryable_current.append(path)
            else:
                blocking_other.append(path)
        return retryable_current, blocking_other

    def _structured_state_gap(self, latest_chap=None):
        if not self.config.get("structured_state_enabled", True):
            return 0
        if latest_chap is None:
            _, _, _, latest_chap, _ = generator.get_latest_chapter_info()
        try:
            state = state_ledger.load_state(generator.DIRS["plot"])
            return max(0, int(latest_chap) - int(state.get("current_chapter", 0)))
        except Exception:
            return 0

    def _catch_up_structured_state(self, latest_chap):
        """One-click migration for books written before the evidence ledger existed."""
        if not self.config.get("structured_state_enabled", True) or latest_chap <= 0:
            return True
        state = state_ledger.load_state(generator.DIRS["plot"])
        start = int(state.get("current_chapter", 0)) + 1
        if start > latest_chap:
            return True
        paths = self._official_chapter_paths_by_number()
        self._ui_progress_append(
            f"[记忆补齐] 检测到旧书正史账本缺少第 {start}-{latest_chap} 章，正在自动逐章提取证据...\n"
        )
        for number in range(start, latest_chap + 1):
            if self._stop_event.is_set():
                raise state_ledger.StateLedgerError("用户停止了长期记忆补齐")
            path = paths.get(number)
            if not path:
                raise state_ledger.StateLedgerError(f"找不到第{number}章正式稿，无法补齐长期记忆")
            self._begin_chapter_model_budget(number)
            content = generator.read_text_safe(path)
            outline = self._extract_chapter_outline(number)
            self._update_structured_state_ledger(number, content, outline)
            self._ui_progress_append(f"  [OK] 第 {number} 章证据记忆已补齐\n")
        return True

    # ============================================================
    # 核心功能：写新章 / 续写
    # ============================================================
    def generate_new_chapter(self):
        req = self.prompt_text.get(1.0, tk.END).strip()
        if not req:
            messagebox.showwarning("提示", "请输入具体的写作要求！")
            return
        self.current_req = req
        sys_prompt = self.build_system_prompt_gui(current_prompt=req)
        user_prompt = f"<本章写作要求>\n{req}\n</本章写作要求>"
        self.start_generation_thread(sys_prompt, user_prompt, is_new=True)

    def continue_chapter(self):
        if not self.latest_filepath or not os.path.exists(self.latest_filepath):
            messagebox.showwarning("提示", "当前没有已写章节，无法续写！")
            return
        req = self.prompt_text.get(1.0, tk.END).strip()
        if not req:
            messagebox.showwarning("提示", "请输入接下来的情节发展要求！")
            return
        existing_content = generator.read_text_safe(self.latest_filepath)
        sys_prompt = self.build_system_prompt_gui(current_prompt=req)
        final_user_prompt = f"【本章已写内容（请紧接着继续往下写）】\n{existing_content}\n\n请根据上文语境和情绪，无缝续写。\n\n<本章写作要求>\n{req}\n</本章写作要求>"
        self.current_req = req
        self.start_generation_thread(sys_prompt, final_user_prompt, is_new=False)

    # ============================================================
    # 核心功能：批量挂机写 (含全部修复)
    # ============================================================

    # 内容漂移防火墙：禁词列表
    BANNED_KEYWORDS = [
        # 平台敏感词
        "共产党", "国家领导", "政府阴谋", "邪教",
        # 过度血腥
        "肠子流出", "内脏外露", "开膛破肚", "活剥人皮", "碎尸",
    ]

    def _auto_switch_volume_outline(self, chap_num):
        """根据章节号同步当前卷大纲内容，不再只改配置不改大纲。"""
        current_vol_name = self._get_story_volume_name(chap_num)
        if not current_vol_name:
            return None

        outline_path = os.path.join(generator.DIRS["plot"], "当前卷大纲.txt")
        current_outline = generator.read_text_safe(outline_path)
        desired_outline = self._extract_volume_outline_from_master(current_vol_name)
        if not desired_outline:
            return f"[WARN] 未能从全书大纲中提取 {current_vol_name} 内容，请检查全书大纲。"

        if current_outline.strip() == desired_outline.strip():
            return None

        with open(outline_path, "w", encoding="utf-8") as f:
            f.write(desired_outline)

        return f" 已同步当前卷大纲为{current_vol_name}"

    def _check_content_drift(self, content, chap_num=None):
        """快速内容漂移检测：禁词 + Markdown残留"""
        banned_keywords = self._load_tool_rules().get("banned_keywords") or self.BANNED_KEYWORDS
        normalized_keywords = []
        for item in banned_keywords:
            if isinstance(item, str):
                keyword = item
            elif isinstance(item, dict):
                keyword = str(item.get("keyword") or "")
            elif isinstance(item, (list, tuple)) and item:
                keyword = str(item[0] or "")
            else:
                keyword = ""
            if keyword:
                normalized_keywords.append(keyword)
        hits = [kw for kw in normalized_keywords if kw in content]
        if chap_num is not None:
            stage_blocked = story_architect.blocked_terms_for_chapter(
                generator.DIRS.get("plot", ""), int(chap_num)
            )
            hits.extend(term for term in stage_blocked if term and term in content)
        # 也检测 Markdown 残留
        md_patterns = [
            r'\*\*.+?\*\*', r'__.+?__', r'^\s*#{1,6}\s+',
            r'`[^`]+`',
        ]
        for pat in md_patterns:
            if re.search(pat, content, re.MULTILINE):
                hits.append("Markdown残留")
                break
        return hits

    def _sanitize_early_reveal_terms(self, content, chap_num):
        """早期章节只保留线索，不让正文提前坐实后期真相名词。"""
        if not self.config.get("early_reveal_sanitizer", True):
            return content, []
        text = content or ""
        hits = []
        reveal_rules = self._load_reveal_rules() or {}
        for topic in reveal_rules.get("topics") or []:
            if not isinstance(topic, dict):
                continue
            before_chapter = int(topic.get("earliest_hard") or 0)
            if before_chapter <= 0 or chap_num >= before_chapter:
                continue
            mappings = topic.get("sanitize_terms") or {}
            if isinstance(mappings, list):
                mappings = {
                    str(item[0]): str(item[1])
                    for item in mappings
                    if isinstance(item, (list, tuple)) and len(item) >= 2
                }
            if not isinstance(mappings, dict):
                continue
            for source, target in mappings.items():
                source = str(source)
                target = str(target)
                if source and source in text:
                    text = text.replace(source, target)
                    label = str(topic.get("label") or source)
                    if label not in hits:
                        hits.append(label)
        return text, hits

    def _sanitize_author_layer_chapter_references(self, content):
        """Remove book-structure references without changing story events."""
        text = content or ""
        if "\n" in text:
            title, body = text.split("\n", 1)
        else:
            title, body = "", text
        replacements = (
            (
                r"第[零〇一二三四五六七八九十百千万两0-9]+章",
                "此前",
            ),
            (r"(?:上一章|前一章|前文)", "此前"),
            (r"(?:下一章|后文|下文)", "随后"),
            (r"(?:本章|这一章)(?!程)", "眼下这段经历"),
            (r"全章(?:中|里|内)?", "整个过程"),
        )
        hits = []
        for pattern, replacement in replacements:
            body, count = re.subn(pattern, replacement, body)
            if count:
                hits.append({"pattern": pattern, "replacement": replacement, "count": count})
        sanitized = f"{title}\n{body}" if title else body
        return sanitized, hits

    def _ensure_text_file(self, path, content):
        if not os.path.exists(path):
            _atomic_write_text(path, content)

    def _write_text_if_placeholder(self, path, content):
        """只在目标为空或仍是模板占位时覆盖，避免改掉用户已写设定。"""
        content = (content or "").strip()
        if not content:
            return False
        current = generator.read_text_safe(path)
        if current and book_initializer.has_meaningful_content(current):
            return False
        _atomic_write_text(path, content.strip() + "\n")
        return True

    def _generate_valid_story_bible(
        self,
        plot_dir,
        title,
        genre,
        total,
        ranges,
        master_outline,
        canon_text,
    ):
        """Generate the machine contract, failing closed after one repair retry.

        A generic local outline is useful as a drafting aid, but it cannot prove
        that the selected book premise, cast and commercial promise were planned.
        Invalid model output is therefore retained only as an audit draft and is
        never promoted to ``story_bible.json``.
        """
        bible_sys, bible_user = book_initializer.build_story_bible_prompt(
            title,
            genre,
            total,
            ranges,
            master_outline,
            canon_text,
        )
        attempts = []
        previous_issues = []
        for attempt_number in range(1, 3):
            user_prompt = bible_user
            if previous_issues:
                user_prompt += (
                    "\n\n【上次输出未通过机器校验】\n- "
                    + "\n- ".join(previous_issues[:20])
                    + "\n请重新输出完整 JSON；不要解释，也不要沿用缺失字段。"
                )
            raw = self.call_llm_non_stream(
                bible_sys,
                user_prompt,
                temp=0.35,
                max_tokens=7600,
            )
            if self._stop_event.is_set():
                raise RuntimeError("用户已停止自动准备")
            candidate = book_initializer.parse_json_object(raw)
            previous_issues = story_architect.validate_story_bible_data(
                candidate, total
            )
            attempts.append({
                "attempt": attempt_number,
                "issues": previous_issues[:20],
                "raw_response": raw or "",
            })
            if not previous_issues:
                return candidate

        draft_path = os.path.join(plot_dir, "story_bible_自动草案.json")
        _atomic_write_text(
            draft_path,
            json.dumps(
                {
                    "status": "UNVERIFIED",
                    "reason": "模型输出连续两次未通过故事圣经机器校验",
                    "attempts": attempts,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        self._append_batch_audit({
            "event": "story_bible_generation_failed",
            "attempts": len(attempts),
            "issues": previous_issues[:20],
            "draft": os.path.basename(draft_path),
        })
        raise RuntimeError(
            "故事圣经连续两次未通过机器校验，已保存未验证草案；"
            "修复前禁止开始托管"
        )

    def _auto_initialize_project_knowledge(self, title, genre, total, ranges, on_complete=None):
        """开书后自动生成初始角色、世界观和基础大纲。"""
        self._ui_progress_append("\n正在自动立项并初始化角色/世界观/全书大纲...\n")
        initialization_ok = False
        try:
            plot_dir = generator.DIRS["plot"]
            blueprint = self._ensure_commercial_blueprint(title, genre, total)
            if blueprint:
                title = str(blueprint.get("final_title") or title).strip() or title
            commercial_context = commercial_planner.render_blueprint(
                blueprint, max_chars=6500
            ) if blueprint else ""
            if self._stop_event.is_set():
                raise RuntimeError("用户已停止自动准备")
            outline_path = os.path.join(plot_dir, "全书大纲.txt")
            existing_outline = generator.read_text_safe(outline_path)
            existing_world = generator.read_text_safe(
                os.path.join(generator.DIRS["world"], "自动生成_世界观.txt")
            )
            existing_characters = generator.read_text_safe(
                os.path.join(generator.DIRS["chars"], "自动生成_核心角色组.txt")
            )
            if all(book_initializer.has_meaningful_content(item) for item in (
                existing_outline, existing_world, existing_characters
            )):
                sections = {
                    "world": existing_world,
                    "characters": existing_characters,
                    "outline": existing_outline,
                    "tone": generator.read_text_safe(os.path.join(plot_dir, "基调铁律.txt")),
                    "canon": generator.read_text_safe(os.path.join(plot_dir, "唯一真相设定表.md")),
                    "reveal_rules": generator.read_text_safe(os.path.join(plot_dir, "reveal_rules.json")),
                }
            else:
                initialization_direction = (
                    str(self.config.get("book_brief") or "").strip()
                    or existing_outline
                )
                if commercial_context:
                    initialization_direction += (
                        "\n\n【已通过的商业立项合同，必须执行】\n" + commercial_context
                    )
                sys_prompt, user_prompt = book_initializer.build_initialization_prompt(
                    title,
                    genre,
                    total,
                    ranges,
                    existing_outline=initialization_direction,
                )
                result = self.call_llm_non_stream(sys_prompt, user_prompt, temp=0.55, max_tokens=5200)
                if self._stop_event.is_set():
                    raise RuntimeError("用户已停止自动准备")
                sections = book_initializer.parse_initialization_output(result)
                if not sections:
                    draft_path = os.path.join(plot_dir, "开书自动初始化草案.txt")
                    _atomic_write_text(draft_path, result or "")
                    self._ui_progress_append(f"  [WARN] 初始化结果未能分段，已保存草案：{os.path.basename(draft_path)}\n")
                    return

            writes = []
            chars_dir = generator.DIRS["chars"]
            world_dir = generator.DIRS["world"]
            if self._write_text_if_placeholder(os.path.join(world_dir, "自动生成_世界观.txt"), sections.get("world", "")):
                writes.append("世界观")
            if self._write_text_if_placeholder(os.path.join(chars_dir, "自动生成_核心角色组.txt"), sections.get("characters", "")):
                writes.append("核心角色")
            if self._write_text_if_placeholder(outline_path, sections.get("outline", "")):
                writes.append("全书大纲")
            initial_sections = story_architect.split_outline_sections(sections.get("outline", ""))
            required_opening = set(range(1, min(10, total) + 1))
            opening_draft = os.path.join(plot_dir, "前10章细纲_自动草案.txt")
            draft_opening_sections = story_architect.split_outline_sections(
                generator.read_text_safe(opening_draft)
            )
            if {1, 2, 3}.issubset(draft_opening_sections):
                initial_sections = draft_opening_sections
            if not {1, 2, 3}.issubset(initial_sections):
                opening_sys, opening_user = book_initializer.build_opening_outline_prompt(
                    title,
                    genre,
                    str(self.config.get("book_brief") or ""),
                    sections.get("world", ""),
                    sections.get("characters", ""),
                    sections.get("outline", ""),
                )
                opening_result = self.call_llm_non_stream(
                    opening_sys, opening_user, temp=0.45, max_tokens=6200
                )
                if self._stop_event.is_set():
                    raise RuntimeError("用户已停止自动准备")
                opening_sections = story_architect.split_outline_sections(opening_result)
                if required_opening.issubset(opening_sections):
                    initial_sections = opening_sections
                else:
                    # 长输出可能在第4-10章截断；黄金三章完整时先保存可用前缀，
                    # 后续章节交给滚动细纲补齐，不能让一次截断吞掉已验证的开篇合同。
                    if {1, 2, 3}.issubset(opening_sections):
                        initial_sections = opening_sections
                    _atomic_write_text(opening_draft, opening_result or "")
                    self._append_batch_audit({
                        "event": "opening_outline_incomplete",
                        "missing": sorted(required_opening - set(opening_sections)),
                        "draft": opening_draft,
                    })
            if blueprint and not self._project_has_official_chapters():
                opening_contract = "\n\n".join(
                    initial_sections.get(chapter, "") for chapter in (1, 2, 3)
                ).strip()
                if not {1, 2, 3}.issubset(initial_sections):
                    raise RuntimeError("黄金三章合同缺章，商业准备不放行")
                opening_sys, opening_user = book_initializer.build_opening_revision_prompt(
                    title,
                    str(self.config.get("book_brief") or "")
                    + "\n\n"
                    + commercial_context,
                    sections.get("world", ""),
                    sections.get("canon", ""),
                    opening_contract,
                )
                revised_opening = self.call_llm_review(
                    opening_sys, opening_user, temp=0.2, max_tokens=5200
                )
                if self._stop_event.is_set():
                    raise RuntimeError("用户已停止自动准备")
                revised_sections = story_architect.split_outline_sections(revised_opening)
                if not {1, 2, 3}.issubset(revised_sections):
                    raise RuntimeError("黄金三章总编修订结果不完整，商业准备不放行")
                for chapter in (1, 2, 3):
                    initial_sections[chapter] = revised_sections[chapter]
                self._append_batch_audit({
                    "event": "golden_three_contract_revised",
                    "chapters": [1, 2, 3],
                    "commercial_score": blueprint.get("commercial_score"),
                })
            first_ten = "\n\n".join(
                initial_sections[chap] for chap in range(1, min(10, total) + 1)
                if chap in initial_sections
            )
            if first_ten and self._write_text_if_placeholder(
                os.path.join(plot_dir, "第一卷逐章细纲.txt"),
                first_ten,
            ):
                writes.append("前10章细纲")

            reveal_rules_text = (sections.get("reveal_rules") or "").strip()
            try:
                initial_reveal_rules = json.loads(reveal_rules_text) if reveal_rules_text else {}
            except Exception:
                initial_reveal_rules = {}
            guardrails_missing = (
                not book_initializer.has_meaningful_content(sections.get("tone", ""))
                or not book_initializer.has_meaningful_content(sections.get("canon", ""))
            )
            if guardrails_missing:
                guard_sys, guard_user = book_initializer.build_guardrails_prompt(
                    title,
                    genre,
                    str(self.config.get("book_brief") or ""),
                    sections.get("world", ""),
                    sections.get("characters", ""),
                    sections.get("outline", ""),
                )
                guard_result = self.call_llm_non_stream(
                    guard_sys, guard_user, temp=0.25, max_tokens=4800
                )
                if self._stop_event.is_set():
                    raise RuntimeError("用户已停止自动准备")
                guard_sections = book_initializer.parse_initialization_output(guard_result)
                for section_name in ("tone", "canon", "reveal_rules"):
                    if guard_sections.get(section_name):
                        sections[section_name] = guard_sections[section_name]

            reveal_rules_text = (sections.get("reveal_rules") or "").strip()
            try:
                initial_reveal_rules = json.loads(reveal_rules_text) if reveal_rules_text else {}
            except Exception:
                initial_reveal_rules = {}
            if (
                not isinstance(initial_reveal_rules.get("topics"), list)
                or len(initial_reveal_rules.get("topics") or []) < 2
            ):
                reveal_sys, reveal_user = book_initializer.build_reveal_rules_prompt(
                    title,
                    str(self.config.get("book_brief") or ""),
                    sections.get("canon", ""),
                )
                reveal_result = self.call_llm_non_stream(
                    reveal_sys, reveal_user, temp=0.1, max_tokens=3200
                )
                if self._stop_event.is_set():
                    raise RuntimeError("用户已停止自动准备")
                parsed_reveal = book_initializer.parse_json_object(reveal_result)
                if isinstance(parsed_reveal.get("topics"), list):
                    sections["reveal_rules"] = json.dumps(parsed_reveal, ensure_ascii=False)
                else:
                    reveal_draft = os.path.join(plot_dir, "reveal_rules_自动草案.txt")
                    _atomic_write_text(reveal_draft, reveal_result or "")
                    self._append_batch_audit({
                        "event": "reveal_rules_incomplete",
                        "draft": reveal_draft,
                    })

            if self._write_text_if_placeholder(os.path.join(plot_dir, "基调铁律.txt"), sections.get("tone", "")):
                writes.append("基调铁律")
            if self._write_text_if_placeholder(os.path.join(plot_dir, "唯一真相设定表.md"), "# 唯一真相设定表\n\n" + sections.get("canon", "")):
                writes.append("唯一真相设定表")

            semantic_config = semantic_consistency_guard.load_semantic_invariants(
                self.project_dir or os.path.dirname(plot_dir)
            )
            if not semantic_config.get("valid"):
                semantic_sys, semantic_user = book_initializer.build_semantic_invariants_prompt(
                    title,
                    genre,
                    sections.get("world", ""),
                    sections.get("characters", ""),
                    sections.get("canon", ""),
                )
                semantic_raw = self.call_llm_non_stream(
                    semantic_sys, semantic_user, temp=0.05, max_tokens=5200
                )
                semantic_data = book_initializer.parse_json_object(semantic_raw)
                required_semantic_keys = {
                    "identity_bindings", "exclusive_fact_groups",
                    "forbidden_patterns", "numeric_rules",
                }
                if not required_semantic_keys.issubset(semantic_data):
                    draft_path = os.path.join(
                        plot_dir, "semantic_invariants_自动草案.json"
                    )
                    _atomic_write_text(draft_path, semantic_raw or "")
                    raise RuntimeError(
                        "语义真相合同缺少必填字段，已保存草案；修复前禁止开始托管"
                    )
                semantic_path = os.path.join(
                    plot_dir, semantic_consistency_guard.CONFIG_FILENAME
                )
                _atomic_write_text(
                    semantic_path,
                    json.dumps(semantic_data, ensure_ascii=False, indent=2),
                )
                semantic_config = semantic_consistency_guard.load_semantic_invariants(
                    self.project_dir or os.path.dirname(plot_dir)
                )
                if not semantic_config.get("valid"):
                    invalid_draft = os.path.join(
                        plot_dir, "semantic_invariants_自动草案.json"
                    )
                    os.replace(semantic_path, invalid_draft)
                    message = self._semantic_guard_message(semantic_config)
                    raise RuntimeError(
                        "语义真相合同无法执行，已转为草案：" + message
                    )
                writes.append("机器可执行语义真相合同")
            reveal_rules_text = (sections.get("reveal_rules") or "").strip()
            if reveal_rules_text:
                try:
                    reveal_rules = json.loads(reveal_rules_text)
                    if isinstance(reveal_rules, dict) and isinstance(reveal_rules.get("topics"), list):
                        reveal_rules["strict_mode"] = True
                        _atomic_write_text(
                            os.path.join(plot_dir, "reveal_rules.json"),
                            json.dumps(reveal_rules, ensure_ascii=False, indent=2),
                        )
                        self._reveal_rules_cache = None
                        writes.append("真相揭示规则")
                except Exception:
                    draft_path = os.path.join(plot_dir, "reveal_rules_自动草案.txt")
                    _atomic_write_text(draft_path, reveal_rules_text)

            story_bible_path = os.path.join(plot_dir, story_architect.STORY_BIBLE_FILENAME)
            story_bible = story_architect.load_story_bible(plot_dir)
            if story_architect.validate_story_bible_data(story_bible, total):
                story_bible = self._generate_valid_story_bible(
                    plot_dir,
                    title,
                    genre,
                    total,
                    ranges,
                    sections.get("outline", ""),
                    sections.get("canon", ""),
                )
                _atomic_write_text(
                    story_bible_path,
                    json.dumps(story_bible, ensure_ascii=False, indent=2),
                )
                writes.append("故事圣经")

            canon_state_path = os.path.join(plot_dir, continuity_guard.CANON_FILENAME)
            if not continuity_guard.load_canon(plot_dir):
                initial_canon = story_architect.build_initial_canon(
                    book_initializer.infer_protagonist(sections.get("characters", ""))
                )
                continuity_guard.save_canon(plot_dir, initial_canon)
                continuity_guard.save_canon_snapshot(plot_dir)
                writes.append("正史状态")

            if sections.get("outline"):
                current_vol = self._extract_volume_outline_from_master("第一卷")
                if current_vol:
                    self._write_text_if_placeholder(os.path.join(plot_dir, "当前卷大纲.txt"), current_vol)

            self._write_generated_project_rules()
            writes.append("番茄发布与题材规则")

            self._append_batch_audit({
                "event": "book_auto_initialized",
                "book_title": title,
                "writes": writes,
            })
            self._ui(self.refresh_knowledge_base)
            self._ui(self.refresh_status)
            self._ui_progress_append(f"  [OK] 开书初始化完成：{('、'.join(writes) if writes else '未覆盖已有资料')}\n")
            initialization_ok = True
        except Exception as e:
            self._append_batch_audit({
                "event": "book_auto_initialize_failed",
                "book_title": title,
                "error": str(e)[:500],
            })
            self._ui_progress_append(f"  [FAIL] 开书初始化失败：{str(e)[:100]}\n")
        finally:
            self._initialization_running = False
            self._finish_project_task()
            if on_complete is not None and not self._closing_requested:
                self._ui(lambda: self.root.after(120, lambda: on_complete(initialization_ok)))

    def _start_auto_initialize_project_knowledge(self, title, genre, total, ranges, on_complete=None):
        if not self.config.get("auto_initialize_knowledge", True):
            return False
        if not self._has_configured_model_provider():
            return False
        if not self._begin_project_task("开书初始化"):
            return False
        self._stop_event.clear()
        self._initialization_running = True
        self._set_busy_widgets(True)
        thread = threading.Thread(
            target=self._auto_initialize_project_knowledge,
            args=(title, genre, total, ranges, on_complete),
            daemon=True,
        )
        thread.start()
        return True

    def new_project_wizard(self):
        self.open_one_click_setup(force_new=True)

    def book_wizard(self):
        self.open_one_click_setup()

    def outline_audit(self):
        issues = self._run_health_check_internal(future_window=int(self.config.get("health_check_future_chapters", 30) or 30))
        fail_count = sum(1 for _, st in issues if st == "FAIL")
        warn_count = sum(1 for _, st in issues if st == "WARN")
        lines = ["体检大纲报告", "=" * 32, f"FAIL: {fail_count} / WARN: {warn_count}", ""]
        for desc, status in issues:
            prefix = "OK" if status == "PASS" else status
            lines.append(f"[{prefix}] {desc}")
        self._ui_clear("\n".join(lines))

    def _save_project_config_patch(self, **updates):
        path = os.path.join(self.project_dir, "project_config.json")
        data = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        if not isinstance(data, dict):
            data = {}
        data.update(updates)
        _atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
        self.config.update(updates)

    def _after_one_click_initialization(self, success):
        if not success:
            if self._stop_event.is_set():
                self._ui_progress_append("\n[PAUSE] 自动准备已按要求停止；再次点击主按钮可继续。\n")
                self._refresh_readiness_status()
                return
            messagebox.showerror(
                "自动准备未完成",
                "商业立项、黄金三章、角色、世界观或大纲准备未通过，"
                "整本任务没有启动。详情已写在进度页。",
            )
            self._refresh_readiness_status()
            return
        self.one_click_generate_full_book()

    def one_click_generate_full_book(self):
        """唯一主入口：自动设置检查、初始化、体检并生成到规划终章。"""
        if self._is_busy():
            return
        if self._project_needs_setup():
            self.open_one_click_setup()
            return
        if self._project_config_error:
            messagebox.showerror("项目配置无效", self._project_config_error)
            return
        if not self._has_configured_model_provider():
            self.open_one_click_setup()
            return
        try:
            self._run_provider_preflight()
        except Exception as exc:
            provider_name = self._provider_display_name()
            self._ui_progress_append(
                f"[FAIL] {provider_name} 开写前预检失败：{str(exc)[:220]}\n",
                clear=True,
            )
            messagebox.showerror(
                f"{provider_name} 预检失败",
                f"当前提供方不可用，整本任务没有启动。\n\n{str(exc)[:300]}",
            )
            return

        title = str(self.config.get("book_title") or "我的小说")
        genre = str(self.config.get("genre_template") or "通用空白")
        total = self._get_story_planned_end_chapter()
        ranges = [list(item) for item in self._get_volume_ranges()]
        if self._initialization_needed():
            with self._model_call_lock:
                self._model_call_count = 0
                self._model_call_limit = 16
                self._chapter_model_call_count = 0
                self._chapter_model_call_limit = 0
                self._chapter_model_call_number = 0
            self._update_model_usage_label()
            self._ui_progress_append(
                "[1/2] 正在自动商业立项，并准备角色、世界观、全书大纲、黄金三章与真相护栏...\n",
                clear=True,
            )
            started = self._start_auto_initialize_project_knowledge(
                title,
                genre,
                total,
                ranges,
                on_complete=self._after_one_click_initialization,
            )
            if not started:
                messagebox.showerror("无法开始", "自动准备任务未能启动，请查看当前状态。")
            return

        _, next_chap, _, latest_chap, _ = generator.get_latest_chapter_info()
        trial_stop = self._get_trial_stop_chapter()
        if self._trial_mode_pending() and trial_stop and latest_chap >= trial_stop:
            if not self._trial_gate_ready():
                latest_review = self._load_latest_commercial_review()
                summary = str(latest_review.get("gate_block_reason") or latest_review.get("summary") or "")
                messagebox.showerror(
                    f"第{trial_stop}章闸门未通过",
                    f"30章试写尚未取得明确 PASS/CONTINUE，不能继续扩大生产。\n\n{summary[:300]}",
                )
                return
            self._save_project_config_patch(
                trial_continue_confirmed=True,
                trial_gate_migration_required=False,
            )
            self._ui_progress_append(
                f"\n[OK] 第{trial_stop}章试写已通过，现由用户确认继续生成整本。\n",
                clear=True,
            )

        planned_end = self._get_story_planned_end_chapter()
        target_end = self._get_generation_target_end()
        if not target_end or target_end < next_chap:
            messagebox.showinfo("整本生成", "这本书已经写到规划终章，或终章规划无效。")
            return
        count = target_end - next_chap + 1
        if count <= 0:
            messagebox.showinfo("整本生成", "已经写到规划终章。")
            return
        check_window = min(
            count,
            int(self.config.get("health_check_future_chapters", 30) or 30),
        )
        issues = self._run_health_check_internal(future_window=check_window)
        fail_items = [(desc, st) for desc, st in issues if st == "FAIL"]
        if fail_items:
            fail_detail = "\n".join(f"  [FAIL] {desc}" for desc, _ in fail_items[:20])
            self._ui_progress_append(
                f"\n[FAIL] 自动体检发现 {len(fail_items)} 项阻止条件：\n{fail_detail}\n",
                clear=True,
            )
            messagebox.showerror("整本生成被安全阻止", "自动体检仍有硬错误，详情已显示在进度页。")
            return
        ledger_gap = self._structured_state_gap()
        if not self.config.get("unattended_cost_confirmed", False):
            if not messagebox.askyesno(
                "确认无人值守生成",
                f"将从第 {next_chap} 章写到第 {target_end} 章，共 {count} 章。\n\n"
                + (f"另将自动补齐旧稿的 {ledger_gap} 章证据记忆。\n\n" if ledger_gap else "")
                + f"预计约 {count * 4}-{count * 8} 次模型调用。\n"
                + (
                    f"DeepSeek 本地累计估算费用达到 ¥{self._configured_cost_limit_cny():.2f} 后自动止损。"
                    if self.get_model_provider() == PROVIDER_DEEPSEEK
                    else "Codex 套餐调用本地费用记为 ¥0；总调用和单章调用次数保险丝照常生效。"
                )
                + "确认后本书不再重复询问。是否开始？",
            ):
                return
            self._save_project_config_patch(unattended_cost_confirmed=True)
        max_per_chapter = max(4, min(20, int(self.config.get("max_model_calls_per_chapter", 16) or 16)))
        migration_calls = ledger_gap * max(
            1,
            int(self.config.get("state_delta_max_attempts", 2) or 2)
            * (
                2
                if (
                    self.config.get("state_delta_independent_audit", True)
                    or self.config.get("pre_save_continuity_audit_enabled", True)
                )
                else 1
            ),
        )
        with self._model_call_lock:
            self._model_call_count = 0
            self._model_call_limit = count * max_per_chapter + migration_calls
            self._chapter_model_call_count = 0
            self._chapter_model_call_limit = 0
            self._chapter_model_call_number = 0
        self._update_model_usage_label()
        self._ui_progress_append(
            f"[2/2] 自动体检通过。\n"
            f"即将从第 {next_chap} 章写到第 {target_end} 章，共 {count} 章。\n"
            + (f"先自动补齐 {ledger_gap} 章旧稿证据记忆。\n" if ledger_gap else "")
            + f"预计 {count * 4}-{count * 8 + migration_calls} 次模型调用；硬上限 {self._model_call_limit} 次。\n"
            + (
                f"本书 DeepSeek 累计估算费用止损：¥{self._configured_cost_limit_cny():.2f}。\n\n"
                if self.get_model_provider() == PROVIDER_DEEPSEEK
                else "Codex 套餐调用：本地费用估算 ¥0；token 与调用次数仍记账。\n\n"
            ),
            clear=True,
        )
        trial_run = bool(self._trial_mode_pending() and target_end == trial_stop)
        self._start_batch_job(
            count,
            label=(f"第1-{trial_stop}章试写" if trial_run else "一键写完整本"),
            target_end=target_end,
            mode=("trial" if trial_run else "full_book"),
        )

    def batch_generate_full_book(self):
        self.one_click_generate_full_book()

    def _start_batch_job(self, count, label="批量生成", target_end=None, resume=False, mode=None):
        runner = getattr(self, "_book_runner", None)
        if runner is None:
            runner = book_runner.BookRunner(self)
            self._book_runner = runner
        return runner.start(
            count,
            label=label,
            target_end=target_end,
            resume=resume,
            mode=mode,
        )

    def _batch_worker_entry(self, total_count):
        """Compatibility entrypoint; new jobs use :class:`book_runner.BookRunner`."""
        runner = getattr(self, "_book_runner", None)
        if runner is None:
            runner = book_runner.BookRunner(self)
            self._book_runner = runner
        return runner._worker_entry(total_count)

    def batch_generate(self):
        # Feature 1: 批量前自动健康检查
        issues = self._run_health_check_internal()
        fail_items = [(desc, st) for desc, st in issues if st == "FAIL"]
        if fail_items:
            fail_detail = "\n".join(f"  [FAIL] {desc}" for desc, _ in fail_items)
            messagebox.showerror(
                "批量生成被阻止",
                f"健康检查发现 {len(fail_items)} 项 FAIL，必须先修复才能启动批量生成：\n\n{fail_detail}"
            )
            return

        count = simpledialog.askinteger("批量生成", "请输入要自动生成的章节数量：", minvalue=1, maxvalue=100)
        if not count:
            return

        _, next_chap, _, _, _ = generator.get_latest_chapter_info()
        planned_end = self._get_story_planned_end_chapter()
        if planned_end and next_chap > planned_end:
            messagebox.showinfo("批量生成已到终点", f"当前规划终章是第 {planned_end} 章，已无后续卷规划。")
            return
        if planned_end and next_chap + count - 1 > planned_end:
            adjusted_count = planned_end - next_chap + 1
            messagebox.showinfo(
                "批量数量已调整",
                f"当前规划终章是第 {planned_end} 章，本次将只生成第 {next_chap}-{planned_end} 章，共 {adjusted_count} 章。"
            )
            count = adjusted_count

        if count > int(self.config.get("health_check_future_chapters", 30) or 30):
            issues = self._run_health_check_internal(future_window=count)
            fail_items = [(desc, st) for desc, st in issues if st == "FAIL"]
            if fail_items:
                fail_detail = "\n".join(f"  [FAIL] {desc}" for desc, _ in fail_items)
                messagebox.showerror(
                    "批量生成被阻止",
                    f"本次计划生成 {count} 章，但托管窗口检查发现 {len(fail_items)} 项 FAIL：\n\n{fail_detail}"
                )
                return

        self._start_batch_job(count, label="批量生成")

    def batch_worker(self, total_count):
        try:
            recovered = self._recover_pending_chapter_commit()
            if recovered.get("status") == "RECOVERED":
                self._ui_progress_append(
                    f"\n[RECOVERED] 已自动完成第 {recovered.get('chapter')} 章上次中断的落盘事务。\n"
                )
        except Exception as exc:
            message = f"上次章节提交自动恢复失败：{str(exc)[:220]}"
            self._write_batch_state(
                status="paused",
                resume_allowed=False,
                stage="pending_commit_recovery_failed",
                message=message,
            )
            self._append_batch_audit({
                "event": "pending_commit_recovery_failed",
                "error": str(exc)[:800],
            })
            self._ui_progress_append(f"\n[PAUSE] {message}\n")
            self.is_batch_running = False
            self._ui(lambda: self.enable_buttons(True))
            self._ui(lambda: self.btn_stop.config(state=tk.DISABLED))
            return
        try:
            self._run_provider_preflight()
        except Exception as exc:
            try:
                provider_name = self._provider_display_name()
            except Exception:
                provider_name = "模型提供方"
            message = f"{provider_name} 预检失败：{str(exc)[:220]}"
            self._write_batch_state(
                status="paused",
                resume_allowed=False,
                stage="provider_preflight_failed",
                message=message,
            )
            self._append_batch_audit({
                "event": "provider_preflight_failed",
                "provider": str(self.config.get("model_provider") or ""),
                "error": str(exc)[:800],
            })
            self._ui_progress_append(f"\n[PAUSE] {message}\n")
            self.is_batch_running = False
            self._ui(lambda: self.enable_buttons(True))
            self._ui(lambda: self.btn_stop.config(state=tk.DISABLED))
            return
        provider = self.get_model_provider()

        batch_end_message = " 批量生成完毕！"
        skipped_chapters = []
        fatal_api_stop_msg = ""
        job_state = self._load_batch_state()
        planned_target_end = int(job_state.get("target_end") or 0)
        consecutive_review_count = self._count_consecutive_review_chapters()
        ending_markers = [
            "（全书完）", "（完）", "全书完", "全剧终", "—— 全文完 ——",
            "（大结局）", "the end", "【完结】", "（全文完）",
        ]

        # Old books are migrated automatically before any new chapter sees an
        # incomplete memory ledger.  A failed migration pauses safely.
        try:
            _, _, _, ledger_latest_chapter, _ = generator.get_latest_chapter_info()
            self._catch_up_structured_state(ledger_latest_chapter)
        except Exception as exc:
            total_count = 0
            batch_end_message = (
                f"[PAUSE] 长期记忆补齐失败：{str(exc)[:220]}。"
                "没有生成下一章，也没有发布未经记账的新内容。"
            )
            self._write_batch_state(
                status="paused",
                resume_allowed=False,
                stage="state_ledger_catchup_failed",
                message=batch_end_message,
            )
            self._append_batch_audit({
                "event": "state_ledger_catchup_failed",
                "error": str(exc)[:800],
            })
            self._ui_progress_append(f"\n  {batch_end_message}\n")

        def _review_draft_path(path):
            root, ext = os.path.splitext(path)
            return f"{root}_待审{ext or '.txt'}"

        def _save_review_draft(path, content):
            draft_path = _review_draft_path(path)
            with open(draft_path, "w", encoding="utf-8") as f:
                f.write(content or "")
            try:
                draft_chap = self._extract_chap_num_from_path(draft_path)
                draft_chars = len(re.findall(r'[\u4e00-\u9fff]', content or ""))
                self._record_chapter_status(draft_chap, "待审暂停", ["已保存待审草稿，托管暂停"], draft_path, draft_chars)
            except Exception:
                pass
            return draft_path

        for i in range(total_count):
            if self._stop_event.is_set():
                self._ui_progress_append(f"\n\n[STOP] 已手动停止，共完成 {i} 章。\n")
                batch_end_message = "[PAUSE] 批量生成已停止。"
                break

            # 刷新章节信息
            self.current_vol, self.next_chap, self.filepath, self.latest_chap, self.latest_filepath = generator.get_latest_chapter_info()
            chap_num = self.next_chap
            self._refresh_live_progress_label()
            self._begin_chapter_model_budget(chap_num)
            # Bug3: 锁定本轮存盘路径
            chapter_filepath = self.filepath

            vol_switch_msg = self._auto_switch_volume_outline(chap_num)
            if vol_switch_msg:
                # P1修复: 跨卷首章重新获取路径
                self.current_vol, self.next_chap, self.filepath, self.latest_chap, self.latest_filepath = generator.get_latest_chapter_info()
                chapter_filepath = self.filepath

            self._ui_clear()
            self._ui_progress_append(f" 批量模式 [{i + 1}/{total_count}] — 正在生成第 {chap_num} 章...\n")
            self._write_batch_state(
                status="running",
                current_chapter=chap_num,
                current_index=i + 1,
                completed_count=i,
                stage="preparing",
                message=f"准备生成第{chap_num}章",
            )
            self._append_batch_audit({"event": "chapter_start", "chapter": chap_num, "index": i + 1})

            # 读取上一章内容
            prev_content = ""
            if self.latest_filepath and os.path.exists(self.latest_filepath):
                raw = generator.read_text_safe(self.latest_filepath)
                if "---" in raw:
                    raw = raw[:raw.rfind("---")].rstrip()
                prev_content = raw[-800:] if len(raw) > 800 else raw

            sys_prompt = self.build_system_prompt_gui(
                current_prompt=f"写第{chap_num}章内容。前情提要：{prev_content[-200:] if prev_content else ''}")

            # 提取细纲；开启自动模式时先滚动补足一批，避免写到中途断档。
            if self.config.get("auto_outline_from_volume", False):
                self._ensure_rolling_outline_batch(chap_num, prev_content)
            chapter_outline = self._extract_chapter_outline(chap_num)
            if not chapter_outline:
                if not self.config.get("auto_outline_from_volume", False):
                    self._ui_progress_append(f"  [PAUSE] 未找到第 {chap_num} 章逐章细纲，且自动补细纲未开启。\n")
                    skipped_chapters.append(chap_num)
                    batch_end_message = f"[PAUSE] 批量生成已暂停：第 {chap_num} 章缺少逐章细纲。"
                    break
                chapter_outline = self._auto_generate_outline(chap_num, prev_content)
                if not chapter_outline:
                    self._ui_progress_append(f"  [FAIL] 自动展开失败，跳过第{chap_num}章\n")
                    skipped_chapters.append(chap_num)
                    batch_end_message = f"[PAUSE] 批量生成已暂停：第 {chap_num} 章无法自动生成细纲。"
                    break
            outline_contract_guard = continuity_guard.validate_outline(
                chapter_outline,
                chap_num,
                chap_num,
                generator.DIRS["plot"],
                require_extended=("章节功能" in chapter_outline),
            )
            outline_guard = self._run_outline_reveal_guard(chapter_outline, chap_num)
            if outline_contract_guard["status"] == "FAIL":
                outline_guard = {
                    "status": "FAIL",
                    "summary": outline_contract_guard.get("summary", "细纲契约未通过"),
                }
            reveal_rules = self._load_reveal_rules()
            strict_reveal = bool(reveal_rules and reveal_rules.get("strict_mode", False))
            outline_rejected = (
                outline_guard["status"] == "FAIL"
                or (outline_guard["status"] == "WARN" and strict_reveal)
            )
            if outline_rejected:
                self._ui_progress_append(
                    f"  [WARN] 第 {chap_num} 章细纲触发护栏，正在自动重建细纲窗口。\n"
                    f"     {outline_guard['summary']}\n"
                )
                repaired_outline = self._regenerate_rejected_outline(
                    chap_num, prev_content, outline_guard.get("summary", "")
                )
                if repaired_outline:
                    chapter_outline = repaired_outline
                    outline_contract_guard = continuity_guard.validate_outline(
                        chapter_outline,
                        chap_num,
                        chap_num,
                        generator.DIRS["plot"],
                        require_extended=("章节功能" in chapter_outline),
                    )
                    outline_guard = self._run_outline_reveal_guard(chapter_outline, chap_num)
                    if outline_contract_guard["status"] == "FAIL" or outline_guard["status"] != "PASS":
                        repaired_outline = ""
                if repaired_outline:
                    self._ui_progress_append(f"    [OK] 第 {chap_num} 章细纲已自动修复。\n")
                else:
                    _save_review_draft(
                        chapter_filepath,
                        f"第{chap_num}章 [待重写]\n\n细纲自动重建两次仍未通过护栏。\n"
                    )
                    skipped_chapters.append(chap_num)
                    batch_end_message = f"[PAUSE] 批量生成已暂停：第 {chap_num} 章细纲自动修复失败。"
                    break
            elif outline_guard["status"] == "WARN":
                self._ui_progress_append(
                    f"  [WARN] 第 {chap_num} 章逐章细纲存在真相预警，继续生成。\n"
                    f"    {outline_guard['summary']}\n"
                )
            outline_block = ""
            if chapter_outline:
                outline_block = (
                    f"\n\n══════════════════════════════════════\n"
                    f"【本章细纲（必须严格遵循）】\n"
                    f"══════════════════════════════════════\n"
                    f"{chapter_outline}\n"
                    f"══════════════════════════════════════\n"
                    f"【硬性执行规则】：\n"
                    f"1. 细纲中的核心事件必须逐条体现在正文中\n"
                    f"2. 细纲中提到的角色名必须在正文中出现\n"
                    f"3. 按细纲的结尾类型在正文最后15%自然落地；可以是问题、代价、关系变化、兑现、反差、危机或余震，不得硬塞无因果突发事件\n"
                    f"4. 禁止自创细纲中没有的新角色/势力/设定\n"
                    f"5. 正史状态、事实库和已发生正文高于细纲；细纲有冲突时保留剧情目标，但不得写入错误事实\n"
                    f"6. 只执行已经通过正史检查的细纲内容，不自行新增角色、物品、情报来源或能力\n"
                    f"7. 出场人物字段及姓名后的括号身份是硬角色约束；不得把同名角色改成拳手、医生、警察等其他职业，也不得把仅提及的人写成现场行动者\n"
                )

            # 收集已有标题防重复
            all_titles = []
            out_dir = generator.DIRS["out"]
            for vol_dir_name in sorted(os.listdir(out_dir)):
                vol_path = os.path.join(out_dir, vol_dir_name)
                if os.path.isdir(vol_path):
                    for fname in sorted(os.listdir(vol_path)):
                        if fname.endswith(".txt") and not fname.startswith("."):
                            try:
                                with open(os.path.join(vol_path, fname), "r", encoding="utf-8", errors="ignore") as tf:
                                    first_line = tf.readline().strip().lstrip("#").strip()
                                    title_match = re.sub(r'^第\d+章\s*', '', first_line)
                                    if title_match:
                                        all_titles.append(title_match)
                            except Exception:
                                pass

            recent_titles = all_titles[-20:]
            title_hint = f"\n【近期已用章节名（本章标题绝对禁止重复或高度相似）】：\n" + "\n".join(recent_titles) + "\n" if recent_titles else ""
            all_titles_set = set(all_titles)
            char_limits = self._get_chapter_char_limits()
            try:
                structured_manifest = self._compile_structured_chapter_context(
                    chap_num, chapter_outline, prev_content
                )
                structured_context = structured_manifest.get("context_text", "")
            except Exception as exc:
                structured_manifest = {}
                structured_context = ""
                if self.config.get("structured_state_enabled", True):
                    skipped_chapters.append(chap_num)
                    batch_end_message = (
                        f"[PAUSE] 第 {chap_num} 章长期正史上下文编译失败：{str(exc)[:180]}。"
                        "为避免在缺失记忆时续写，托管已暂停。"
                    )
                    self._append_batch_audit({
                        "event": "chapter_context_compile_failed",
                        "chapter": chap_num,
                        "error": str(exc)[:500],
                    })
                    self._ui_progress_append(f"  {batch_end_message}\n")
                    break
            # The system prompt already carries timeline, foreshadowing and
            # stage-contract sources. Keep only recent chapter facts here to
            # avoid paying for the same context twice on every chapter.
            fact_context = knowledge_manager.build_generation_context(
                generator.DIRS["plot"], chap_num, max_chars=1600, include_sources=False
            )
            canon_context = ""
            if self.config.get("canon_guard_enabled", True):
                canon_context = continuity_guard.build_canon_context(generator.DIRS["plot"], max_chars=2200)
            fact_block = ""
            if fact_context:
                fact_block = (
                    "\n\n【关键事实库（必须遵守，防止跨章遗忘）】\n"
                    f"{fact_context}\n"
                    "【执行规则】：事实库中已完成、已死亡、已离场、已持有、已知道/未知的状态不得写反；"
                    "如果事实库与本章细纲冲突，以正史和事实库为准；只能保留细纲的剧情目标，"
                    "不得靠一句补叙、临时回忆或新设定强行圆错。\n"
                )
            canon_block = ""
            if canon_context:
                canon_block = (
                    "\n\n" + canon_context + "\n"
                    "【正史执行规则】：正史状态高于滚动细纲和自动摘要；发现冲突时修正细纲表达，"
                    "不得覆盖正史、不得让主角境界和伤势回档、不得重演已完成事件。\n"
                )
            story_block = ""
            if structured_context:
                story_block = (
                    "\n\n" + structured_context + "\n"
                    "【账本执行规则】：以上内容来自已保存正文的逐字证据校验。角色知情边界、"
                    "资源状态、未结伏笔和已解决事项均为硬约束；大纲与其冲突时，只保留剧情目标，"
                    "不得编造补丁。正文若要改变任一状态，必须在本章用可见行动、对话或文件明确写出变化依据。\n"
                )

            plan_prompt = f"""你现在要写第 {chap_num} 章。

请根据本章细纲、正史状态、关键事实库和上一章结尾，直接输出本章的小说正文。

格式要求（极重要）：
- 第一行必须是章节标题，格式为"第{chap_num}章 标题"（标题简洁明了4-8字）
- 标题后空一行再写正文
- 禁止使用任何Markdown格式，不要在标题前加#号
{title_hint}
内容要求：
1. 紧接上一章的剧情自然展开，不要重复已经发生的事。
2. 【反重复铁律】：凡是已经发生过的事件，本章绝对禁止再写一遍！
3. 完成细纲指定的章节功能：推进主线、兑现前文、改变关系、支付代价或完成必要铺垫至少一项，并按结尾类型形成具体的下一页期待。
4. 细纲是本章剧情目标；正史状态、事实库和已经发生的正文拥有更高优先级。
5. 本章正文中文字数目标在{char_limits['target_min']}-{char_limits['target_max']}字之间，硬下限为{char_limits['min']}字。
6. 【严禁回绕】：写完就停，绝对不要重复前面已经写过的段落或场景！
7. 【反AI腔】不要把一句完整意思拆成多行短句；章尾悬念最多保留1-2个短锤句，不要整段都写成"他知道。她看见。它在等。"。
8. 【反诗化堆词】"像是/仿佛/似乎"全文合计最多4次；少用"某种""不是……是……"和破折号——来硬造神秘感，优先用动作、细节、场景推进压迫感。
9. 【反幻觉·最高优先级】严禁引用前文中没有出现过的角色、对话、实体或事件。如果你不确定某事是否已经发生，就不要提它。严禁编造"某角色说过XXX""之前发现了XXX"等虚假回忆。只使用上一章结尾片段和细纲中明确给出的信息。
10. 【输出前静默核对】不要输出检查过程，但必须逐项确认：时间是否顺推；地点移动是否有路程；伤势是否允许当前动作；关键物品、证物和资源是否确实持有且未耗尽；角色知道信息是否有现场、对话、文件或目击来源；推测是否被误写成客观事实；人物能力是否符合当前项目设定。
11. 【能力与信息边界·最高优先级】角色只能使用已经取得的知识、能力、权限、物品和资源；不得凭空新增技能、机制、组织、证据或无代价解法。项目设定中的限制、交换与代价必须完整保留。
12. 【番茄发布规则】不得出现低俗色情、未成年人负面导向、极端血腥细节、攻击引战、人肉网暴、站外导流、博彩或违规盈利；不得用重复段落、无关信息、改名换皮和空洞议论恶意水文。虚构灾难、悬疑和反派行为本身不等于违规，按具体描写和导向判断。
13. 【商业节奏】黄金三章、冲突推进、调查发现、兑现回收和高潮决断章，开篇300字内应出现人物目标、异常或外部压力之一，核心事件须在正文前60%开始实质发生；关系转折、代价余波和铺垫蓄力章可自然放宽，但前25%必须明确承接对象和本章变化方向。专业流程只保留能产生新证据、冲突或选择的动作，同一结论不得换说法重复证明。
14. 【触发顺序】若细纲指定能力、异常或关键发现由某个具体动作首次触发，那么该动作之前不得在其他部位、物品或回忆中提前出现同类感知、能力预告或核心联想。
15. 【落点可见】细纲中的时间锚点、状态落点、不可逆变化和章末钩子都要在正文中用可定位的动作、对白或文件落地；允许自然改写，不要求逐字照抄，但不能只靠读者推断。
16. 【禁作者层回指】标题之外不得写“第X章、本章、全章、上一章/下一章”等创作层编号；回忆旧事必须改用具体事件、时间或地点锚点。
17. 【职责动作闭环】同章建立的职责、权限、伤势和动作限制必须与后续动作一致；被限定只能观察或下令的人，不得无解释亲自执行已经交给他人的动作。
{outline_block}

【上一章结尾片段（请衔接）】：
{prev_content if prev_content else "（这是第一章，无上文）"}
{fact_block}{canon_block}{story_block}"""

            # 托管策略：初稿 + 多轮带诊断自修复；仍失败时按 L2/L3 策略决定继续或暂停。
            max_retries = max(1, int(self.config.get("auto_repair_max_attempts", repair_engine.DEFAULT_MAX_REPAIR_ATTEMPTS) or repair_engine.DEFAULT_MAX_REPAIR_ATTEMPTS))
            retry_count = 0
            api_retry_count = 0
            chapter_content = ""
            paused_failure_content = ""
            success = False
            should_pause_for_review = False
            fatal_api_stop_msg = ""
            repair_notes = []
            last_failure_reason = ""
            last_failed_content = ""
            chinese_chars = 0
            chapter_status = "正式可用"
            chapter_review_notes = []
            review_streak_limit_reached = False
            commercial_review_pause_after_save = False
            commercial_review_pause_reason = ""
            commercial_review_result = None
            bind_commercial_review = False
            narrative_audit_result = None
            prepared_state_delta = None
            chapter_validation_receipt = None

            def _retry_or_block(reason, content):
                nonlocal retry_count, should_pause_for_review, paused_failure_content
                nonlocal last_failure_reason, last_failed_content, success, chapter_status
                nonlocal chapter_content, chinese_chars, chapter_validation_receipt
                if self._stop_event.is_set():
                    return False
                last_failure_reason = reason
                last_failed_content = content or chapter_content or ""
                chapter_validation_receipt = None
                reveal_rules = self._load_reveal_rules()
                strict_reveal = bool(reveal_rules and reveal_rules.get("strict_mode", False))
                if (
                    retry_count < max_retries - 1
                    and not self._stop_event.is_set()
                    and repair_engine.should_rewrite(
                        reason,
                        last_failed_content,
                        char_limits=char_limits,
                        strict_reveal=strict_reveal,
                    )
                ):
                    repair_notes.append(repair_engine.format_repair_note(reason, retry_count + 1))
                    retry_count += 1
                    self._write_batch_state(
                        status="running",
                        current_chapter=chap_num,
                        stage="auto_rewrite",
                        attempt=retry_count + 1,
                        message=reason[:300],
                    )
                    self._append_batch_audit({
                        "event": "auto_rewrite",
                        "chapter": chap_num,
                        "attempt": retry_count + 1,
                        "reason": reason[:800],
                    })
                    self._ui_progress_append(
                        f"  [RETRY] {reason[:120]}，自动重写第 {retry_count + 1}/{max_retries} 版...\n"
                    )
                    return True

                decision = repair_engine.decide_after_retries(
                    reason,
                    last_failed_content,
                    char_limits=char_limits,
                    strict_reveal=strict_reveal,
                    min_salvage_chars=int(self.config.get("min_review_continue_chars", repair_engine.DEFAULT_MIN_SALVAGE_CHARS) or repair_engine.DEFAULT_MIN_SALVAGE_CHARS),
                )
                if decision["action"] == "continue_review":
                    chapter_content = last_failed_content
                    chinese_chars = repair_engine.chinese_char_count(chapter_content)
                    chapter_status = decision["status"]
                    note = f"{decision['level']}降级继续：{reason[:500]}"
                    chapter_review_notes.append(note)
                    should_pause_for_review = False
                    paused_failure_content = ""
                    retry_count = max_retries
                    success = True
                    self._write_batch_state(
                        status="running",
                        current_chapter=chap_num,
                        stage="review_continue",
                        message=note[:300],
                    )
                    self._append_batch_audit({
                        "event": "auto_repair_giveup_continue",
                        "chapter": chap_num,
                        "category": decision.get("category"),
                        "reason": reason[:800],
                        "chars": chinese_chars,
                    })
                    self._ui_progress_append(
                        f"  [WARN] 自修复已达上限，降级为“{chapter_status}”并继续托管：{decision['message']}\n"
                    )
                    return False

                should_pause_for_review = True
                paused_failure_content = last_failed_content
                retry_count = max_retries
                return False

            def _hard_retry_or_pause(reason, content):
                """Retry a hard gate, but never downgrade its final FAIL to official."""
                nonlocal success, should_pause_for_review, paused_failure_content
                nonlocal retry_count, chapter_status
                _retry_or_block(reason, content)
                if success:
                    success = False
                    should_pause_for_review = True
                    paused_failure_content = content
                    retry_count = max_retries
                    chapter_status = "待审暂停"

            def _pause_without_retry(reason, content):
                """Keep a structurally valid candidate as draft when a stage gate fails."""
                nonlocal success, should_pause_for_review, paused_failure_content
                nonlocal retry_count, chapter_status, last_failure_reason, last_failed_content
                last_failure_reason = reason
                last_failed_content = content or chapter_content or ""
                success = False
                should_pause_for_review = True
                paused_failure_content = last_failed_content
                retry_count = max_retries
                chapter_status = "待审暂停"

            while retry_count < max_retries and not success and not self._stop_event.is_set():
                try:
                    prepared_state_delta = None
                    narrative_audit_result = None
                    chapter_validation_receipt = None
                    self._ui_clear()
                    attempt_prompt = plan_prompt
                    if repair_notes:
                        attempt_prompt = repair_engine.build_repair_prompt(
                            plan_prompt,
                            repair_notes,
                            failed_content=last_failed_content or chapter_content,
                            chapter_outline=chapter_outline,
                            prev_content=prev_content,
                            char_limits=char_limits,
                            chap_num=chap_num,
                        )
                    self._write_batch_state(
                        status="running",
                        current_chapter=chap_num,
                        stage="generating",
                        attempt=retry_count + 1,
                        message=f"生成第{chap_num}章第{retry_count + 1}版",
                    )
                    sys_prompt_run, plan_prompt_run, prompt_stats = self._apply_generation_prompt_budget(
                        sys_prompt, attempt_prompt
                    )
                    if retry_count == 0:
                        self._ui_progress_append(
                            f"   输入估算：{prompt_stats['final']['input_tokens']} tokens"
                            f"（system {prompt_stats['final']['system_chars']} chars / "
                            f"user {prompt_stats['final']['user_chars']} chars）\n"
                        )
                    elif prompt_stats["trimmed"]:
                        self._ui_progress_append(
                            f"   重试前裁剪上下文：{prompt_stats['original']['input_tokens']} -> "
                            f"{prompt_stats['final']['input_tokens']} tokens\n"
                        )
                    state_only_candidate = (
                        self._load_state_only_candidate(chap_num, _review_draft_path(chapter_filepath))
                        if retry_count == 0 else None
                    )
                    if state_only_candidate is not None:
                        chapter_content = state_only_candidate
                        self._ui_progress_append("   复用状态审计失败时保留的原正文，重新核验，不重新生成。\n")
                    else:
                        self._before_model_call()
                        model_name = self.get_model_name()
                        chapter_content = self._call_generation_text(
                            sys_prompt_run,
                            plan_prompt_run,
                            model_name=model_name,
                            token_limit=int(self.config.get("max_tokens", 8192) or 8192),
                            task_type="batch_chapter_generation",
                            on_chunk=self._ui_append,
                        )

                    if self._stop_event.is_set():
                        # A stopped stream is intentionally discarded. It must
                        # never reach quality checks or be saved as a chapter.
                        chapter_content = ""
                        self._ui_progress_append(
                            "\n  [PAUSE] 已中断当前流式响应，不保存不完整正文。\n"
                        )
                        break

                    api_retry_count = 0

                    chapter_content = self.strip_markdown_artifacts(chapter_content)

                    # Markdown 标题清理 (Section 11.2)
                    lines = chapter_content.split("\n", 1)
                    if lines and lines[0].strip().startswith("#"):
                        lines[0] = lines[0].strip().lstrip("#").strip()
                        chapter_content = "\n".join(lines)

                    chapter_content, title_fixed = self._ensure_chapter_title(chapter_content, chap_num, chapter_outline)
                    if title_fixed:
                        self._ui_progress_append(f"   已本地补齐/规范章节标题\n")
                    chapter_content, reveal_sanitized = self._sanitize_early_reveal_terms(chapter_content, chap_num)
                    if reveal_sanitized:
                        self._ui_progress_append(f"   已本地净化早期真相术语：{', '.join(reveal_sanitized)}\n")
                    chapter_content, chapter_ref_repairs = (
                        self._sanitize_author_layer_chapter_references(
                            chapter_content
                        )
                    )
                    if chapter_ref_repairs:
                        self._append_batch_audit({
                            "event": "author_layer_reference_sanitized",
                            "chapter": chap_num,
                            "repairs": chapter_ref_repairs,
                        })
                        self._ui_progress_append(
                            "   已本地改写正文中的作者层章节回指，无需再次调用模型\n"
                        )
                    if planned_target_end and chap_num < planned_target_end:
                        tail_start = max(0, len(chapter_content) - 260)
                        prefix, tail = chapter_content[:tail_start], chapter_content[tail_start:]
                        removed_markers = [
                            marker for marker in ending_markers
                            if marker.lower() in tail.lower()
                        ]
                        if removed_markers:
                            for marker in removed_markers:
                                tail = re.sub(re.escape(marker), "", tail, flags=re.IGNORECASE)
                            chapter_content = (prefix + tail).rstrip()
                            self._append_batch_audit({
                                "event": "early_ending_marker_removed",
                                "chapter": chap_num,
                                "markers": removed_markers,
                            })
                            self._ui_progress_append("   已移除规划终章前的误完结标记\n")

                    # 重复结构硬拦截：第二个章节标题/大段重复必须返修或转待审，
                    # 不能静默截断后写入正式稿，避免两版稿拼接污染后续上下文。
                    repeat_issues = self._detect_chapter_repetition(chapter_content, chap_num)
                    if repeat_issues:
                        reason = "重复结构检测未通过：" + "；".join(repeat_issues[:3])
                        self._ui_progress_append(f"  [RETRY] {reason}\n")
                        _hard_retry_or_pause(reason, chapter_content)
                        continue

                    # 托管模式不做“续写式扩写”。短章直接进入诊断重写，避免拼接稿结构松散。
                    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', chapter_content))

                    # 超长稿常见于两版拼接。机械截断会丢失证据闭环和章末钩子，
                    # 因此必须整章重写并在耗尽重试后保持待审。
                    long_limit = int(char_limits["target_max"])
                    if chinese_chars > long_limit:
                        reason = (
                            f"字数过长（{chinese_chars}字），超过目标上限{long_limit}字，"
                            "疑似重复或多版拼接"
                        )
                        self._ui_progress_append(f"  [RETRY] {reason}\n")
                        _hard_retry_or_pause(reason, chapter_content)
                        continue

                    # 字数硬下限：不足则进入自修复或 L2/L3 策略处理。
                    if chinese_chars < char_limits["min"]:
                        reason = f"字数不足（{chinese_chars}字），低于{char_limits['min']}字硬下限"
                        self._ui_progress_append(f"  [WARN] {reason}\n")
                        if repair_engine.should_rewrite(reason, chapter_content, char_limits=char_limits):
                            _retry_or_block(reason, chapter_content)
                            continue
                        chapter_status = "可继续但需复核"
                        chapter_review_notes.append(reason)
                        self._append_batch_audit({
                            "event": "short_chapter_review_continue",
                            "chapter": chap_num,
                            "chars": chinese_chars,
                            "reason": reason,
                        })

                    # 高频模糊类比是可精确计数的机械指标，应在交给 LLM
                    # 质检前先拦截，避免审稿器漏报，或把清晰的计数问题埋在
                    # 一长串细纲意见之后导致下一版只修了第一项。
                    simile_words = ("像是", "仿佛", "似乎")
                    simile_count = sum(chapter_content.count(word) for word in simile_words)
                    if simile_count > 4:
                        reason = (
                            f"反AI腔机械检查未通过：程序实测‘像是/仿佛/似乎’合计{simile_count}次，"
                            "允许上限为4次。必须将超出的表达改成直接动作、事实或具体感官描写，"
                            "并在输出前逐字复数。"
                        )
                        self._ui_progress_append(f"  [RETRY] {reason}\n")
                        _hard_retry_or_pause(reason, chapter_content)
                        continue

                    # 内容漂移防火墙：命中禁词/格式残留后进入自修复或 L2/L3 策略处理。
                    drift_hits = self._check_content_drift(chapter_content, chap_num=chap_num)
                    if drift_hits:
                        reason = f"漂移防火墙命中禁词/格式残留：{drift_hits}"
                        self._ui_progress_append(f"   {reason}\n")
                        _hard_retry_or_pause(reason, chapter_content)
                        continue

                    # 标题去重：重复则进入自修复或 L2/L3 策略处理。
                    first_line = chapter_content.strip().split("\n")[0].lstrip("#").strip()
                    new_title = re.sub(r'^第\d+章\s*', '', first_line)
                    if new_title and new_title in all_titles_set:
                        reason = f"标题重复：{new_title}"
                        self._ui_progress_append(f"  {reason}\n")
                        _hard_retry_or_pause(reason, chapter_content)
                        continue

                    candidate_report = chapter_validator.validate_candidate(
                        self.project_dir or os.path.dirname(generator.DIRS["plot"]),
                        chap_num,
                        chapter_content,
                        char_limits,
                    )
                    if not candidate_report.get("passed"):
                        validator_messages = "；".join(
                            str(item.get("message") or item.get("code") or "")
                            for item in (candidate_report.get("issues") or [])[:8]
                        )
                        reason = "最终正文确定性验收未通过：" + validator_messages
                        self._ui_progress_append(f"  [FAIL] {reason[:320]}\n")
                        _hard_retry_or_pause(reason, chapter_content)
                        continue
                    chapter_validation_receipt = (
                        chapter_validator.build_validation_receipt(candidate_report)
                    )

                    # 项目真相的本地语义硬门每章都扫描全量正式稿和当前候选。
                    # 它不依赖模型给分，可直接识别身份、死因、数值与元话语冲突。
                    semantic_report = self._run_semantic_consistency_guard(
                        chapter_content, chap_num
                    )
                    if not semantic_report.get("passed"):
                        reason = (
                            "语义一致性硬门未通过："
                            + self._semantic_guard_message(semantic_report)
                        )
                        self._ui_progress_append(f"  [FAIL] {reason[:320]}\n")
                        _hard_retry_or_pause(reason, chapter_content)
                        continue

                    # 正史连续性硬检查：不能以“需复核”降级进入后续上下文。
                    if self.config.get("canon_guard_enabled", True):
                        canon_check = continuity_guard.validate_chapter(
                            chapter_content,
                            chap_num,
                            generator.DIRS["plot"],
                        )
                        if canon_check["status"] == "FAIL":
                            reason = f"正史连续性未通过：{canon_check['summary']}"
                            self._ui_progress_append(f"  [FAIL] {reason}\n")
                            _hard_retry_or_pause(reason, chapter_content)
                            continue

                    # 标题章节号校验
                    title_chap_match = re.match(r'第(\d+)章', first_line)
                    if title_chap_match and int(title_chap_match.group(1)) != chap_num:
                        chapter_content = re.sub(r'^第\d+章', f'第{chap_num}章', chapter_content, count=1)

                    # 旧版逐章模型质检只在严格模式运行。简单模式前面已经完成
                    # 字数、结构、重复、语义与正史等确定性硬检查，不再重复耗费模型。
                    if self._strict_release_mode():
                        self._ui_progress_append("   质检审核中...")
                        try:
                            gate_result = self._run_quality_gate(
                                chapter_content, chapter_outline, prev_content
                            )
                            gate_status = repair_engine.parse_gate_status(gate_result)
                            if gate_status == "FAIL":
                                reason = f"质检未通过：{gate_result[:1800]}"
                                self._ui_progress_append(
                                    f" FAIL！\n    {gate_result[:200]}...\n"
                                )
                                _hard_retry_or_pause(reason, chapter_content)
                                continue
                            if gate_status == "WARN":
                                chapter_status = "可继续但需复核"
                                chapter_review_notes.append(
                                    f"质检预警：{gate_result[:500]}"
                                )
                                self._ui_progress_append(
                                    " WARN（已标记需复核，不自动发布）\n"
                                )
                            else:
                                self._ui_progress_append(" PASS\n")
                        except Exception as e:
                            reason = f"质检服务调用失败：{str(e)[:500]}"
                            self._append_batch_audit({
                                "event": "quality_gate_unavailable",
                                "chapter": chap_num,
                                "error": str(e)[:800],
                            })
                            self._ui_progress_append(
                                f" FAIL（质检不可用，禁止形成正式章节）：{str(e)[:100]}\n"
                            )
                            _hard_retry_or_pause(reason, chapter_content)
                            continue

                    # ---- 发布前设定总校（FAIL 进入自修复或 L2/L3 策略处理）----
                    release_guard = self._run_release_guard(chapter_content, chap_num, prev_content=prev_content)
                    if release_guard["status"] == "FAIL":
                        reason = f"设定总校未通过：{release_guard['summary']}"
                        self._ui_progress_append(f"  [FAIL] 设定拦截：{release_guard['summary']}\n")
                        _hard_retry_or_pause(reason, chapter_content)
                        continue
                    if release_guard["status"] == "WARN":
                        if self._strict_release_mode():
                            chapter_status = "可继续但需复核"
                        chapter_review_notes.append(f"设定预警：{release_guard['summary']}")
                        self._ui_progress_append(f"  [WARN] 设定预警：{release_guard['summary']}\n")

                    # ---- 真相分级放行检查（FAIL 进入自修复或 L2/L3 策略处理）----
                    self._ui_progress_append(f"   真相节奏检查中...")
                    truth_guard = self._run_truth_reveal_guard(chapter_content, chap_num)
                    if truth_guard["status"] == "FAIL":
                        reason = f"真相越界：{truth_guard['summary']}"
                        self._ui_progress_append(f" FAIL！ {reason}\n")
                        _hard_retry_or_pause(reason, chapter_content)
                        continue
                    elif truth_guard["status"] == "WARN":
                        reveal_rules = self._load_reveal_rules()
                        if reveal_rules and reveal_rules.get("strict_mode", False):
                            reason = f"真相越界（严格模式）：{truth_guard['summary']}"
                            self._ui_progress_append(f" FAIL(严格模式)！{reason}\n")
                            _hard_retry_or_pause(reason, chapter_content)
                            continue
                        else:
                            if self._strict_release_mode():
                                chapter_status = "可继续但需复核"
                            chapter_review_notes.append(f"真相预警：{truth_guard['summary']}")
                            self._ui_progress_append(f" WARN（已放行）\n")
                    else:
                        self._ui_progress_append(f" PASS\n")

                    # ---- 逐章正文硬检 ----
                    # 简单发布模式只运行确定性检查；模型总编审稿留给
                    # 第3/10/30章等低频商业闸门。严格模式保留旧链路。
                    if self._strict_release_mode():
                        self._ui_progress_append("   主线与可读性独立硬审中...")
                        try:
                            narrative_audit_result = self._run_narrative_quality_guard(
                                chap_num,
                                chapter_content,
                                chapter_outline,
                            )
                            scores = narrative_audit_result.get("scores", {})
                            self._ui_progress_append(
                                " PASS"
                                f"（主线{scores.get('mainline_alignment', 0)} / "
                                f"可读性{scores.get('prose_validity', 0)}）\n"
                            )
                        except Exception as exc:
                            reason = f"主线与可读性硬审未通过：{str(exc)[:900]}"
                            self._append_batch_audit({
                                "event": "narrative_guard_rejected",
                                "chapter": chap_num,
                                "error": str(exc)[:1000],
                            })
                            self._ui_progress_append(f" FAIL\n    {reason[:260]}\n")
                            _hard_retry_or_pause(reason, chapter_content)
                            continue
                    else:
                        self._ui_progress_append("   正文确定性硬检中...")
                        local_issues = narrative_guard.deterministic_issues(
                            chapter_content
                        )
                        if local_issues:
                            reason = "正文确定性硬检未通过：" + "；".join(
                                local_issues[:8]
                            )
                            self._ui_progress_append(f" FAIL\n    {reason[:260]}\n")
                            _hard_retry_or_pause(reason, chapter_content)
                            continue
                        self._append_batch_audit({
                            "event": "simple_release_narrative_check",
                            "chapter": chap_num,
                            "status": "PASS",
                        })
                        self._ui_progress_append(" PASS\n")

                    # ---- 保存前证据正史审计 ----
                    # The same validated delta is committed after the atomic
                    # chapter save.  A contradiction must be repaired here,
                    # never after the chapter has become an official checkpoint.
                    if (
                        self._strict_release_mode()
                        and self.config.get("structured_state_enabled", True)
                    ):
                        self._ui_progress_append("   保存前证据正史审计中...")
                        try:
                            prepared_state_delta = self._prepare_structured_state_delta(
                                chap_num, chapter_content, chapter_outline,
                                **self._manual_chapter_state_repair_arguments(
                                    chap_num, chapter_content, chapter_outline
                                ),
                            )
                            self._ui_progress_append(" PASS\n")
                        except Exception as exc:
                            reason = f"保存前证据正史审计未通过：{str(exc)[:700]}"
                            self._ui_progress_append(f" FAIL\n    {reason[:240]}\n")
                            self._handle_state_preparation_failure(
                                chap_num, chapter_content, exc, reason,
                                _pause_without_retry, _hard_retry_or_pause,
                            )
                            continue

                    semantic_interval = max(1, int(self.config.get("semantic_arc_audit_interval", 10) or 10))
                    if (
                        self._strict_release_mode()
                        and self.config.get("semantic_arc_audit_enabled", True)
                        and chap_num % semantic_interval == 0
                    ):
                        self._ui_progress_append("   阶段语义审计中...")
                        semantic_audit = self._run_semantic_arc_audit(chapter_content, chap_num)
                        self._append_batch_audit({
                            "event": "semantic_arc_audit",
                            "chapter": chap_num,
                            "status": semantic_audit.get("status"),
                            "current": semantic_audit.get("current"),
                            "summary": semantic_audit.get("summary", "")[:800],
                        })
                        semantic_hard_gate = commercial_reviewer.is_hard_gate(
                            chap_num, self.config, self._get_volume_ranges()
                        )
                        if semantic_audit.get("current") == "FAIL":
                            reason = f"正史连续性语义审计未通过：{semantic_audit.get('summary', '')}"
                            self._ui_progress_append(f" FAIL\n    {reason[:220]}\n")
                            _hard_retry_or_pause(reason, chapter_content)
                            continue
                        if (
                            semantic_audit.get("status") == "FAIL"
                            or (
                                semantic_hard_gate
                                and semantic_audit.get("status") != "PASS"
                            )
                        ):
                            reason = f"历史语义问题尚未解决：{semantic_audit.get('summary', '')}"
                            self._ui_progress_append(f" FAIL\n    {reason[:220]}\n")
                            _pause_without_retry(reason, chapter_content)
                            continue
                        if semantic_audit.get("status") in {"WARN", "FAIL"}:
                            self._ui_progress_append(
                                f" WARN（历史或区间问题已记审计，不归因当前章）："
                                f"{semantic_audit.get('summary', '')[:160]}\n"
                            )
                        else:
                            self._ui_progress_append(" PASS\n")

                    # 低频阶段总编审稿：黄金三章、第10/30章和卷末才运行。
                    # 它判断整段是否值得继续扩大生产，不触发单章自动重写。
                    volume_ranges = self._get_volume_ranges()
                    if commercial_reviewer.should_run(
                        chap_num, self.config, volume_ranges
                    ):
                        self._ui_progress_append("   阶段商业审稿中...")
                        try:
                            commercial_review_result = self._run_commercial_stage_review(
                                chapter_content, chap_num, persist_report=False
                            )
                            commercial_status = commercial_review_result.get("status", "WARN")
                            commercial_action = commercial_review_result.get("action", "ADJUST")
                            commercial_summary = commercial_review_result.get("summary", "")
                            hard_stage_gate = commercial_reviewer.is_hard_gate(
                                chap_num, self.config, volume_ranges
                            )
                            commercial_severity = commercial_reviewer.classify_review_severity(
                                commercial_review_result,
                                hard_gate=hard_stage_gate and self._strict_release_mode(),
                            )
                            commercial_review_result["severity"] = commercial_severity
                            commercial_review_result["hard_gate"] = bool(hard_stage_gate)
                            debt_path = os.path.join(
                                generator.DIRS["plot"], "revision_debt.json"
                            )
                            debt_blocked = bool(
                                self._strict_release_mode()
                                and commercial_reviewer.hard_gate_debt_blocked(
                                    chap_num,
                                    commercial_review_result,
                                    commercial_reviewer.load_revision_debt(debt_path),
                                    self.config,
                                    volume_ranges,
                                )
                            )
                            commercial_review_result["gate_blocked"] = bool(debt_blocked)
                            if debt_blocked:
                                commercial_review_result["gate_block_reason"] = (
                                    "硬闸门仍有上一阶段修订债务未取得正文证据核销"
                                )
                            self._append_batch_audit({
                                "event": "commercial_stage_review",
                                "chapter": chap_num,
                                "status": commercial_status,
                                "current": commercial_review_result.get("current"),
                                "action": commercial_action,
                                "severity": commercial_severity,
                                "summary": commercial_summary[:800],
                                "gate_blocked": bool(debt_blocked),
                                "report": commercial_review_result.get("report_path", ""),
                            })
                            if commercial_status in {"WARN", "FAIL"} or commercial_action in {"ADJUST", "PAUSE"}:
                                if self._strict_release_mode():
                                    chapter_status = "可继续但需复核"
                                chapter_review_notes.append(
                                    f"阶段商业审稿{commercial_status}/{commercial_action}：{commercial_summary[:500]}"
                                )
                            if debt_blocked:
                                debt_reason = commercial_review_result["gate_block_reason"]
                                chapter_review_notes.append(debt_reason)
                                self._ui_progress_append(
                                    f" BLOCKED（{debt_reason}，候选章仅保存待审草稿）\n"
                                )
                                _pause_without_retry(debt_reason, chapter_content)
                                continue
                            elif (
                                self._strict_release_mode()
                                and commercial_reviewer.should_pause_after_review(
                                    chap_num,
                                    commercial_status,
                                    commercial_action,
                                    self.config,
                                    volume_ranges=volume_ranges,
                                )
                            ):
                                pause_reason = (
                                    f"{commercial_status}/{commercial_action}："
                                    f"{commercial_summary[:500]}"
                                )
                                if hard_stage_gate and self._strict_release_mode():
                                    self._ui_progress_append(
                                        f" {commercial_status}/{commercial_action}（硬闸门未通过，候选章仅保存待审草稿）\n"
                                    )
                                    _pause_without_retry(pause_reason, chapter_content)
                                    continue
                                else:
                                    commercial_review_pause_after_save = True
                                    commercial_review_pause_reason = pause_reason
                                    self._ui_progress_append(
                                        f" {commercial_status}/{commercial_action}（正文照常保存，保存后暂停）\n"
                                    )
                            else:
                                self._ui_progress_append(
                                    f" {commercial_status}/{commercial_action}\n"
                                )
                        except Exception as exc:
                            if self._strict_release_mode():
                                chapter_status = "可继续但需复核"
                            chapter_review_notes.append(f"阶段商业审稿调用失败：{str(exc)[:160]}")
                            pause_on_unavailable = commercial_reviewer.should_pause_after_review(
                                chap_num,
                                "UNAVAILABLE",
                                "UNAVAILABLE",
                                self.config,
                                review_unavailable=True,
                                volume_ranges=volume_ranges,
                            )
                            unavailable_reason = f"阶段商业审稿不可用：{str(exc)[:500]}"
                            if (
                                self._strict_release_mode()
                                and pause_on_unavailable
                                and commercial_reviewer.is_hard_gate(
                                    chap_num, self.config, volume_ranges
                                )
                            ):
                                _pause_without_retry(unavailable_reason, chapter_content)
                            elif pause_on_unavailable and self._strict_release_mode():
                                commercial_review_pause_after_save = True
                                commercial_review_pause_reason = unavailable_reason
                            self._append_batch_audit({
                                "event": "commercial_stage_review_unavailable",
                                "chapter": chap_num,
                                "error": str(exc)[:800],
                            })
                            self._ui_progress_append(
                                (
                                    f" FAIL（服务异常，硬闸门保持待审）：{str(exc)[:100]}\n"
                                    if self._strict_release_mode()
                                    else f" WARN（阶段建议不可用，不阻塞续写）：{str(exc)[:100]}\n"
                                )
                            )
                            if should_pause_for_review:
                                continue

                    success = True

                except Exception as e:
                    err_msg = str(e)
                    err_lower = err_msg.lower()
                    if "已达到止损" in err_msg or "费用止损" in err_msg:
                        fatal_api_stop_msg = err_msg
                        self._ui_progress_append(
                            f"\n  [PAUSE] 费用止损已触发，当前章不保存：{err_msg[:180]}\n"
                        )
                        retry_count = max_retries
                        break
                    if (
                        provider == PROVIDER_CODEX_SOL
                        and self._is_codex_fatal_transport_error(e)
                    ):
                        fatal_api_stop_msg = (
                            "Codex CLI、访问权限、协议或证据链异常，已立即安全暂停；"
                            "当前章不保存，也不会把基础设施错误当成内容问题重写。"
                        )
                        self._append_batch_audit({
                            "event": "codex_transport_fatal_pause",
                            "chapter": chap_num,
                            "error_type": type(e).__name__,
                            "error": err_msg[:800],
                        })
                        self._ui_progress_append(
                            f"\n  [PAUSE] {fatal_api_stop_msg}\n  {err_msg[:180]}\n"
                        )
                        retry_count = max_retries
                        break
                    is_balance_error = (
                        "insufficient balance" in err_lower
                        or "余额不足" in err_msg
                        or "error code: 402" in err_lower
                    )
                    if is_balance_error:
                        fatal_api_stop_msg = "余额不足（402 Insufficient Balance），本轮已停止。请充值、检查 API 账户，或切换可用模型后再继续。"
                        self._ui_progress_append(f"\n  [FAIL] 生成中断: {fatal_api_stop_msg}\n")
                        retry_count = max_retries
                        break

                    transient_api_error = self._is_codex_timeout_error(e) or any(marker in err_lower for marker in (
                        "incomplete chunked",
                        "peer closed connection",
                        "connection reset",
                        "connection error",
                        "timed out",
                        "timeout",
                        "rate limit",
                        "error code: 429",
                        "error code: 500",
                        "error code: 502",
                        "error code: 503",
                        "error code: 504",
                    ))
                    api_retry_max = max(1, int(self.config.get("api_retry_max_attempts", 5) or 5))
                    if transient_api_error:
                        api_retry_count += 1
                    if transient_api_error and api_retry_count < api_retry_max:
                        base_wait = max(1, int(self.config.get("api_retry_backoff_seconds", 3) or 3))
                        wait_seconds = min(30, base_wait * (2 ** (api_retry_count - 1)))
                        chapter_content = ""
                        self._write_batch_state(
                            status="running",
                            current_chapter=chap_num,
                            stage="api_transport_retry",
                            attempt=api_retry_count,
                            message=err_msg[:300],
                        )
                        self._append_batch_audit({
                            "event": "api_transport_retry",
                            "chapter": chap_num,
                            "attempt": api_retry_count,
                            "wait_seconds": wait_seconds,
                            "error": err_msg[:800],
                        })
                        self._ui_progress_append(
                            f"\n  [RETRY] 网络传输中断，{wait_seconds}秒后重连 "
                            f"({api_retry_count}/{api_retry_max})。\n"
                        )
                        time.sleep(wait_seconds)
                        continue
                    if transient_api_error:
                        fatal_api_stop_msg = (
                            f"网络连续中断{api_retry_count}次，未保存不完整正文。"
                            "保留批次状态，网络恢复后可从当前章继续。"
                        )
                        self._ui_progress_append(f"\n  [PAUSE] {fatal_api_stop_msg}\n")
                        retry_count = max_retries
                        break

                    if "maximum context length" in err_lower:
                        try:
                            self._ui_progress_append(
                                f"\n  [FAIL] 上下文超限：估算输入 {prompt_stats['final']['input_tokens']} tokens"
                                f"（system {prompt_stats['final']['system_chars']} chars / "
                                f"user {prompt_stats['final']['user_chars']} chars）\n"
                            )
                        except Exception:
                            self._ui_progress_append("\n  [FAIL] 上下文超限。\n")

                    self._ui_progress_append(f"\n  [FAIL] 生成失败: {err_msg[:80]}\n")
                    if retry_count < max_retries - 1:
                        repair_notes.append(f"第{retry_count + 1}版生成异常：{err_msg[:500]}")
                        retry_count += 1
                        self._write_batch_state(
                            status="running",
                            current_chapter=chap_num,
                            stage="api_retry",
                            attempt=retry_count + 1,
                            message=err_msg[:300],
                        )
                        self._append_batch_audit({
                            "event": "api_retry",
                            "chapter": chap_num,
                            "attempt": retry_count + 1,
                            "error": err_msg[:800],
                        })
                        time.sleep(3)
                    else:
                        retry_count = max_retries

            if not success:
                if self._stop_event.is_set():
                    self._ui_progress_append("[PAUSE] 用户停止，当前章节未成功生成，跳过保存。")
                    batch_end_message = "[PAUSE] 批量生成已停止。"
                    break

                if fatal_api_stop_msg:
                    if "网络连续中断" in fatal_api_stop_msg:
                        self._ui_progress_append(f"\n\n[PAUSE] {fatal_api_stop_msg}\n")
                        batch_end_message = "[PAUSE] 批量生成因网络连续中断暂停，可自动恢复。"
                    elif "止损" in fatal_api_stop_msg:
                        self._ui_progress_append(f"\n\n[PAUSE] {fatal_api_stop_msg}\n")
                        batch_end_message = "[PAUSE] 批量生成已达到本书 API 费用止损。"
                    elif "Codex CLI" in fatal_api_stop_msg:
                        self._ui_progress_append(f"\n\n[PAUSE] {fatal_api_stop_msg}\n")
                        batch_end_message = "[PAUSE] Codex 基础设施异常，已安全暂停。"
                    else:
                        self._ui_progress_append(f"\n\n[FAIL] {fatal_api_stop_msg}\n")
                        batch_end_message = "[FAIL] 批量生成已中止（余额不足）。"
                    break

                if should_pause_for_review:
                    save_content = paused_failure_content or chapter_content
                    if save_content:
                        draft_path = _save_review_draft(chapter_filepath, save_content)
                        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', save_content))
                        self._write_batch_state(
                            status="paused",
                            current_chapter=chap_num,
                            stage="awaiting_review",
                            message=last_failure_reason[:300] if last_failure_reason else "质检未通过",
                        )
                        self._append_batch_audit({
                            "event": "chapter_paused",
                            "chapter": chap_num,
                            "reason": last_failure_reason[:800] if last_failure_reason else "质检未通过",
                            "draft": draft_path,
                        })
                        self._ui_progress_append(
                            f"\n  [WARN] 第 {chap_num} 章质检未通过，已保存待审草稿({chinese_chars}字)：{os.path.basename(draft_path)}\n"
                            f"  [PAUSE] 批量生成已暂停。\n\n"
                        )
                        skipped_chapters.append(chap_num)
                        batch_end_message = f"[PAUSE] 批量生成已暂停：第 {chap_num} 章质检未通过。"
                        break
                    skipped_chapters.append(chap_num)
                    batch_end_message = f"[PAUSE] 批量生成已暂停：第 {chap_num} 章未能生成可审草稿。"
                    break

                if chapter_content:
                    draft_path = _save_review_draft(chapter_filepath, chapter_content)
                    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', chapter_content))
                    self._ui_progress_append(
                        f"\n  [WARN] 第 {chap_num} 章未通过，已保存待审草稿({chinese_chars}字)：{os.path.basename(draft_path)}\n"
                        f"  [PAUSE] 批量生成已暂停。\n\n"
                    )
                    skipped_chapters.append(chap_num)
                    batch_end_message = f"[PAUSE] 批量生成已暂停：第 {chap_num} 章未通过。"
                    break
                skipped_chapters.append(chap_num)
                batch_end_message = f"[PAUSE] 批量生成已暂停：第 {chap_num} 章未能生成。"
                break

            if chapter_status != "正式可用":
                draft_path = _save_review_draft(chapter_filepath, chapter_content)
                skipped_chapters.append(chap_num)
                self._write_batch_state(
                    status="paused",
                    resume_allowed=False,
                    current_chapter=chap_num,
                    stage="awaiting_review",
                    message="候选章存在未解决警告，未进入正式目录",
                )
                self._append_batch_audit({
                    "event": "non_official_candidate_blocked",
                    "chapter": chap_num,
                    "status": chapter_status,
                    "notes": chapter_review_notes[:8],
                    "draft": draft_path,
                })
                self._ui_progress_append(
                    f"  [PAUSE] 第 {chap_num} 章状态为“{chapter_status}”，"
                    f"已保存待审草稿：{os.path.basename(draft_path)}；未写入正式目录。\n"
                )
                batch_end_message = (
                    f"[PAUSE] 第 {chap_num} 章仍有警告，修正并重新通过全部闸门后才能继续。"
                )
                break

            # ---- 跨章一致性快检（保存正式稿前执行） ----
            cross_issues = self._run_cross_chapter_check(
                chapter_content, chap_num, chapter_filepath
            )

            current_cross_issues = [
                i for i in cross_issues
                if self._cross_issue_touches_chapter(i, chap_num)
            ]
            historical_cross_issues = [
                i for i in cross_issues
                if not self._cross_issue_touches_chapter(i, chap_num)
            ]
            blocking_cross_issues = [
                issue for issue in cross_issues
                if self._cross_issue_is_blocking(issue)
            ]
            advisory_cross_issues = [
                issue for issue in cross_issues
                if (
                    issue.get("severity") == "HIGH"
                    and not self._cross_issue_is_blocking(issue)
                )
            ]
            historical_blocking_issues = [
                issue for issue in historical_cross_issues
                if self._cross_issue_is_blocking(issue)
            ]
            if historical_blocking_issues:
                self._ui_progress_append(
                    f"  [FAIL] 跨章扫描发现 {len(historical_blocking_issues)} 个未解决历史硬问题，"
                    f"已纳入本次阻断。\n"
                )
                self._append_batch_audit({
                    "event": "historical_cross_issues_blocking",
                    "chapter": chap_num,
                    "issues": historical_blocking_issues[:5],
                })
            if advisory_cross_issues:
                self._ui_progress_append(
                    f"  [WARN] 跨章扫描发现 {len(advisory_cross_issues)} 个长期文风问题，"
                    "已交给阶段审稿与后续写作约束，不阻断当前章入库。\n"
                )
                self._append_batch_audit({
                    "event": "cross_chapter_style_advisory",
                    "chapter": chap_num,
                    "issues": advisory_cross_issues[:5],
                })
            if blocking_cross_issues:
                draft_path = _save_review_draft(chapter_filepath, chapter_content)
                skipped_chapters.append(chap_num)
                self._ui_progress_append(
                    f"  [PAUSE] 第 {chap_num} 章跨章扫描发现高优先级问题，已保存待审草稿：{os.path.basename(draft_path)}\n"
                    f"  批量生成已暂停。\n"
                )
                self._write_batch_state(
                    status="paused",
                    current_chapter=chap_num,
                    stage="cross_chapter_review",
                    message=f"跨章扫描发现{len(blocking_cross_issues)}个高优先级硬问题",
                )
                self._append_batch_audit({
                    "event": "chapter_paused",
                    "chapter": chap_num,
                    "reason": "跨章扫描未通过",
                    "issues": blocking_cross_issues[:5],
                })
                self._ui(self.refresh_reader_files_silent)
                batch_end_message = f"[PAUSE] 批量生成已暂停：第 {chap_num} 章跨章扫描未通过。"
                break

            # ---- 保存章节 ----
            # First durably record the already-reviewed inputs.  If Windows,
            # Python, or the machine stops after the official file is replaced,
            # the next health check can finish every metadata step without
            # asking DeepSeek to recreate anything.
            try:
                commercial_should_run = commercial_reviewer.should_run(
                    chap_num, self.config, volume_ranges
                )
                commercial_clear_pass = self._commercial_review_is_clear_pass(
                    commercial_review_result
                )
                bind_commercial_review = bool(
                    commercial_review_result
                    and (self._strict_release_mode() or commercial_clear_pass)
                )
                chapter_commit.begin(
                    generator.DIRS["plot"],
                    chapter=chap_num,
                    chapter_path=chapter_filepath,
                    chapter_text=chapter_content,
                    chapter_outline=chapter_outline,
                    prepared_state_delta=prepared_state_delta,
                    narrative_audit_result=narrative_audit_result,
                    narrative_audit_required=self._strict_release_mode(),
                    commercial_review_result=(
                        commercial_review_result if bind_commercial_review else None
                    ),
                    commercial_review_required=bool(
                        commercial_should_run
                        and (self._strict_release_mode() or commercial_clear_pass)
                    ),
                    commercial_hard_gate=bool(
                        commercial_reviewer.is_hard_gate(
                            chap_num, self.config, volume_ranges
                        )
                        and (self._strict_release_mode() or commercial_clear_pass)
                    ),
                    commercial_expected_chapters=(
                        list(
                            (commercial_review_result or {}).get(
                                "expected_chapters"
                            )
                            or []
                        )
                        if bind_commercial_review
                        else []
                    ),
                    commercial_review_model=(
                        self._commercial_review_model_for_result(
                            commercial_review_result
                        )
                        if bind_commercial_review
                        else ""
                    ),
                    validation_receipt=chapter_validation_receipt,
                    char_limits=char_limits,
                    chapter_status=chapter_status,
                    chapter_review_notes=chapter_review_notes,
                )
                _atomic_write_text(chapter_filepath, chapter_content)
                chapter_commit.mark_step(generator.DIRS["plot"], "chapter_saved")
            except Exception as exc:
                try:
                    if not os.path.exists(chapter_filepath):
                        chapter_commit.discard_unwritten(generator.DIRS["plot"])
                except Exception:
                    pass
                batch_end_message = f"[PAUSE] 第 {chap_num} 章正式稿写入失败：{str(exc)[:160]}"
                self._write_batch_state(
                    status="paused",
                    resume_allowed=False,
                    current_chapter=chap_num,
                    stage="chapter_save_failed",
                    message=batch_end_message,
                )
                self._append_batch_audit({
                    "event": "chapter_save_failed",
                    "chapter": chap_num,
                    "error": str(exc)[:800],
                })
                self._ui_progress_append(f"\n  {batch_end_message}\n")
                break

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            history_path = os.path.join(generator.DIRS["hist"], f"Ch{chap_num}_{timestamp}.txt")
            _atomic_write_text(
                history_path,
                f"[批量生成] 第{chap_num}章\n\n" + "-" * 40 + "\n\n" + chapter_content,
            )

            self._ui_clear(chapter_content)
            if chapter_status == "正式可用":
                self._ui_progress_append(
                    f"  [OK] 第 {chap_num} 章正式稿已保存！状态：{chapter_status}({chinese_chars}字)；"
                    "正在校验长期记忆，校验成功后才进入发布稿\n"
                )
            else:
                self._ui_progress_append(
                    f"  [WARN] 第 {chap_num} 章正式稿已保存！状态：{chapter_status}({chinese_chars}字)，"
                    "需后续复核；长期记忆校验完成前不会进入发布稿。\n"
                )
            try:
                recovered_commit = self._recover_pending_chapter_commit()
                self._append_batch_audit({
                    "event": "chapter_commit_complete",
                    "chapter": chap_num,
                    "receipt": recovered_commit.get("receipt", ""),
                })
            except Exception as exc:
                batch_end_message = (
                    f"[PAUSE] 第 {chap_num} 章正文已保存，但提交收尾失败："
                    f"{str(exc)[:180]}。下次体检会从正文断点继续。"
                )
                self._write_batch_state(
                    status="paused",
                    resume_allowed=False,
                    current_chapter=chap_num,
                    stage="post_save_commit_failed",
                    message=batch_end_message,
                )
                self._append_batch_audit({
                    "event": "post_save_commit_failed",
                    "chapter": chap_num,
                    "error": str(exc)[:500],
                })
                self._ui_progress_append(f"\n  {batch_end_message}\n")
                break

            # In simple mode an advisory WARN/FAIL is intentionally not bound
            # to the formal chapter receipt, but its action plan must still be
            # durable after the chapter is safely committed.  Otherwise the
            # same diagnosis can recur for hundreds of chapters without ever
            # entering the next generation context.
            if commercial_review_result and not bind_commercial_review:
                try:
                    self._persist_unbound_commercial_review_actions(
                        commercial_review_result,
                        chap_num,
                        bound_to_commit=bind_commercial_review,
                    )
                except Exception as exc:
                    chapter_review_notes.append(
                        f"阶段审稿行动单同步失败（不回滚已提交正文）：{str(exc)[:160]}"
                    )
                    self._append_batch_audit({
                        "event": "commercial_revision_actions_persist_failed",
                        "chapter": chap_num,
                        "error": str(exc)[:500],
                    })

            if chapter_status == "可继续但需复核":
                consecutive_review_count += 1
            else:
                consecutive_review_count = 0
            max_review_streak = max(
                1,
                int(self.config.get("max_consecutive_review_chapters", 5) or 5),
            )
            review_streak_limit_reached = consecutive_review_count >= max_review_streak
            self._write_batch_state(
                status="running",
                current_chapter=chap_num,
                completed_count=i + 1,
                stage="saved",
                message=f"第{chap_num}章已保存",
            )
            self._append_batch_audit({
                "event": "chapter_saved",
                "chapter": chap_num,
                "chars": chinese_chars,
                "attempts": min(max_retries, retry_count + 1),
                "consecutive_review_chapters": consecutive_review_count,
                "path": chapter_filepath,
            })
            self._ui(self.refresh_reader_files_silent)

            if commercial_review_pause_after_save:
                summary = commercial_review_pause_reason or (
                    commercial_review_result or {}
                ).get("summary", "阶段商业审稿要求调整方向")
                report_path = (commercial_review_result or {}).get("report_path", "")
                batch_end_message = (
                    f"[PAUSE] 第 {chap_num} 章已完整保存；阶段商业审稿判定应先调整后续方向："
                    f"{summary[:220]}"
                )
                self._write_batch_state(
                    status="paused",
                    resume_allowed=False,
                    current_chapter=chap_num,
                    stage="commercial_review_pause",
                    message=batch_end_message,
                    review_report=report_path,
                )
                self._append_batch_audit({
                    "event": "commercial_review_pause",
                    "chapter": chap_num,
                    "summary": summary[:800],
                    "report": report_path,
                })
                self._ui_progress_append(f"\n  {batch_end_message}\n")
                break

            if review_streak_limit_reached:
                batch_end_message = (
                    f"[PAUSE] 已连续 {consecutive_review_count} 章标记为需复核。"
                    "为避免软问题持续污染后文，托管已在完整保存本章后暂停。"
                )
                self._write_batch_state(
                    status="paused",
                    resume_allowed=False,
                    current_chapter=chap_num,
                    stage="review_streak_limit",
                    message=batch_end_message,
                )
                self._append_batch_audit({
                    "event": "review_streak_limit_reached",
                    "chapter": chap_num,
                    "count": consecutive_review_count,
                    "limit": max_review_streak,
                })
                self._ui_progress_append(f"\n  {batch_end_message}\n")
                break

            # 只有到达规划终章后才接受完结标记，防止模型中途误收书。
            last_200 = chapter_content[-200:].lower()
            if (
                any(m.lower() in last_200 for m in ending_markers)
                and (not planned_target_end or chap_num >= planned_target_end)
            ):
                self._ui_progress_append(f"\n\n已到规划终章并检测到完结标志，共 {chap_num} 章。")
                break

            # 章节间冷却 + 停止检查 (P0修复: 保存完毕后才响应停止)
            if self._stop_event.is_set():
                self._ui_progress_append(f"\n[PAUSE] 用户停止，第 {chap_num} 章已完整保存，停止批量生成。")
                batch_end_message = f"[PAUSE] 用户停止，已完整保存到第 {chap_num} 章。"
                break
            time.sleep(2)

        # 完成
        self._ui(self.refresh_status)
        trial_completed = bool(
            self._trial_mode_pending()
            and planned_target_end == self._get_trial_stop_chapter()
            and self._trial_gate_ready()
        )
        if trial_completed:
            batch_end_message = (
                f"[TRIAL PASS] 第1-{planned_target_end}章试写已通过硬闸门。"
                "系统已停止扩大生产，等待用户确认继续整本。"
            )
        if skipped_chapters:
            skip_str = '、'.join(str(c) for c in skipped_chapters)
            self._ui_progress_append(f"\n\n[WARN] 以下章节质检未通过，已保存为 _待审.txt 草稿，需人工处理：第 {skip_str} 章\n")
        self._ui_progress_append(f"\n{'=' * 40}\n{batch_end_message}\n")
        final_status = (
            "trial_passed"
            if trial_completed
            else "completed" if not skipped_chapters and "完毕" in batch_end_message else "paused"
        )
        resume_allowed = False
        if fatal_api_stop_msg and "网络连续中断" in fatal_api_stop_msg:
            current_cycles = int(self._load_batch_state().get("network_resume_cycles") or 0)
            max_cycles = max(0, int(self.config.get("auto_resume_network_max_cycles", 6) or 0))
            if current_cycles >= max_cycles:
                final_status = "paused"
                batch_end_message = f"[PAUSE] 网络自动恢复已达到{max_cycles}轮上限，请检查网络后再继续。"
            else:
                final_status = "interrupted"
                resume_allowed = True
        if "中止" in batch_end_message:
            final_status = "failed"
        self._write_batch_state(
            status=final_status,
            resume_allowed=resume_allowed,
            stage="finished",
            skipped_chapters=skipped_chapters,
            message=batch_end_message,
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
        self._append_batch_audit({
            "event": "batch_end",
            "status": final_status,
            "message": batch_end_message,
            "skipped_chapters": skipped_chapters,
        })
        self.is_batch_running = False
        self._ui(lambda: self.btn_new.config(state=tk.NORMAL))
        self._ui(lambda: self.btn_continue.config(state=tk.NORMAL))
        self._ui(lambda: self.btn_batch.config(state=tk.NORMAL))
        self._ui(lambda: self.btn_full_book.config(state=tk.NORMAL))
        self._ui(lambda: self.btn_stop.config(state=tk.DISABLED))
        self.is_generating = False
        self._ui(self.update_word_count)
        if (
            final_status == "interrupted"
            and resume_allowed
            and self.config.get("auto_resume_full_book", False)
        ):
            delay_seconds = max(
                10,
                int(self.config.get("auto_resume_network_delay_seconds", 60) or 60),
            )
            self._ui_progress_append(
                f"\n[INFO] {delay_seconds}秒后将自动从当前章恢复一键全本任务。\n"
            )
            self._ui(
                lambda: self.root.after(
                    delay_seconds * 1000,
                    self._auto_resume_interrupted_full_book,
                )
            )


    def _detect_chapter_repetition(self, content, chap_num=None):
        """返回章节内明显拼接/重复问题；空列表表示通过。"""
        if not content:
            return ["内容为空"]

        text = content.replace("\r\n", "\n").replace("\r", "\n").strip()
        lines = text.split("\n")
        issues = []

        title_pat = re.compile(r'^第\s*([零〇一二三四五六七八九十百千万两\d]+)\s*章(?:\s|$)')
        title_hits = []
        for idx, line in enumerate(lines, 1):
            stripped = line.strip().lstrip("#").strip()
            if title_pat.match(stripped):
                title_hits.append((idx, stripped))
        if len(title_hits) > 1:
            positions = "、".join(str(pos) for pos, _ in title_hits[:4])
            issues.append(f"同章内出现{len(title_hits)}个章节标题（行{positions}）")

        # 只统计较长叙事段，避免“继续训练”“嗯”等合理短句触发误报。
        paragraphs = []
        for raw in re.split(r'\n\s*\n+', text):
            para = re.sub(r'\s+', '', raw.strip())
            if not para:
                continue
            if title_pat.match(para):
                continue
            chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', para))
            if chinese_chars >= 28 and len(para) >= 35:
                paragraphs.append((para, chinese_chars))

        seen = {}
        repeated_count = 0
        repeated_chars = 0
        samples = []
        for para, chinese_chars in paragraphs:
            if para in seen:
                seen[para] += 1
                repeated_count += 1
                repeated_chars += chinese_chars
                if len(samples) < 2:
                    samples.append(para[:60])
            else:
                seen[para] = 1

        if repeated_count >= 1:
            issues.append(f"长段落重复{repeated_count}处，约{repeated_chars}字")
            if samples:
                issues.append("重复样例：" + " / ".join(samples))

        sentences = [
            re.sub(r'\s+', '', item)
            for item in re.split(r'[。！？!?]+', text)
            if len(re.sub(r'\s+', '', item)) >= 12
        ]
        sentence_counts = {}
        for sentence in sentences:
            sentence_counts[sentence] = sentence_counts.get(sentence, 0) + 1
        duplicate_sentence = next(
            (sentence for sentence, count in sentence_counts.items() if count >= 2),
            "",
        )
        if duplicate_sentence:
            issues.append("同一句子完整重复两次以上：" + duplicate_sentence[:60])

        for issue in narrative_guard.deterministic_issues(content):
            if issue not in issues:
                issues.append(issue)

        return issues

    def _find_official_repetition_issues(self, latest_chap=None, lookback=40):
        """扫描近期正式章节，发现已混入的重复结构。"""
        out_dir = generator.DIRS.get("out", "output")
        if not os.path.isdir(out_dir):
            return []

        files = []
        for root_dir, dirs, fnames in os.walk(out_dir):
            dirs[:] = [d for d in dirs if d != ".backup"]
            for fname in fnames:
                if self._is_official_chapter_file(fname):
                    fpath = os.path.join(root_dir, fname)
                    chap = self._extract_chap_num_from_path(fpath)
                    if latest_chap and chap < max(1, latest_chap - lookback + 1):
                        continue
                    files.append((chap, fpath))
        files.sort(key=lambda x: x[0])

        problems = []
        for chap, fpath in files:
            try:
                content = generator.read_text_safe(fpath)
                issues = self._detect_chapter_repetition(content, chap)
                if issues:
                    problems.append((chap, issues, fpath))
            except Exception as exc:
                problems.append((chap, [f"读取失败：{str(exc)[:80]}"], fpath))
        return problems

    def _truncate_chapter(self, content, target_chars):
        """将章节截断到目标字数，在段落结尾处切断"""
        char_count = 0
        lines = content.split('\n')
        cut_idx = len(lines)

        for i, line in enumerate(lines):
            chars_in_line = len(re.findall(r'[\u4e00-\u9fff]', line))
            char_count += chars_in_line
            if char_count >= target_chars:
                # 找到最近的空行（段落结尾）
                for j in range(i + 1, min(i + 10, len(lines))):
                    if not lines[j].strip():
                        cut_idx = j
                        break
                else:
                    cut_idx = i + 1
                break

        kept = lines[:cut_idx]
        while kept and not kept[-1].strip():
            kept.pop()
        return '\n'.join(kept)

    def _validate_memory_update(self, old_content, new_content, chap_num):
        """防止记忆压缩把关键锚点整段压没。"""
        new_text = (new_content or "").strip()
        old_text = (old_content or "").strip()
        if len(new_text) < 80:
            return False, "新备忘录过短，疑似压缩失败"
        if old_text and len(new_text) < min(300, len(old_text) * 0.25):
            return False, "新备忘录相对旧备忘录缩得过猛，疑似丢失上下文"
        if re.search(r'^(无|暂无|没有需要记录|本章无重要信息)[。.!！\s]*$', new_text):
            return False, "新备忘录为空泛结论，不能覆盖旧记忆"

        protected_terms = self._load_tool_rules().get("memory_required_terms") or []
        old_terms = [term for term in protected_terms if term and term in old_text]
        if len(old_terms) >= 4:
            lost_terms = [term for term in old_terms if term not in new_text]
            if len(lost_terms) >= max(3, int(len(old_terms) * 0.7)):
                return False, f"新备忘录丢失过多关键锚点：{'、'.join(lost_terms[:6])}"

        if f"第{chap_num}" not in new_text and str(chap_num) not in new_text:
            self._append_batch_audit({
                "event": "memory_warning",
                "chapter": chap_num,
                "reason": "新备忘录未显式包含当前章号",
            })
        canon_check = continuity_guard.validate_memory_snapshot(
            new_text,
            generator.DIRS["plot"],
        )
        if canon_check.get("status") == "FAIL":
            return False, canon_check.get("summary") or "新备忘录与正史状态冲突"
        return True, "PASS"

    def _record_chapter_status(
        self,
        chap_num,
        status,
        notes,
        chapter_filepath,
        chars,
        narrative_audit_result=None,
    ):
        try:
            with open(chapter_filepath, "r", encoding="utf-8-sig") as f:
                chapter_text = f.read()
        except OSError as exc:
            raise RuntimeError(f"无法读取正式章节，状态未记账：{exc}") from exc
        chapter_hash = state_ledger.chapter_sha256(chapter_text)
        narrative_audit_id = ""
        narrative_review_model = ""
        if narrative_audit_result:
            narrative_review_model = self._require_automatic_review_model(
                narrative_audit_result.get("review_model"),
                f"第{int(chap_num)}章叙事审计结果",
            )
            narrative_audit_id = narrative_guard.calculate_audit_id(
                narrative_audit_result,
                chap_num,
                chapter_text,
            )
        record_id = state_ledger.chapter_sha256(
            f"{int(chap_num)}|{chapter_hash}|{str(status)}|"
            f"{narrative_audit_id}|{narrative_review_model}"
        )
        payload = {
            "record_id": record_id,
            "chapter": chap_num,
            "status": status,
            "notes": notes or [],
            "path": chapter_filepath,
            "chapter_sha256": chapter_hash,
            "narrative_audit_id": narrative_audit_id,
            "narrative_review_model": narrative_review_model,
            "chars": chars,
            "time": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            os.makedirs(generator.DIRS.get("logs", generator.DIRS["out"]), exist_ok=True)
            status_path = os.path.join(
                generator.DIRS.get("logs", generator.DIRS["out"]),
                "chapter_status.jsonl",
            )
            if os.path.exists(status_path):
                with open(status_path, "r", encoding="utf-8-sig") as f:
                    for line in f:
                        try:
                            if json.loads(line).get("record_id") == record_id:
                                return payload
                        except (json.JSONDecodeError, AttributeError):
                            continue
            with open(status_path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception as exc:
            raise RuntimeError(f"章节状态记账失败，禁止继续发布：{exc}") from exc
        self._append_batch_audit({"event": "chapter_status", **payload})
        return payload

    def _load_latest_chapter_statuses(self):
        path = os.path.join(
            generator.DIRS.get("logs", generator.DIRS["out"]),
            "chapter_status.jsonl",
        )
        latest = {}
        if not os.path.exists(path):
            return latest
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                        chapter = int(row.get("chapter") or 0)
                        if chapter > 0:
                            latest[chapter] = row
                    except Exception:
                        continue
        except Exception:
            return {}
        return latest

    def _count_consecutive_review_chapters(self):
        """Count review debt immediately behind the current official checkpoint."""
        statuses = self._load_latest_chapter_statuses()
        _, _, _, latest_chap, _ = generator.get_latest_chapter_info()
        count = 0
        for chapter in range(latest_chap, 0, -1):
            row = statuses.get(chapter) or {}
            if row.get("status") != "可继续但需复核":
                break
            count += 1
        return count

    def _remove_publish_copy(self, chap_num):
        publish_dir = generator.DIRS.get("publish", "")
        if not publish_dir:
            return False
        path = os.path.join(publish_dir, f"第{int(chap_num):04d}章.txt")
        if not os.path.isfile(path):
            return False
        os.remove(path)
        return True

    def _verify_existing_guarded_audit_history(self):
        """Prevent manual save from bypassing damaged earlier audit receipts."""
        statuses = self._load_latest_chapter_statuses()
        official_paths = self._official_chapter_paths_by_number()
        issues = []
        for chapter, path in sorted(official_paths.items()):
            row = statuses.get(int(chapter)) or {}
            if not row.get("status"):
                issues.append(f"第{chapter}章缺少可核验章节状态")
                continue
            expected_id = str((row or {}).get("narrative_audit_id") or "")
            if not expected_id:
                continue
            try:
                narrative_guard.verify_persisted_audit(
                    generator.DIRS["plot"],
                    int(chapter),
                    generator.read_text_safe(path),
                    expected_audit_id=expected_id,
                    expected_review_model=self._narrative_review_model_for_chapter(
                        chapter, row
                    ),
                    min_score=int(
                        self.config.get("narrative_guard_min_score", 75) or 75
                    ),
                )
            except Exception as exc:
                issues.append(str(exc))
        if issues:
            raise narrative_guard.NarrativeGuardError(
                "已有正式章节的叙事审计链损坏：" + "；".join(issues[:6])
            )
        return True

    def _copy_to_publish(self, chapter_filepath, chap_num):
        publish_dir = generator.DIRS.get("publish")
        if not publish_dir:
            raise RuntimeError("发布目录未配置")
        if not os.path.isfile(chapter_filepath):
            raise RuntimeError("正式章节不存在，不能生成发布稿")
        source_text = generator.read_text_exact(chapter_filepath)
        status = self._load_latest_chapter_statuses().get(int(chap_num)) or {}
        source_hash = state_ledger.chapter_sha256(source_text)
        if not status.get("status"):
            raise RuntimeError(f"第{chap_num}章缺少可核验状态，不能生成发布稿")
        if str(status.get("chapter_sha256") or "") != source_hash:
            raise RuntimeError(f"第{chap_num}章状态与正式正文哈希不一致")
        if status.get("status") != "正式可用":
            raise RuntimeError(f"第{chap_num}章仍待复核，不能生成发布稿")
        if self._strict_release_mode():
            narrative_guard.verify_persisted_audit(
                generator.DIRS["plot"],
                int(chap_num),
                source_text,
                expected_audit_id=str(status.get("narrative_audit_id") or ""),
                expected_review_model=self._narrative_review_model_for_chapter(
                    chap_num, status
                ),
                min_score=int(self.config.get("narrative_guard_min_score", 75) or 75),
            )
        os.makedirs(publish_dir, exist_ok=True)
        publish_path = os.path.join(publish_dir, f"第{chap_num:04d}章.txt")
        temp_path = publish_path + ".tmp"
        try:
            shutil.copy2(chapter_filepath, temp_path)
            with open(temp_path, "rb+") as handle:
                os.fsync(handle.fileno())
            os.replace(temp_path, publish_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        published_text = generator.read_text_exact(publish_path)
        if source_hash != state_ledger.chapter_sha256(published_text):
            raise RuntimeError("发布稿与正式章节哈希不一致")
        return publish_path

    def _commercial_publish_release_required(self):
        if not self._strict_release_mode():
            return False
        blueprint = self._load_commercial_blueprint()
        return bool(
            self.config.get("hold_publish_until_commercial_pass", True)
            and self.config.get("commercial_review_enabled", True)
            and blueprint.get("gate_status") == "PASS"
        )

    def _commercial_publish_release_ready(self):
        if not self._commercial_publish_release_required():
            return True
        latest = self._load_latest_commercial_review()
        return bool(
            int(latest.get("chapter") or 0) >= 3
            and latest.get("status") == "PASS"
            and latest.get("action") == "CONTINUE"
            and not latest.get("gate_blocked")
            and self._latest_commercial_review_valid(latest)
        )

    def _sync_approved_chapters_to_publish(self, through_chapter):
        """Release held files after a passing stage review, without releasing tech debt."""
        publish_dir = generator.DIRS.get("publish")
        if not publish_dir:
            raise RuntimeError("发布目录未配置")
        os.makedirs(publish_dir, exist_ok=True)
        statuses = self._load_latest_chapter_statuses()
        copied = 0
        for root_dir, dirs, files in os.walk(generator.DIRS["out"]):
            dirs[:] = [name for name in dirs if name != ".backup"]
            for filename in files:
                if not self._is_official_chapter_file(filename):
                    continue
                source = os.path.join(root_dir, filename)
                chapter = self._extract_chap_num_from_path(source)
                if chapter <= 0 or chapter > int(through_chapter):
                    continue
                row = statuses.get(chapter) or {}
                status = row.get("status")
                if status is None:
                    raise RuntimeError(
                        f"第{chapter}章缺少可核验的章节状态，拒绝同步发布稿"
                    )
                source_hash = state_ledger.chapter_sha256(
                    generator.read_text_exact(source)
                )
                if str(row.get("chapter_sha256") or "") != source_hash:
                    raise RuntimeError(
                        f"第{chapter}章状态记录与当前正文哈希不一致，拒绝同步发布稿"
                    )
                if status != "正式可用":
                    continue
                publish_path = os.path.join(
                    publish_dir, f"第{int(chapter):04d}章.txt"
                )
                if os.path.isfile(publish_path):
                    published_hash = state_ledger.chapter_sha256(
                        generator.read_text_exact(publish_path)
                    )
                    if published_hash == source_hash:
                        # A sealed legacy chapter may predate the current
                        # narrative-audit format.  An already-published,
                        # byte-equivalent mirror needs no destructive recopy;
                        # missing or stale mirrors still go through the strict
                        # per-chapter release checks in _copy_to_publish().
                        continue
                self._copy_to_publish(source, chapter)
                copied += 1
        return copied

    def _run_skill_pipeline(self, chap_num, chapter_filepath, chapter_content, prev_content):
        """技能流水线：润色->台词教练->钩子->多维审查->实体追踪->编年史->伏笔->记忆压缩"""
        try:
            import skill_engine
        except ImportError:
            self._ui_progress_append("\n[WARN] skill_engine 未找到，跳过技能流水线\n")
            return

        skills_dir = os.path.join(_exe_dir, "skills")
        if not os.path.isdir(skills_dir):
            skills_dir = os.path.join(self.project_dir, "skills")
        if not os.path.isdir(skills_dir):
            return

        # 按顺序执行的技能列表
        # 托管批量优先保证"不断线"和"省 token"，编辑类技能留给手动精修
        if self.is_batch_running:
            pipeline_skills = ["memory_compressor"]
            if chap_num % 3 == 0:
                pipeline_skills.extend([
                    "entity_extractor",
                    "chronicle_keeper",
                    "foreshadow_hunter",
                ])
            self._ui_progress_append("\n   托管模式：跳过润色/钩子，仅保留记忆维护链路")
        else:
            pipeline_skills = [
                "polish_master",
                "cliffhanger_expert",
                "memory_compressor",
                "entity_extractor",
                "chronicle_keeper",
                "foreshadow_hunter",
            ]

        configured_skills = self.config.get("enabled_skills", [])
        if isinstance(configured_skills, list) and configured_skills:
            pipeline_skills = [sid for sid in pipeline_skills if sid in configured_skills]

        skill_max_tokens = {
            "polish_master": 4500,
            "cliffhanger_expert": 4200,
            "memory_compressor": 1200,
            "entity_extractor": 1500,
            "chronicle_keeper": 700,
            "foreshadow_hunter": 1400,
        }

        current_content = chapter_content

        for skill_id in pipeline_skills:
            if self._stop_event.is_set():
                break

            skill_path = os.path.join(skills_dir, f"{skill_id}.json")
            if not os.path.exists(skill_path):
                continue

            try:
                with open(skill_path, "r", encoding="utf-8") as f:
                    skill_config = json.load(f)
            except Exception:
                continue

            skill_name = skill_config.get("name", skill_id)
            self._ui_progress_append(f"\n   {skill_name}...")

            # 构建上下文
            extra_context = ""
            if skill_config.get("requires_context"):
                memo_path = os.path.join(generator.DIRS["plot"], "全局备忘录.txt")
                extra_context = generator.read_text_safe(memo_path)
                if prev_content:
                    extra_context += f"\n\n【上一章结尾200字】\n{prev_content[-200:]}"
            if skill_id == "memory_compressor":
                canon_context = continuity_guard.build_canon_context(
                    generator.DIRS["plot"],
                    max_chars=2800,
                )
                if canon_context:
                    extra_context += f"\n\n【不可覆盖正史】\n{canon_context}"

            # 构建 LLM 调用函数（匹配 skill_engine.execute_skill 签名）
            def _make_llm_fn():
                _max_tokens = min(
                    self.config.get("max_tokens", 8192),
                    skill_config.get("max_tokens", skill_max_tokens.get(skill_id, 1500))
                )
                def llm_fn(system_prompt, user_prompt, temperature):
                    return self.call_llm_non_stream(
                        system_prompt,
                        user_prompt,
                        temp=temperature,
                        max_tokens=_max_tokens,
                        review=False,
                        task_type=f"skill_{skill_id}",
                    )
                return llm_fn

            # 执行技能（带重试）
            result = ""
            try:
                for skill_try in range(3):
                    try:
                        result = skill_engine.execute_skill(
                            skill_config,
                            current_content,
                            _make_llm_fn(),
                            extra_context=extra_context,
                        )
                        break
                    except Exception as api_err:
                        if skill_try < 2:
                            time.sleep(3)
                        else:
                            raise api_err
            except Exception as e:
                if self._is_codex_fatal_transport_error(e):
                    raise
                self._ui_progress_append(f" [FAIL] 失败: {str(e)[:60]}")
                continue

            if not result or not result.strip():
                self._ui_progress_append(" (无输出，跳过)")
                continue

            # 处理输出
            output_type = skill_config.get("output_type", "popup")

            if output_type == "save_to_file":
                save_dir = os.path.join(generator.DIRS["plot"])
                save_filename = skill_config.get("save_filename", f"{skill_id}_output.txt")
                save_path = os.path.join(save_dir, save_filename)
                save_mode = skill_config.get("save_mode", "overwrite")

                old_file_content = generator.read_text_safe(save_path)
                # 备忘录损坏保护
                if save_filename == "全局备忘录.txt":
                    memory_ok, memory_msg = self._validate_memory_update(old_file_content, result, chap_num)
                    if not memory_ok:
                        self._ui_progress_append(f" [WARN] {memory_msg}，保留原备忘录")
                        self._append_batch_audit({
                            "event": "memory_update_rejected",
                            "chapter": chap_num,
                            "reason": memory_msg,
                        })
                        continue

                if save_mode == "overwrite":
                    # 覆盖前备份
                    if os.path.exists(save_path) and save_filename == "全局备忘录.txt":
                        backup_dir = os.path.join(generator.DIRS["plot"], "备忘录备份")
                        os.makedirs(backup_dir, exist_ok=True)
                        backup_name = f"备忘录_Ch{chap_num}_{datetime.now().strftime('%H%M%S')}.txt"
                        shutil.copy2(save_path, os.path.join(backup_dir, backup_name))
                    with open(save_path, "w", encoding="utf-8") as f:
                        f.write(result)
                else:
                    with open(save_path, "a", encoding="utf-8") as f:
                        f.write("\n" + result)
                self._ui_progress_append(" [OK]")

            elif output_type in ("replace_editor", "editor_replace"):
                # P0 修复：批量托管模式禁止任何技能替换正文
                if self.is_batch_running:
                    self._ui_progress_append(" [SKIP] 托管模式跳过正文替换")
                    continue
                # replace_editor: 润色/台词/钩子 — 替换章节内容
                result_chars = len(re.findall(r'[\u4e00-\u9fff]', result))
                original_chars = len(re.findall(r'[\u4e00-\u9fff]', current_content))

                # 字数回滚保护 (Section 10.3)
                if result_chars < 1800 or result_chars < original_chars * 0.8:
                    self._ui_progress_append(f" [WARN] 字数暴跌({original_chars}->{result_chars})，回滚")
                else:
                    # 标题保护
                    original_first_line = current_content.split("\n")[0].strip()
                    result_first_line = result.split("\n")[0].strip().lstrip("#").strip()
                    if not result_first_line.startswith("第") and original_first_line.startswith("第"):
                        result = original_first_line + "\n\n" + result

                    # Feature 5: 覆盖前备份
                    backup_dir = os.path.join(os.path.dirname(chapter_filepath), ".backup")
                    os.makedirs(backup_dir, exist_ok=True)
                    backup_name = f"第{str(chap_num).zfill(4)}章_before_{skill_name}.txt"
                    with open(os.path.join(backup_dir, backup_name), "w", encoding="utf-8") as f:
                        f.write(current_content)

                    try:
                        self._commit_manual_chapter(
                            chap_num,
                            chapter_filepath,
                            result,
                            f"技能精修_{skill_name}",
                        )
                    except Exception as exc:
                        self._ui_progress_append(
                            f" [BLOCKED] 精修稿未通过正式闸门：{str(exc)[:100]}"
                        )
                        continue
                    current_content = result
                    self._ui_progress_append(" [OK]")
            else:
                # popup 或其他: 只显示不替换
                self._ui_progress_append(" [OK]")

            time.sleep(2)  # 技能间冷却

        if current_content != chapter_content:
            self._ui_clear(current_content)

    def stop_batch(self):
        self._stop_event.set()
        preparing = bool(self._initialization_running and not self.is_batch_running)
        self._write_batch_state(
            status="paused",
            resume_allowed=False,
            stage="manual_stop_requested",
            message=(
                "用户要求停止，当前模型调用返回后终止自动准备。"
                if preparing else
                "用户要求停止，当前安全步骤完成后暂停。"
            ),
        )
        self.btn_stop.config(state=tk.DISABLED)
        self._ui_progress_append(
            "\n\n[PAUSE] 已请求停止；当前模型调用返回后不再开始下一步，"
            "已经完整保存的文件会保留。\n"
            if preparing else
            "\n\n[PAUSE] 已请求停止；系统会在当前安全边界停下，"
            "不会把半章当成正式稿。\n"
        )

    def _cross_issue_touches_chapter(self, issue, chap_num):
        """判断跨章扫描问题是否应归因到当前新章。"""
        for key in ("chap", "dup_chap", "curr_chap"):
            if issue.get(key) == chap_num:
                return True
        matched_chapters = issue.get("matched_chapters")
        if isinstance(matched_chapters, (list, tuple, set)):
            if chap_num in matched_chapters:
                return True
        # 事件去重里如果当前章是首次出现，后续重复通常来自历史扫描窗口，
        # 不应该把当前章转待审；因此 first_chap 不作为拦截依据。
        return False

    def _cross_issue_is_blocking(self, issue):
        """硬事实矛盾继续拦截；简单模式的累计文风密度只做改进提醒。"""
        if issue.get("severity") != "HIGH":
            return False
        if (
            issue.get("type") == "叙述模板重复"
            and not self._strict_release_mode()
        ):
            return False
        return True

    # ============================================================
    # 保存功能
    # ============================================================
    @staticmethod
    def _strip_preflight_volatile_fields(value):
        """Remove timestamps/paths that do not change a review decision."""
        volatile = {
            "updated_at",
            "persisted_at",
            "validated_at",
            "created_at",
            "completed_at",
            "failed_at",
            "stored_at",
            "report_path",
            "action_plan_path",
            "revision_debt_path",
        }
        if isinstance(value, dict):
            return {
                str(key): NovelGeneratorGUI._strip_preflight_volatile_fields(item)
                for key, item in value.items()
                if str(key) not in volatile
            }
        if isinstance(value, (list, tuple)):
            return [
                NovelGeneratorGUI._strip_preflight_volatile_fields(item)
                for item in value
            ]
        return value

    @staticmethod
    def _formal_suffix_source_digest(path, *, normalize_volatile=False):
        """Hash a preflight source by review semantics, not runtime metadata."""
        with open(path, "rb") as handle:
            raw = handle.read()
        if normalize_volatile:
            try:
                payload = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            else:
                stable = NovelGeneratorGUI._strip_preflight_volatile_fields(
                    payload
                )
                return formal_suffix_rewrite.build_preflight_fingerprint(stable)
        return hashlib.sha256(raw).hexdigest()

    def _formal_suffix_cache_active(self):
        context = getattr(self, "_formal_suffix_rewrite_context", None)
        return isinstance(context, dict) and bool(context.get("cache_enabled"))

    def _formal_suffix_emit_progress(self, event, **payload):
        context = getattr(self, "_formal_suffix_rewrite_context", None)
        if not isinstance(context, dict):
            if str(event).startswith("state_model_call_"):
                row = {"event": str(event), **payload}
                outcome = {"state_model_call_started": "调用开始（等待返回，不代表已有新输出）",
                           "state_model_call_completed": "调用返回（仍需验收）",
                           "state_model_call_failed": "调用异常"}.get(event, event)
                message = (f"第{payload.get('chapter')}章状态第{payload.get('attempt')}轮 "
                           f"{payload.get('phase')}：{outcome}"
                           + (f"，耗时{payload['elapsed_seconds']}秒" if "elapsed_seconds" in payload else ""))
                # Ordinary GUI/headless batches have no suffix callback. Use
                # their existing thread-safe UI sink and durable audit sink.
                for sink, value in ((getattr(self, "_ui_progress_append", None), f"[状态审查] {message}\n"),
                                    (getattr(self, "_append_batch_audit", None), row)):
                    if callable(sink):
                        try:
                            sink(value)
                        except Exception:
                            pass
            return
        row = {"event": str(event), **payload}
        callback = context.get("progress_callback")
        if callable(callback):
            try:
                callback(dict(row))
            except Exception:
                pass
        append_audit = getattr(self, "_append_batch_audit", None)
        if callable(append_audit):
            try:
                append_audit({"event": "formal_suffix_progress", **row})
            except Exception:
                pass

    def _formal_suffix_code_fingerprint(self):
        root = os.path.dirname(os.path.abspath(__file__))
        names = (
            "gui_app.py",
            "formal_suffix_rewrite.py",
            "narrative_guard.py",
            "state_ledger.py",
            "state_repair.py",
            "continuity_guard.py",
            "temporal_memory.py",
            "knowledge_manager.py",
            "commercial_reviewer.py",
            "chapter_validator.py",
        )
        hashes = {}
        for name in names:
            path = os.path.join(root, name)
            if not os.path.isfile(path):
                hashes[name] = "missing"
                continue
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            hashes[name] = digest.hexdigest()
        return formal_suffix_rewrite.build_preflight_fingerprint(hashes)

    def _formal_suffix_safe_config(self):
        safe = {}
        for key, value in dict(self.config or {}).items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in (
                "api_key", "token", "password", "secret", "credential"
            )):
                continue
            safe[str(key)] = value
        return self._strip_preflight_volatile_fields(safe)

    def _formal_suffix_preflight_fingerprint(
        self,
        chap_num,
        chapter_content,
        chapter_outline,
    ):
        """Bind reusable model work to the exact prose and predecessor truth."""
        plot_dir = generator.DIRS["plot"]
        chapter = int(chap_num)
        state = state_ledger.load_state(plot_dir)
        canon = continuity_guard.load_canon(plot_dir)
        story_context = story_architect.build_story_context(
            plot_dir, chapter, max_chars=4500
        )
        canon_context = continuity_guard.build_canon_context(
            plot_dir, max_chars=4500
        )
        recent_context = knowledge_manager.build_generation_context(
            plot_dir, chapter, max_chars=2600, include_sources=False
        )
        state_context, _manifest = state_ledger.render_context(
            state,
            chapter,
            chapter_outline or "",
            "",
            max_chars=7000,
            stale_warn_chapters=max(
                3, int(self.config.get("subplot_stale_warn_chapters", 8) or 8)
            ),
        )
        temporal_context = ""
        if self.config.get("temporal_memory_enabled", True):
            try:
                hits = temporal_memory.retrieve(
                    plot_dir,
                    f"{chapter_outline or ''}\n{chapter_content or ''}",
                    before_chapter=chapter,
                    max_hits=max(
                        4,
                        int(self.config.get("temporal_memory_max_hits", 24) or 24),
                    ),
                    max_chars=max(
                        800,
                        int(self.config.get("temporal_memory_max_chars", 2800) or 2800),
                    ),
                )
                temporal_context = temporal_memory.render_retrieval(
                    hits,
                    max_chars=max(
                        800,
                        int(self.config.get("temporal_memory_max_chars", 2800) or 2800),
                    ),
                )
            except Exception as exc:
                temporal_context = f"UNAVAILABLE:{type(exc).__name__}:{str(exc)[:160]}"

        prior_audits = []
        audit_dir = os.path.join(plot_dir, "narrative_audits")
        drift_window = max(
            3, int(self.config.get("narrative_guard_drift_window", 5) or 5)
        )
        for previous in range(max(1, chapter - drift_window + 1), chapter):
            path = os.path.join(audit_dir, f"chapter_{previous:04d}.json")
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8-sig") as handle:
                    prior_audits.append(
                        self._strip_preflight_volatile_fields(json.load(handle))
                    )
            except Exception:
                prior_audits.append({"chapter": previous, "invalid": True})

        official_hashes = {}
        for number, path in sorted(self._official_chapter_paths_by_number().items()):
            if int(number) >= chapter:
                continue
            official_hashes[str(int(number))] = state_ledger.chapter_sha256(
                generator.read_text_exact(path)
            )

        commercial_sources = {}
        for relative in (
            "revision_debt.json",
            "commercial_blueprint.json",
            "商业蓝图.md",
            "商业审稿行动单.md",
            "每10章审稿清单.txt",
        ):
            path = os.path.join(plot_dir, relative)
            if os.path.isfile(path):
                commercial_sources[relative] = self._formal_suffix_source_digest(
                    path,
                    normalize_volatile=relative.lower().endswith(".json"),
                )

        payload = {
            "cache_schema": formal_suffix_rewrite.PREFLIGHT_CACHE_SCHEMA_VERSION,
            "chapter": chapter,
            "chapter_sha256": state_ledger.chapter_sha256(chapter_content),
            "outline_sha256": hashlib.sha256(
                str(chapter_outline or "").encode("utf-8")
            ).hexdigest(),
            "predecessor_state": self._strip_preflight_volatile_fields(state),
            "predecessor_canon": self._strip_preflight_volatile_fields(canon),
            "official_prefix_hashes": official_hashes,
            "prior_narrative_audits": prior_audits,
            "story_context": story_context,
            "canon_context": canon_context,
            "recent_context": recent_context,
            "state_context": state_context,
            "temporal_context": temporal_context,
            "commercial_contract": self._commercial_contract_context(max_chars=7000),
            "commercial_sources": commercial_sources,
            "review_model": self.get_review_model_name(),
            "provider": self.get_model_provider(),
            "config": self._formal_suffix_safe_config(),
            "code_fingerprint": self._formal_suffix_code_fingerprint(),
        }
        return formal_suffix_rewrite.build_preflight_fingerprint(payload)

    def _load_formal_suffix_preflight_stage(self, chapter, fingerprint, stage):
        if not self._formal_suffix_cache_active():
            return None
        return formal_suffix_rewrite.load_preflight_stage(
            self.project_dir,
            chapter=int(chapter),
            context_fingerprint=fingerprint,
            stage=stage,
        )

    def _store_formal_suffix_preflight_stage(
        self, chapter, fingerprint, stage, payload
    ):
        if not self._formal_suffix_cache_active():
            return ""
        return formal_suffix_rewrite.store_preflight_stage(
            self.project_dir,
            chapter=int(chapter),
            context_fingerprint=fingerprint,
            stage=stage,
            payload=payload,
        )

    def _formal_suffix_state_repair_arguments(self, chapter, fingerprint):
        if not fingerprint:
            return {}
        # Failure history is required even when approval reuse is disabled.
        return self._durable_state_repair_arguments(chapter, fingerprint, "structured_state_rejected")

    def _durable_state_repair_arguments(self, chapter, fingerprint, stage):
        binding = {"chapter": int(chapter), "context_fingerprint": fingerprint}
        pending_key = (os.path.abspath(self.project_dir), int(chapter), fingerprint, stage)
        pending_rows = getattr(self, "_state_repair_pending_rejections", None)
        if pending_rows is None:
            pending_rows = self._state_repair_pending_rejections = {}
        marker_stage = stage + "_pending"

        def load(name):
            return formal_suffix_rewrite.load_preflight_stage(
                self.project_dir, **binding, stage=name, strict=True
            )

        def verified_store(name, payload):
            formal_suffix_rewrite.store_preflight_stage(
                self.project_dir, **binding, stage=name, payload=payload
            )
            if load(name) != payload:
                raise OSError("状态修复记录写入后读回不一致")

        try:
            hint = load(stage)
            marker = load(marker_stage)
            if marker is not None and (
                not isinstance(marker, dict) or marker.get("status") not in {"PENDING", "RESOLVED"}
                or not re.fullmatch(r"[0-9a-f]{64}", str(marker.get("payload_sha256", "")))
            ):
                raise ValueError("待补存状态记录标记无效")
            pending = pending_rows.get(pending_key)
            if pending is not None or (marker and marker["status"] == "PENDING"):
                expected = state_repair.digest(pending) if pending is not None else marker["payload_sha256"]
                if pending is not None:
                    # Retry the actual failed payload, not a tiny probe. Retain
                    # it in memory until both persistence and readback succeed.
                    verified_store(stage, pending)
                    hint = load(stage)
                if hint is None or state_repair.digest(hint) != expected:
                    raise OSError("真实失败记录尚未补存；重启后缺少原记录，需恢复记录，不能仅靠探针解锁")
                verified_store(marker_stage, {"status": "RESOLVED", "payload_sha256": expected})
                pending_rows.pop(pending_key, None)
            if pending_rows:
                raise OSError("另一个绑定仍有未补存的状态失败记录")
            # A fresh nonce distinguishes a successful write from an old file.
            verified_store("state_repair_storage_probe", {"nonce": str(time.monotonic_ns())})
        except Exception as exc:
            self._state_repair_persistence_fault = (
                "STATE_REPAIR_STORAGE_FAILED：状态失败记录存储检查未通过；"
                "已停止，修复存储后再试，不调用模型或重写正文。"
            )
            raise state_ledger.StateExtractionError(self._state_repair_persistence_fault) from exc
        self._state_repair_persistence_fault = ""

        def store_rejection(payload):
            if not isinstance(payload, dict) or payload.get("status") != "REJECTED":
                raise ValueError("状态修复缓存只接受REJECTED反馈")
            pending_rows[pending_key] = copy.deepcopy(payload)
            marker = {"status": "PENDING", "payload_sha256": state_repair.digest(payload)}
            # A compact durable intent prevents a restart from erasing a failed
            # larger write. It is not evidence and can never approve a chapter.
            verified_store(marker_stage, marker)
            verified_store(stage, payload)
            verified_store(marker_stage, {**marker, "status": "RESOLVED"})
            pending_rows.pop(pending_key, None)

        return {"repair_context": hint, "rejection_callback": store_rejection}

    def _handle_state_preparation_failure(self, chapter, content, error, reason, pause, rewrite):
        if not isinstance(error, state_ledger.StateProseConflictError):
            try:
                self._remember_state_only_candidate(chapter, content)
            except Exception as exc:
                self._append_batch_audit({"event": "state_candidate_marker_failed",
                                          "chapter": chapter, "error": type(exc).__name__})
            pause(reason, content)
        else:
            rewrite(reason, content)

    def _remember_state_only_candidate(self, chapter, content):
        """A durable retry hint is not approval and never changes formal text."""
        body_hash = state_ledger.chapter_sha256(content)
        formal_suffix_rewrite.store_preflight_stage(
            self.project_dir, chapter=chapter,
            context_fingerprint=state_repair.digest({"chapter": chapter, "body": body_hash}),
            stage="state_only_candidate",
            payload={"chapter_sha256": body_hash, "scope": "state_only"},
        )

    def _load_state_only_candidate(self, chapter, draft_path):
        if not os.path.isfile(draft_path):
            return None
        content = generator.read_text_exact(draft_path)
        body_hash = state_ledger.chapter_sha256(content)
        marker = formal_suffix_rewrite.load_preflight_stage(
            self.project_dir, chapter=chapter,
            context_fingerprint=state_repair.digest({"chapter": chapter, "body": body_hash}),
            stage="state_only_candidate",
        )
        if marker == {"chapter_sha256": body_hash, "scope": "state_only"}:
            return content
        return None

    def _manual_chapter_state_repair_arguments(
        self, chapter, content, outline, suffix_fingerprint=""
    ):
        """Persist rejected extraction hints, never approvals, for manual appends."""
        if not self.config.get("structured_state_enabled", True):
            return {}
        if suffix_fingerprint:
            return self._formal_suffix_state_repair_arguments(chapter, suffix_fingerprint)
        try:
            fingerprint = self._formal_suffix_preflight_fingerprint(chapter, content, outline)
        except Exception as exc:
            self._append_batch_audit({
                "event": "manual_state_repair_cache_unavailable",
                "chapter": int(chapter), "error": type(exc).__name__,
            })
            raise state_ledger.StateExtractionError(
                "STATE_REPAIR_STORAGE_FAILED：状态修复绑定无法读取；停止调用并保留正文。"
            ) from exc
        return self._durable_state_repair_arguments(chapter, fingerprint, "manual_structured_state_rejected")

    def _validate_cached_narrative_preflight(
        self, payload, chap_num, chapter_content
    ):
        if not isinstance(payload, dict):
            raise narrative_guard.NarrativeGuardError("缓存叙事审计结构无效")
        if int(payload.get("chapter") or 0) != int(chap_num):
            raise narrative_guard.NarrativeGuardError("缓存叙事审计章号不一致")
        narrative_guard.verify_review_receipt(
            payload,
            chapter_content,
            expected_review_model=self.get_review_model_name(),
        )
        raw_payload = narrative_guard.parse_json_object(
            str(payload.get("review_raw_response") or "")
        )
        normalized, issues = narrative_guard.validate_audit(
            raw_payload,
            chapter_text=chapter_content,
            requirements=payload.get("outline_requirements") or [],
            min_score=int(self.config.get("narrative_guard_min_score", 75) or 75),
        )
        if issues or str(normalized.get("verdict") or "").upper() != "PASS":
            raise narrative_guard.NarrativeGuardError(
                "缓存叙事审计不再满足当前门槛：" + "；".join(issues[:6])
            )
        previous = narrative_guard.load_recent_audits(
            generator.DIRS["plot"],
            before_chapter=int(chap_num),
            limit=max(
                2,
                int(self.config.get("narrative_guard_drift_window", 5) or 5) - 1,
            ),
        )
        drift = narrative_guard.evaluate_rolling_drift(
            payload,
            previous,
            marginal_score=int(
                self.config.get("narrative_guard_marginal_score", 82) or 82
            ),
            max_consecutive_marginal=int(
                self.config.get("narrative_guard_max_consecutive_marginal", 2) or 2
            ),
        )
        if drift:
            raise narrative_guard.NarrativeGuardError(
                "缓存叙事审计滚动门槛失效：" + "；".join(drift[:6])
            )
        return payload

    def _validate_cached_state_preflight(
        self, payload, chap_num, chapter_content, state
    ):
        if not isinstance(payload, dict):
            raise state_ledger.StateLedgerError("缓存状态预检结构无效")
        expected_hash = state_ledger.chapter_sha256(chapter_content)
        if (
            int(payload.get("chapter") or 0) != int(chap_num)
            or payload.get("chapter_sha256") != expected_hash
            or payload.get("already_committed")
            or not isinstance(payload.get("delta"), dict)
        ):
            raise state_ledger.StateLedgerError("缓存状态预检身份不一致")
        normalized, issues = state_ledger.validate_delta(
            payload.get("delta"), int(chap_num), chapter_content, state,
            progression_context=continuity_guard.load_canon(generator.DIRS["plot"]) or {},
        )
        if issues:
            raise state_ledger.StateLedgerError(
                "缓存状态预检不再满足当前账本：" + "；".join(issues[:8])
            )
        result = dict(payload)
        result["delta"] = normalized
        return result

    @staticmethod
    def _validate_cached_release_guard_preflight(
        payload, chap_num, chapter_content
    ):
        if not isinstance(payload, dict):
            raise RuntimeError("缓存发布设定总校结构无效")
        expected_hash = state_ledger.chapter_sha256(chapter_content)
        if (
            int(payload.get("chapter") or 0) != int(chap_num)
            or str(payload.get("chapter_sha256") or "") != expected_hash
            or str(payload.get("status") or "").upper() != "PASS"
        ):
            raise RuntimeError("缓存发布设定总校身份或结论无效")
        return dict(payload)

    def _capture_formal_suffix_reusable_evidence(self, start_chapter, end_chapter):
        """Capture old hash-bound evidence before a micro-edit rewinds the suffix."""
        plot_dir = os.path.join(self.project_dir, "plot")
        state_deltas = {}
        narrative_audits = {}
        for chapter in range(int(start_chapter), int(end_chapter) + 1):
            delta_path = os.path.join(
                plot_dir, "state_deltas", f"chapter_{chapter:04d}.json"
            )
            try:
                with open(delta_path, "r", encoding="utf-8-sig") as handle:
                    delta_payload = json.load(handle)
            except Exception:
                delta_payload = {}
            if isinstance(delta_payload.get("delta"), dict):
                state_deltas[chapter] = copy.deepcopy(delta_payload["delta"])

            audit_path = os.path.join(
                plot_dir, "narrative_audits", f"chapter_{chapter:04d}.json"
            )
            try:
                with open(audit_path, "r", encoding="utf-8-sig") as handle:
                    audit_payload = json.load(handle)
            except Exception:
                audit_payload = {}
            if isinstance(audit_payload, dict) and audit_payload:
                narrative_audits[chapter] = copy.deepcopy(audit_payload)
        return {
            "state_deltas": state_deltas,
            "narrative_audits": narrative_audits,
        }

    def _reuse_micro_edit_narrative_audit(self, chap_num, chapter_content):
        context = getattr(self, "_formal_suffix_rewrite_context", None)
        if not isinstance(context, dict) or not context.get("micro_edit"):
            return None
        payload = (context.get("reusable_narrative_audits") or {}).get(
            int(chap_num)
        )
        if not isinstance(payload, dict):
            return None
        try:
            result = self._validate_cached_narrative_preflight(
                copy.deepcopy(payload), int(chap_num), chapter_content
            )
        except Exception as exc:
            self._formal_suffix_emit_progress(
                "preflight_evidence_reuse_rejected",
                chapter=int(chap_num),
                stage="narrative_audit",
                error=str(exc)[:300],
            )
            return None
        self._formal_suffix_emit_progress(
            "preflight_evidence_reused",
            chapter=int(chap_num),
            stage="narrative_audit",
        )
        return result

    def _reuse_micro_edit_state_delta(
        self, chap_num, chapter_content, state
    ):
        context = getattr(self, "_formal_suffix_rewrite_context", None)
        if not isinstance(context, dict) or not context.get("micro_edit"):
            return None
        source = (context.get("reusable_state_deltas") or {}).get(int(chap_num))
        if not isinstance(source, dict):
            return None
        proposed = copy.deepcopy(source)
        normalized, issues = state_ledger.validate_delta(
            proposed, int(chap_num), chapter_content, state
        )
        pruned_paths = []
        if issues:
            proposed, pruned_paths = state_ledger.prune_unlocatable_evidence_items(
                proposed, issues
            )
            normalized, issues = state_ledger.validate_delta(
                proposed, int(chap_num), chapter_content, state
            )
        if issues:
            self._formal_suffix_emit_progress(
                "preflight_evidence_reuse_rejected",
                chapter=int(chap_num),
                stage="structured_state",
                error="；".join(issues[:6])[:500],
            )
            return None
        result = {
            "chapter": int(chap_num),
            "chapter_sha256": state_ledger.chapter_sha256(chapter_content),
            "already_committed": False,
            "delta": normalized,
        }
        self._formal_suffix_emit_progress(
            "preflight_evidence_reused",
            chapter=int(chap_num),
            stage="structured_state",
            pruned_paths=list(pruned_paths or []),
        )
        return result

    def _reuse_micro_edit_release_guard(self, chap_num, chapter_content):
        context = getattr(self, "_formal_suffix_rewrite_context", None)
        if not isinstance(context, dict) or not context.get("micro_edit"):
            return None
        unchanged = set(context.get("micro_unchanged_chapters") or [])
        if int(chap_num) not in unchanged:
            return None
        audit = self._reuse_micro_edit_narrative_audit(chap_num, chapter_content)
        if audit is None:
            return None
        self._formal_suffix_emit_progress(
            "preflight_evidence_reused",
            chapter=int(chap_num),
            stage="release_guard",
        )
        return {
            "status": "PASS",
            "summary": "正文未改动，复用原正式章哈希绑定证据。",
            "issues": [],
            "chapter": int(chap_num),
            "chapter_sha256": state_ledger.chapter_sha256(chapter_content),
            "evidence_reused": True,
        }

    def _commit_manual_chapter(self, chap_num, chapter_path, content, history_label):
        """Manual text must pass the same narrative and evidence gates as automation."""
        content = (content or "").strip()
        char_limits = self._get_chapter_char_limits()
        chinese_chars = repair_engine.chinese_char_count(content)
        if chinese_chars < int(char_limits["min"]):
            raise RuntimeError(
                f"第{chap_num}章仅{chinese_chars}字，低于正式章硬下限{char_limits['min']}字"
            )
        if chinese_chars > int(char_limits["target_max"]):
            raise RuntimeError(
                f"第{chap_num}章共{chinese_chars}字，超过正式章上限{char_limits['target_max']}字；"
                "请整章压缩重写，不能追加或截断"
            )
        repetition_issues = self._detect_chapter_repetition(content, chap_num)
        if repetition_issues:
            raise RuntimeError("正文确定性检查未通过：" + "；".join(repetition_issues[:8]))
        candidate_report = chapter_validator.validate_candidate(
            self.project_dir or os.path.dirname(generator.DIRS["plot"]),
            chap_num,
            content,
            char_limits,
        )
        if not candidate_report.get("passed"):
            raise RuntimeError(
                "最终正文验收未通过："
                + "；".join(
                    str(item.get("message") or item.get("code") or "")
                    for item in (candidate_report.get("issues") or [])[:8]
                )
            )
        validation_receipt = chapter_validator.build_validation_receipt(
            candidate_report
        )
        semantic_report = self._run_semantic_consistency_guard(content, chap_num)
        if not semantic_report.get("passed"):
            raise RuntimeError(
                "语义一致性硬门未通过：" + self._semantic_guard_message(semantic_report)
            )
        canon_check = continuity_guard.validate_chapter(
            content, chap_num, generator.DIRS["plot"]
        )
        if canon_check.get("status") != "PASS":
            raise RuntimeError("正史连续性未通过：" + str(canon_check.get("summary") or ""))
        outline = self._extract_chapter_outline(chap_num)
        strict_release = self._strict_release_mode()
        cache_fingerprint = ""
        if strict_release and self._formal_suffix_cache_active():
            self._formal_suffix_emit_progress(
                "chapter_preflight_started", chapter=int(chap_num)
            )
            try:
                cache_fingerprint = self._formal_suffix_preflight_fingerprint(
                    chap_num, content, outline
                )
            except Exception as exc:
                self._formal_suffix_emit_progress(
                    "preflight_cache_unavailable",
                    chapter=int(chap_num),
                    error=str(exc)[:300],
                )

        release_guard = self._reuse_micro_edit_release_guard(chap_num, content)
        if strict_release and cache_fingerprint:
            cached_release_guard = self._load_formal_suffix_preflight_stage(
                chap_num, cache_fingerprint, "release_guard"
            )
            if cached_release_guard is not None:
                try:
                    release_guard = self._validate_cached_release_guard_preflight(
                        cached_release_guard, chap_num, content
                    )
                except Exception as exc:
                    self._formal_suffix_emit_progress(
                        "preflight_cache_invalid",
                        chapter=int(chap_num),
                        stage="release_guard",
                        error=str(exc)[:300],
                    )
                    release_guard = None
                else:
                    self._formal_suffix_emit_progress(
                        "preflight_cache_hit",
                        chapter=int(chap_num),
                        stage="release_guard",
                    )
        if release_guard is None:
            if strict_release:
                self._formal_suffix_emit_progress(
                    "preflight_model_started",
                    chapter=int(chap_num),
                    stage="release_guard",
                )
            release_guard = self._run_release_guard(content, chap_num)
            if strict_release:
                self._formal_suffix_emit_progress(
                    "preflight_model_completed",
                    chapter=int(chap_num),
                    stage="release_guard",
                )
                if (
                    cache_fingerprint
                    and str(release_guard.get("status") or "").upper() == "PASS"
                ):
                    release_guard_payload = {
                        **dict(release_guard),
                        "chapter": int(chap_num),
                        "chapter_sha256": state_ledger.chapter_sha256(content),
                    }
                    try:
                        self._store_formal_suffix_preflight_stage(
                            chap_num,
                            cache_fingerprint,
                            "release_guard",
                            release_guard_payload,
                        )
                    except Exception as exc:
                        self._formal_suffix_emit_progress(
                            "preflight_cache_store_failed",
                            chapter=int(chap_num),
                            stage="release_guard",
                            error=str(exc)[:300],
                        )

        release_guard_warning = ""
        if (
            release_guard.get("status") == "FAIL"
            or (strict_release and release_guard.get("status") != "PASS")
        ):
            raise RuntimeError(
                "发布前设定总校未通过：" + str(release_guard.get("summary") or "未知问题")
            )
        if release_guard.get("status") == "WARN":
            release_guard_warning = str(release_guard.get("summary") or "")
        self._begin_chapter_model_budget(chap_num)
        if strict_release:
            self._verify_existing_guarded_audit_history()
        current_state = (
            state_ledger.load_state(generator.DIRS["plot"])
            if strict_release
            else {"current_chapter": 0, "applied_chapters": {}}
        )
        ledger_hash = (
            str(current_state.get("applied_chapters", {}).get(str(chap_num)) or "")
            if strict_release
            else ""
        )
        file_hash = ""
        if os.path.isfile(chapter_path):
            file_hash = state_ledger.chapter_sha256(
                generator.read_text_exact(chapter_path)
            )
        if strict_release and ledger_hash and not file_hash:
            raise state_ledger.StateLedgerError(
                f"第{chap_num}章正史账本已存在，但正式正文文件缺失，拒绝手动覆盖"
            )
        if strict_release and ledger_hash and file_hash and ledger_hash != file_hash:
            raise state_ledger.StateLedgerError(
                f"第{chap_num}章正式正文与证据正史哈希不一致，拒绝手动覆盖"
            )
        if (
            strict_release
            and
            file_hash
            and not ledger_hash
            and int(current_state.get("current_chapter", 0) or 0) >= int(chap_num)
        ):
            raise state_ledger.StateLedgerError(
                f"第{chap_num}章已有正式正文但缺少对应证据账本，请先运行体检补齐"
            )
        existing_hash = file_hash or ledger_hash
        replacement = bool(
            file_hash and file_hash != state_ledger.chapter_sha256(content)
        )
        if replacement:
            if strict_release:
                if int(current_state.get("current_chapter", 0)) != int(chap_num):
                    raise state_ledger.StateLedgerError(
                        "只能续写或改写最后一章；当前章之后已有正式正史，不能局部覆盖"
                    )
            else:
                official_numbers = sorted(self._official_chapter_paths_by_number())
                if official_numbers and int(chap_num) != int(official_numbers[-1]):
                    raise RuntimeError("只能改写当前最后一章，不能越过后续正式章节")
        if replacement:
            old_status = self._load_latest_chapter_statuses().get(int(chap_num)) or {}
            if strict_release:
                try:
                    narrative_guard.verify_persisted_audit(
                        generator.DIRS["plot"],
                        int(chap_num),
                        generator.read_text_safe(chapter_path),
                        expected_audit_id=str(old_status.get("narrative_audit_id") or ""),
                        expected_review_model=self._narrative_review_model_for_chapter(
                            chap_num, old_status
                        ),
                        min_score=int(
                            self.config.get("narrative_guard_min_score", 75) or 75
                        ),
                    )
                except Exception as exc:
                    # An invalid old receipt is a reason to replace the last chapter,
                    # not a reason to preserve unsafe prose.  The new candidate still
                    # has to pass every current gate before it can become official.
                    self._append_batch_audit({
                        "event": "invalid_old_audit_replacement_allowed",
                        "chapter": int(chap_num),
                        "error": str(exc)[:500],
                    })
        if (
            replacement
            and self.config.get("canon_guard_enabled", True)
            and not continuity_guard.has_canon_snapshot_before(
                generator.DIRS["plot"], chap_num
            )
        ):
            raise RuntimeError(
                f"缺少第{chap_num - 1}章正史快照，不能安全覆盖第{chap_num}章；"
                "请保留原章并从下一章续写"
            )
        state_override = (
            state_ledger.build_state_before_chapter(generator.DIRS["plot"], chap_num)
            if strict_release and replacement else None
        )
        narrative_audit_result = None
        if strict_release:
            if cache_fingerprint:
                cached_narrative = self._load_formal_suffix_preflight_stage(
                    chap_num, cache_fingerprint, "narrative_audit"
                )
                if cached_narrative is not None:
                    try:
                        narrative_audit_result = (
                            self._validate_cached_narrative_preflight(
                                cached_narrative, chap_num, content
                            )
                        )
                    except Exception as exc:
                        self._formal_suffix_emit_progress(
                            "preflight_cache_invalid",
                            chapter=int(chap_num),
                            stage="narrative_audit",
                            error=str(exc)[:300],
                        )
                        narrative_audit_result = None
                    else:
                        self._formal_suffix_emit_progress(
                            "preflight_cache_hit",
                            chapter=int(chap_num),
                            stage="narrative_audit",
                        )
            if narrative_audit_result is None:
                narrative_audit_result = self._reuse_micro_edit_narrative_audit(
                    chap_num, content
                )
            if narrative_audit_result is None:
                self._formal_suffix_emit_progress(
                    "preflight_model_started",
                    chapter=int(chap_num),
                    stage="narrative_audit",
                )
                narrative_audit_result = self._run_narrative_quality_guard(
                    chap_num,
                    content,
                    outline,
                )
                self._formal_suffix_emit_progress(
                    "preflight_model_completed",
                    chapter=int(chap_num),
                    stage="narrative_audit",
                )
                if cache_fingerprint:
                    try:
                        self._store_formal_suffix_preflight_stage(
                            chap_num,
                            cache_fingerprint,
                            "narrative_audit",
                            narrative_audit_result,
                        )
                    except Exception as exc:
                        self._formal_suffix_emit_progress(
                            "preflight_cache_store_failed",
                            chapter=int(chap_num),
                            stage="narrative_audit",
                            error=str(exc)[:300],
                        )
        else:
            local_issues = narrative_guard.deterministic_issues(content)
            if local_issues:
                raise RuntimeError(
                    "正文确定性叙事检查未通过：" + "；".join(local_issues[:8])
                )
        prepared_state_delta = None
        if strict_release:
            if cache_fingerprint:
                cached_state = self._load_formal_suffix_preflight_stage(
                    chap_num, cache_fingerprint, "structured_state"
                )
                if cached_state is not None:
                    try:
                        prepared_state_delta = self._validate_cached_state_preflight(
                            cached_state,
                            chap_num,
                            content,
                            (
                                state_override
                                if isinstance(state_override, dict)
                                else current_state
                            ),
                        )
                    except Exception as exc:
                        self._formal_suffix_emit_progress(
                            "preflight_cache_invalid",
                            chapter=int(chap_num),
                            stage="structured_state",
                            error=str(exc)[:300],
                        )
                        prepared_state_delta = None
                    else:
                        self._formal_suffix_emit_progress(
                            "preflight_cache_hit",
                            chapter=int(chap_num),
                            stage="structured_state",
                        )
            if prepared_state_delta is None:
                prepared_state_delta = self._reuse_micro_edit_state_delta(
                    chap_num,
                    content,
                    (
                        state_override
                        if isinstance(state_override, dict)
                        else current_state
                    ),
                )
            if prepared_state_delta is None:
                self._formal_suffix_emit_progress(
                    "preflight_model_started",
                    chapter=int(chap_num),
                    stage="structured_state",
                )
                prepared_state_delta = self._prepare_structured_state_delta(
                    chap_num, content, outline, state_override=state_override,
                    **self._manual_chapter_state_repair_arguments(
                        chap_num, content, outline, cache_fingerprint
                    ),
                )
                self._formal_suffix_emit_progress(
                    "preflight_model_completed",
                    chapter=int(chap_num),
                    stage="structured_state",
                )
                if cache_fingerprint and prepared_state_delta:
                    try:
                        self._store_formal_suffix_preflight_stage(
                            chap_num,
                            cache_fingerprint,
                            "structured_state",
                            prepared_state_delta,
                        )
                    except Exception as exc:
                        self._formal_suffix_emit_progress(
                            "preflight_cache_store_failed",
                            chapter=int(chap_num),
                            stage="structured_state",
                            error=str(exc)[:300],
                        )
        commercial_review_result = None
        chapter_review_notes = [
            (
                "手动保存已通过严格主线审计、证据正史与确定性硬检查"
                if strict_release
                else "手动保存已通过字数、结构、重复、语义与正史确定性硬检查"
            )
        ]
        if release_guard_warning:
            chapter_review_notes.append(
                "发布风格提示（不阻塞正文）：" + release_guard_warning[:500]
            )
        volume_ranges = self._get_volume_ranges()
        commercial_should_run = commercial_reviewer.should_run(
            chap_num, self.config, volume_ranges
        )
        if commercial_should_run:
            commercial_cache_hit = False
            try:
                if cache_fingerprint:
                    cached_commercial = self._load_formal_suffix_preflight_stage(
                        chap_num, cache_fingerprint, "commercial_review"
                    )
                    if cached_commercial is not None:
                        try:
                            commercial_reviewer.verify_review_receipt(
                                cached_commercial,
                                self._commercial_review_model_for_result(
                                    cached_commercial
                                ),
                            )
                        except Exception as exc:
                            self._formal_suffix_emit_progress(
                                "preflight_cache_invalid",
                                chapter=int(chap_num),
                                stage="commercial_review",
                                error=str(exc)[:300],
                            )
                        else:
                            commercial_review_result = cached_commercial
                            commercial_cache_hit = True
                            self._formal_suffix_emit_progress(
                                "preflight_cache_hit",
                                chapter=int(chap_num),
                                stage="commercial_review",
                            )
                if commercial_review_result is None:
                    self._formal_suffix_emit_progress(
                        "preflight_model_started",
                        chapter=int(chap_num),
                        stage="commercial_review",
                    )
                    commercial_review_result = self._run_commercial_stage_review(
                        content, chap_num, persist_report=False
                    )
                    self._formal_suffix_emit_progress(
                        "preflight_model_completed",
                        chapter=int(chap_num),
                        stage="commercial_review",
                    )
                debt_path = os.path.join(generator.DIRS["plot"], "revision_debt.json")
                debt_blocked = commercial_reviewer.hard_gate_debt_blocked(
                    chap_num,
                    commercial_review_result,
                    commercial_reviewer.load_revision_debt(debt_path),
                    self.config,
                    volume_ranges,
                )
                commercial_review_result["gate_blocked"] = bool(debt_blocked)
                should_pause = commercial_reviewer.should_pause_after_review(
                    chap_num,
                    commercial_review_result.get("status"),
                    commercial_review_result.get("action"),
                    self.config,
                    volume_ranges=volume_ranges,
                )
                if cache_fingerprint and not commercial_cache_hit:
                    try:
                        commercial_reviewer.verify_review_receipt(
                            commercial_review_result,
                            self._commercial_review_model_for_result(
                                commercial_review_result
                            ),
                        )
                        self._store_formal_suffix_preflight_stage(
                            chap_num,
                            cache_fingerprint,
                            "commercial_review",
                            commercial_review_result,
                        )
                    except Exception as exc:
                        self._formal_suffix_emit_progress(
                            "preflight_cache_store_failed",
                            chapter=int(chap_num),
                            stage="commercial_review",
                            error=str(exc)[:300],
                        )
                if debt_blocked or should_pause:
                    summary = str(
                        commercial_review_result.get("summary")
                        or commercial_review_result.get("gate_block_reason")
                        or "未取得明确 PASS/CONTINUE"
                    )
                    if cache_fingerprint:
                        try:
                            self._store_formal_suffix_preflight_stage(
                                chap_num,
                                cache_fingerprint,
                                "commercial_review_rejected",
                                {
                                    "schema_version": 1,
                                    "status": "REJECTED",
                                    "chapter": int(chap_num),
                                    "reason": (
                                        "revision_debt_blocked"
                                        if debt_blocked else "review_pause"
                                    ),
                                    "review_result": commercial_review_result,
                                },
                            )
                        except Exception as exc:
                            self._formal_suffix_emit_progress(
                                "preflight_cache_store_failed",
                                chapter=int(chap_num),
                                stage="commercial_review",
                                error=str(exc)[:300],
                            )
                    if strict_release:
                        raise RuntimeError("阶段商业硬闸门未通过：" + summary)
                    chapter_review_notes.append(
                        "阶段商业审稿未通过，但正文已允许正式入库：" + summary[:500]
                    )
            except Exception as exc:
                if strict_release:
                    raise
                commercial_review_result = None
                chapter_review_notes.append(
                    f"阶段商业审稿暂不可用，不阻塞正文入库：{str(exc)[:300]}"
                )
                self._append_batch_audit({
                    "event": "simple_release_commercial_unavailable",
                    "chapter": int(chap_num),
                    "error": str(exc)[:800],
                })
        commercial_clear_pass = self._commercial_review_is_clear_pass(
            commercial_review_result
        )
        bind_commercial_review = bool(
            commercial_review_result and (strict_release or commercial_clear_pass)
        )
        chapter_commit.begin(
            generator.DIRS["plot"],
            chapter=chap_num,
            chapter_path=chapter_path,
            chapter_text=content,
            chapter_outline=outline,
            prepared_state_delta=prepared_state_delta,
            narrative_audit_result=narrative_audit_result,
            narrative_audit_required=strict_release,
            commercial_review_result=(
                commercial_review_result if bind_commercial_review else None
            ),
            commercial_review_required=bool(
                commercial_should_run and (strict_release or commercial_clear_pass)
            ),
            commercial_hard_gate=bool(
                commercial_reviewer.is_hard_gate(
                    chap_num, self.config, volume_ranges
                )
                and (strict_release or commercial_clear_pass)
            ),
            commercial_expected_chapters=(
                list((commercial_review_result or {}).get("expected_chapters") or [])
                if bind_commercial_review
                else []
            ),
            commercial_review_model=(
                self._commercial_review_model_for_result(commercial_review_result)
                if bind_commercial_review
                else ""
            ),
            validation_receipt=validation_receipt,
            char_limits=char_limits,
            chapter_status="正式可用",
            chapter_review_notes=chapter_review_notes,
            replacement=replacement,
            previous_chapter_sha256=str(existing_hash or ""),
        )
        _atomic_write_text(chapter_path, content)
        chapter_commit.mark_step(generator.DIRS["plot"], "chapter_saved")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        history_path = os.path.join(
            generator.DIRS["hist"],
            f"Ch{chap_num}_{history_label}_{timestamp}.txt",
        )
        _atomic_write_text(
            history_path,
            self.current_req + "\n\n" + "-" * 40 + "\n\n" + content,
        )
        self._recover_pending_chapter_commit()
        self.refresh_status()
        self.refresh_reader_files_silent()

    def _prepare_formal_suffix_rewrite(self, start_chapter, end_chapter):
        """Rewind current formal state so the reviewed tail can be recommitted."""
        start = int(start_chapter)
        end = int(end_chapter)
        plot_dir = generator.DIRS["plot"]
        official = self._official_chapter_paths_by_number()
        if not official or max(official) != end:
            raise RuntimeError("正式尾段范围已变化，拒绝继续重写")
        if any(chapter not in official for chapter in range(start, end + 1)):
            raise RuntimeError("正式尾段正文不连续")

        state = state_ledger.load_state(plot_dir)
        if int(state.get("current_chapter") or 0) != end:
            raise RuntimeError(
                f"状态账本停在第{int(state.get('current_chapter') or 0)}章，"
                f"与尾段末章{end}不一致"
            )
        applied_hashes = dict(state.get("applied_chapters") or {})
        old_hashes = {}
        for chapter in range(start, end + 1):
            ledger_hash = str(applied_hashes.get(str(chapter)) or "")
            if not ledger_hash:
                raise RuntimeError(f"第{chapter}章缺少旧状态账本哈希，不能安全回退")
            old_hashes[chapter] = ledger_hash

        audit_dir = os.path.join(plot_dir, "narrative_audits")
        receipt_dir = os.path.join(plot_dir, "runtime", "commit_receipts")
        superseded_receipts = os.path.join(receipt_dir, "superseded")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

        for chapter in range(end, start - 1, -1):
            state_ledger.supersede_last_chapter_delta(
                plot_dir,
                chapter,
                expected_chapter_sha256=old_hashes[chapter],
            )
            continuity_guard.restore_before_chapter(plot_dir, chapter)
            audit_path = os.path.join(audit_dir, f"chapter_{chapter:04d}.json")
            if os.path.isfile(audit_path):
                narrative_guard.supersede_audit(
                    plot_dir,
                    chapter,
                    expected_chapter_sha256=old_hashes[chapter],
                )
            if os.path.isdir(receipt_dir):
                matches = glob.glob(
                    os.path.join(receipt_dir, f"chapter_{chapter:04d}_*.json")
                )
                if matches:
                    os.makedirs(superseded_receipts, exist_ok=True)
                for source in matches:
                    target = os.path.join(
                        superseded_receipts,
                        f"{os.path.splitext(os.path.basename(source))[0]}_{stamp}.json",
                    )
                    os.replace(source, target)

        if self.config.get("temporal_memory_enabled", True):
            temporal_memory.rebuild_from_deltas(plot_dir)
        knowledge_manager.rebuild_memory_sources_from_structured_state(
            plot_dir,
            archive_tag=f"formal_suffix_before_{start:04d}_{stamp}",
        )
        knowledge_manager.truncate_fact_db_after(plot_dir, start - 1)

        for chapter in range(start, end + 1):
            path = official[chapter]
            if os.path.isfile(path):
                os.remove(path)
            self._remove_publish_copy(chapter)

    def rewrite_formal_suffix_from_drafts(
        self,
        start_chapter,
        end_chapter,
        draft_paths=None,
        progress_callback=None,
        micro_edit=False,
    ):
        """Review and recommit a contiguous formal tail as one transaction."""
        start = int(start_chapter)
        end = int(end_chapter)
        if draft_paths is None:
            draft_paths = {}
            for chapter in range(start, end + 1):
                volume = next(
                    (
                        index
                        for index, (first, last, _name) in enumerate(
                            self._get_volume_ranges(), start=1
                        )
                        if first <= chapter <= last
                    ),
                    self.current_vol,
                )
                draft_paths[chapter] = os.path.join(
                    self.project_dir,
                    "drafts",
                    f"第{volume:02d}卷",
                    f"第{chapter:04d}章.txt",
                )
        candidates = {int(key): str(value) for key, value in dict(draft_paths).items()}
        micro_edit_report = {}
        reusable_evidence = {"state_deltas": {}, "narrative_audits": {}}
        if micro_edit:
            candidates = {
                chapter: str(path)
                for chapter, path in formal_suffix_rewrite.resolve_sparse_micro_edit_candidates(
                    self.project_dir,
                    candidates,
                    start_chapter=start,
                    end_chapter=end,
                ).items()
            }
            micro_edit_report = formal_suffix_rewrite.validate_micro_edit_candidates(
                self.project_dir,
                candidates,
                max_changed_chars=int(
                    self.config.get("formal_micro_edit_max_changed_chars", 800) or 800
                ),
                max_changed_ratio=float(
                    self.config.get("formal_micro_edit_max_changed_ratio", 0.08) or 0.08
                ),
            )
            reusable_evidence = self._capture_formal_suffix_reusable_evidence(
                start, end
            )
        previous_req = self.current_req
        previous_context = getattr(self, "_formal_suffix_rewrite_context", None)
        owned_project_lock = None
        if getattr(self, "_project_job_lock", None) is None:
            owned_project_lock = project_job_lock.ProjectJobLock(self.project_dir)
            locked, reason = owned_project_lock.acquire(
                f"正式尾段重写第{start}-{end}章"
            )
            if not locked:
                raise RuntimeError(reason)

        progress_labels = {
            "snapshot_started": "正在建立全量回滚快照",
            "snapshot_completed": "回滚快照完成",
            "prepare_started": "正在回退旧尾段状态",
            "prepare_completed": "旧尾段已安全回退",
            "verification_started": "正在核验正式稿、状态账本与正史",
            "verification_completed": "正式尾段一致性核验完成",
            "rollback_started": "检测到失败，正在恢复事务前状态",
            "rollback_completed": "已恢复事务前状态；通过的预检缓存会保留",
            "rollback_failed": "回滚核验失败，需要人工检查",
            "completed": "正式尾段事务完成",
        }
        stage_labels = {
            "release_guard": "发布设定总校",
            "narrative_audit": "叙事审计",
            "structured_state": "状态提取与证据复核",
            "commercial_review": "阶段商业审稿",
        }

        def report_progress(row):
            event = str((row or {}).get("event") or "")
            chapter = int((row or {}).get("chapter") or 0)
            stage = stage_labels.get(str((row or {}).get("stage") or ""), "预检")
            if event == "chapter_started":
                message = (
                    f"第{chapter}章开始处理 "
                    f"({int(row.get('completed') or 0) + 1}/{int(row.get('total') or 0)})"
                )
            elif event == "chapter_completed":
                message = (
                    f"第{chapter}章已正式提交 "
                    f"({int(row.get('completed') or 0)}/{int(row.get('total') or 0)})"
                )
            elif event == "preflight_cache_hit":
                message = f"第{chapter}章{stage}命中安全缓存，跳过重复模型调用"
            elif event == "preflight_model_started":
                message = f"第{chapter}章正在执行{stage}"
            elif event == "preflight_model_completed":
                message = f"第{chapter}章{stage}调用结束（以阶段验收结果为准）"
            elif event in {"state_model_call_started", "state_model_call_completed", "state_model_call_failed"}:
                outcome = {"state_model_call_started": "调用开始", "state_model_call_completed": "调用返回（仍需验收）",
                           "state_model_call_failed": "调用异常"}[event]
                message = (f"第{chapter}章状态第{row.get('attempt')}轮 {row.get('phase')}：{outcome}"
                           + (f"，耗时{row['elapsed_seconds']}秒" if "elapsed_seconds" in row else ""))
            elif event == "preflight_cache_invalid":
                message = f"第{chapter}章{stage}缓存已失效，改为重新审查"
            elif event == "preflight_cache_unavailable":
                message = f"第{chapter}章缓存指纹不可用，本次按完整流程执行"
            elif event == "preflight_cache_store_failed":
                message = f"第{chapter}章{stage}缓存写入失败，不影响本次安全提交"
            elif event == "preflight_evidence_reused":
                message = f"第{chapter}章{stage}复用原正式证据，跳过重复模型调用"
            elif event == "preflight_evidence_reuse_rejected":
                message = f"第{chapter}章{stage}原证据不再适用，改为重新审查"
            else:
                message = progress_labels.get(event, "")
            append_ui = getattr(self, "_ui_progress_append", None)
            if message and callable(append_ui):
                try:
                    append_ui(f"[正式尾段] {message}...\n")
                except Exception:
                    pass
            if callable(progress_callback):
                try:
                    progress_callback(dict(row or {}))
                except Exception:
                    pass

        self._formal_suffix_rewrite_context = {
            "start_chapter": start,
            "end_chapter": end,
            "micro_edit": bool(micro_edit),
            "micro_unchanged_chapters": list(
                micro_edit_report.get("unchanged_chapters") or []
            ),
            "reusable_state_deltas": reusable_evidence.get("state_deltas") or {},
            "reusable_narrative_audits": reusable_evidence.get("narrative_audits") or {},
            "cache_enabled": bool(
                self.config.get("formal_suffix_preflight_cache_enabled", True)
            ),
            "progress_callback": report_progress,
        }

        def prepare(first, last):
            self._prepare_formal_suffix_rewrite(first, last)

        def commit(chapter, target, text):
            self.current_req = (
                f"正式尾段事务式重写：第{start}-{end}章；当前提交第{chapter}章。"
            )
            self._commit_manual_chapter(
                chapter,
                str(target),
                text,
                "正式尾段重写",
            )

        def verify(first, last):
            state = state_ledger.load_state(generator.DIRS["plot"])
            if int(state.get("current_chapter") or 0) != last:
                raise RuntimeError("尾段重写后状态账本末章不正确")
            canon = continuity_guard.load_canon(generator.DIRS["plot"])
            if int(canon.get("current_chapter") or 0) != last:
                raise RuntimeError("尾段重写后正史末章不正确")
            facts = knowledge_manager.load_fact_db(generator.DIRS["plot"])
            if int(facts.get("latest_chapter") or 0) != last:
                raise RuntimeError("尾段重写后事实库末章不正确")

        try:
            result = formal_suffix_rewrite.execute_formal_suffix_rewrite(
                self.project_dir,
                start_chapter=start,
                end_chapter=end,
                candidates=candidates,
                prepare_suffix=prepare,
                commit_chapter=commit,
                verify_suffix=verify,
                progress_callback=lambda row: self._formal_suffix_emit_progress(
                    str((row or {}).get("event") or ""),
                    **{
                        key: value
                        for key, value in dict(row or {}).items()
                        if key != "event"
                    },
                ),
            )
            result["preflight_cache_dir"] = os.path.join(
                self.project_dir,
                ".runtime",
                formal_suffix_rewrite.PREFLIGHT_CACHE_DIRNAME,
            )
            if micro_edit_report:
                result["micro_edit"] = micro_edit_report
            return result
        finally:
            self.current_req = previous_req
            if previous_context is None:
                try:
                    del self._formal_suffix_rewrite_context
                except AttributeError:
                    pass
            else:
                self._formal_suffix_rewrite_context = previous_context
            if owned_project_lock is not None:
                owned_project_lock.release()

    def save_new_chapter(self):
        content = self.strip_markdown_artifacts(self.result_text.get(1.0, tk.END).strip())
        if not content:
            return
        saved_chap = self.next_chap
        if self._strict_release_mode():
            guard = self._run_release_guard(content, saved_chap)
            if guard["status"] == "FAIL":
                messagebox.showerror("设定拦截", f"检测到高风险设定冲突，已阻止保存。\n\n{guard['summary']}")
                return
            if guard["status"] == "WARN":
                detail = ("\n\n详细提示：\n" + guard["raw"][:500]) if guard.get("raw") else ""
                messagebox.showerror(
                    "设定预警已拦截",
                    f"检测到潜在设定风险，必须修正后才能保存：\n\n"
                    f"{guard['summary']}{detail}",
                )
                return
        try:
            self._commit_manual_chapter(saved_chap, self.filepath, content, "手动新章")
        except Exception as exc:
            messagebox.showerror("保存未完全同步", f"正式稿写入或配套状态同步失败：\n{str(exc)}")
            return
        messagebox.showinfo("成功", f"第 {saved_chap} 章保存成功！")
        self.btn_save_new.config(state=tk.DISABLED)

    def save_append_chapter(self):
        content = self.strip_markdown_artifacts(self.result_text.get(1.0, tk.END).strip())
        if not content:
            return
        existing = generator.read_text_safe(self.latest_filepath) if self.latest_filepath else ""
        title_pattern = re.compile(
            r'(?m)^\s*第\s*([零〇一二三四五六七八九十百千万两\d]+)\s*章(?:\s|$)'
        )
        title_hits = list(title_pattern.finditer(content))
        first_line = content.split("\n", 1)[0].strip().lstrip("#").strip()
        same_chapter_full_rewrite = bool(
            title_hits
            and title_hits[0].start() == 0
            and re.match(rf'^第\s*0*{int(self.latest_chap)}\s*章(?:\s|$)', first_line)
        )
        if title_hits and not same_chapter_full_rewrite:
            messagebox.showerror(
                "已阻止拼接",
                "续写内容包含章节标题，无法判断它是片段还是另一版整章。\n\n"
                "请粘贴完整的当前章修订稿；不要把两版正文追加在一起。",
            )
            return

        limits = self._get_chapter_char_limits()
        existing_chars = repair_engine.chinese_char_count(existing)
        if same_chapter_full_rewrite:
            combined = content.strip()
            history_label = "手动整章替换"
        else:
            if existing_chars >= int(limits["min"]):
                messagebox.showerror(
                    "正式章不可追加",
                    f"第{self.latest_chap}章已有{existing_chars}字，已达到完整章节硬下限。\n\n"
                    "继续追加容易形成两版拼接稿。请改为粘贴带本章标题的完整修订稿。",
                )
                return
            combined = (existing.rstrip() + "\n\n" + content).strip()
            history_label = "手动补全未完成章"
        combined_chars = repair_engine.chinese_char_count(combined)
        if combined_chars > int(limits["target_max"]):
            messagebox.showerror(
                "已阻止超长拼接",
                f"合并后共{combined_chars}字，超过正式章上限{limits['target_max']}字。\n\n"
                "请压缩为一版完整章节后再保存。",
            )
            return
        if self._strict_release_mode():
            guard = self._run_release_guard(
                combined, self.latest_chap, prev_content=existing
            )
            if guard["status"] == "FAIL":
                messagebox.showerror("设定拦截", f"检测到高风险设定冲突，已阻止追加保存。\n\n{guard['summary']}")
                return
            if guard["status"] == "WARN":
                detail = ("\n\n详细提示：\n" + guard["raw"][:500]) if guard.get("raw") else ""
                messagebox.showerror(
                    "设定预警已拦截",
                    f"检测到潜在设定风险，必须修正后才能追加保存：\n\n"
                    f"{guard['summary']}{detail}",
                )
                return
        try:
            backup_dir = os.path.join(os.path.dirname(self.latest_filepath), ".backup")
            os.makedirs(backup_dir, exist_ok=True)
            backup_path = os.path.join(
                backup_dir,
                f"第{self.latest_chap:04d}章_before_manual_append_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            )
            _atomic_write_text(backup_path, existing)
            self._commit_manual_chapter(
                self.latest_chap,
                self.latest_filepath,
                combined,
                history_label,
            )
        except Exception as exc:
            messagebox.showerror("追加未完全同步", f"正式稿写入或配套状态同步失败：\n{str(exc)}")
            return
        messagebox.showinfo("成功", f"第 {self.latest_chap} 章已通过完整校验并保存！")
        self.btn_save_append.config(state=tk.DISABLED)

    # ============================================================
    # 自动保存
    # ============================================================
    def auto_save_loop(self):
        if not self._is_busy():
            current_text = self.result_text.get(1.0, tk.END).strip()
            if current_text and current_text != self.last_saved_text:
                autosave_path = os.path.join(generator.DIRS["hist"], "autosave_draft.txt")
                try:
                    _atomic_write_text(autosave_path, current_text)
                    self.last_saved_text = current_text
                except Exception:
                    pass
        self.root.after(60000, self.auto_save_loop)

    # ============================================================
    # 工具箱：记忆压缩
    # ============================================================
    def compress_memory(self):
        if not self.latest_filepath or not os.path.exists(self.latest_filepath):
            messagebox.showwarning("提示", "尚无已写章节！")
            return
        if not self._begin_project_task("提炼记忆"):
            return
        self._tool_task_running = True
        self.disable_buttons()
        text = generator.read_text_safe(self.latest_filepath)
        old_memo = generator.read_text_safe(os.path.join(generator.DIRS["plot"], "全局备忘录.txt"))

        sys_prompt = ("你是长篇小说的\"超级记忆压缩机与切片系统\"。\n"
                      "【硬性规则-违反则压缩失败】：\n"
                      "1. 必须保留备忘录中所有未死亡的关键角色的状态。\n"
                      "2. 多线并行时，每个分兵角色的最新已知状态和所在位置都必须保留。\n"
                      "3. 未解悬念和伏笔线索不可删除，只可精简措辞。\n"
                      "4. 只压缩措辞冗余，不可压缩关键信息。\n"
                      "5. 如果备忘录中包含'核心基调提醒'字段，必须原样保留。\n"
                      "6. 【最高优先级】每一章的最后200字中出现的悬念、异常现象、未解释事件，必须逐条提取并写入【未关闭章末悬念】字段。")
        user_prompt = f"""请执行绝对结构化的"短期记忆切片"。用不超过800字总结【旧记忆】+【最新章节】。

必须严格保留且更新以下结构：
【核心坐标状态】时间/地点/主角状态/关键队友多线状态
【近期事件纪要·已完成】
【近期事件纪要·进行中】
【未关闭章末悬念】逐条列出最近3章章末悬念，标注来源章节号
【主线线索与遗留伏笔】
【下一步主线导向】首要目标+衔接提醒

【旧记忆参考】：{old_memo if old_memo else "暂无。"}

【最新章节内容】：{text}"""

        def do_compress():
            self._ui_clear("正在提炼记忆...\n")
            try:
                memo_content = self.call_llm_non_stream(sys_prompt, user_prompt, temp=0.3)
                path = os.path.join(generator.DIRS["plot"], "全局备忘录.txt")
                if len(memo_content.strip()) < 50:
                    self._ui(lambda: messagebox.showwarning("警告", "压缩结果异常（内容过短），已保留原备忘录。"))
                    return
                if os.path.exists(path):
                    backup_dir = os.path.join(generator.DIRS["plot"], "备忘录备份")
                    os.makedirs(backup_dir, exist_ok=True)
                    backup_name = f"备忘录_手动_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                    shutil.copy2(path, os.path.join(backup_dir, backup_name))
                with open(path, "w", encoding="utf-8") as f:
                    f.write(memo_content)
                self._ui_clear(f"[OK] 记忆备忘录已更新！\n\n{memo_content}")
                self._ui(lambda: messagebox.showinfo("成功", "记忆备忘录已更新！"))
            except Exception as e:
                self._ui(lambda: messagebox.showerror("错误", f"提炼记忆失败: {str(e)}"))
            finally:
                self._tool_task_running = False
                self._finish_project_task()

        threading.Thread(target=do_compress, daemon=True).start()

    # ============================================================
    # 工具箱：伏笔追踪
    # ============================================================
    def track_foreshadowing(self):
        if not self.latest_filepath or not os.path.exists(self.latest_filepath):
            messagebox.showwarning("提示", "尚无已写章节！")
            return
        if not self._begin_project_task("伏笔追踪"):
            return
        self._tool_task_running = True
        self.disable_buttons()
        text = generator.read_text_safe(self.latest_filepath)
        old_table = generator.read_text_safe(os.path.join(generator.DIRS["plot"], "伏笔与因果追踪表.txt"))

        sys_prompt = "你是长篇小说的\"伏笔与因果追踪系统\"。"
        user_prompt = f"""请从以下最新章节中提取所有伏笔、未解悬念、已收回的伏笔，并与旧表格合并更新。

输出格式（Markdown表格）：
| 编号 | 伏笔描述 | 埋设章节 | 状态(未解/已收) | 收回章节 | 备注 |

【旧伏笔追踪表】：{old_table if old_table else "暂无。"}

【最新章节内容】：{text}"""

        def do_track():
            self._ui_clear("正在追踪伏笔...\n")
            try:
                table = self.call_llm_non_stream(sys_prompt, user_prompt, temp=0.2)
                path = os.path.join(generator.DIRS["plot"], "伏笔与因果追踪表.txt")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(table)
                self._ui_clear(f"[OK] 伏笔追踪表已更新！\n\n{table}")
                self._ui(lambda: messagebox.showinfo("成功", "伏笔追踪表已更新！"))
            except Exception as e:
                self._ui(lambda: messagebox.showerror("错误", f"伏笔追踪失败: {str(e)}"))
            finally:
                self._tool_task_running = False
                self._finish_project_task()

        threading.Thread(target=do_track, daemon=True).start()

    # ============================================================
    # 工具箱：进度编年史
    # ============================================================
    def update_chronicle(self):
        if not self.latest_filepath or not os.path.exists(self.latest_filepath):
            messagebox.showwarning("提示", "尚无已写章节！")
            return
        if not self._begin_project_task("更新编年史"):
            return
        self._tool_task_running = True
        self.disable_buttons()

        text = generator.read_text_safe(self.latest_filepath)
        chronicle_path = os.path.join(generator.DIRS["plot"], "世界编年史.txt")
        old_chronicle = generator.read_text_safe(chronicle_path)

        sys_prompt = "你是这本小说的'时间轴与编年史管理员'。"
        user_prompt = f"""请分析【最新章节内容】，提取时间流逝和重大里程碑事件，追加更新编年史。
格式：[相对时间戳] 第X章发生的里程碑简述。

【已有编年史参考】：{old_chronicle if old_chronicle else "（暂无）"}

【第 {self.latest_chap} 章最新内容】：{text}"""

        def do_update():
            self._ui_clear("正在推演时间轴...\n")
            try:
                new_entry = self.call_llm_non_stream(sys_prompt, user_prompt, temp=0.1)
                with open(chronicle_path, "a", encoding="utf-8") as f:
                    if not old_chronicle:
                        f.write("=== 小世界核心编年史 ===\n\n")
                    f.write(f"{new_entry}\n")
                self._ui_clear(f"[OK] 编年史已更新：\n\n{new_entry}")
                self._ui(lambda: messagebox.showinfo("成功", "世界编年史已更新！"))
            except Exception as e:
                self._ui(lambda: messagebox.showerror("错误", f"编年史更新失败: {str(e)}"))
            finally:
                self._tool_task_running = False
                self._finish_project_task()

        threading.Thread(target=do_update, daemon=True).start()



    # ============================================================
    # 工具箱：打开历史章节 (Fix2: os.walk 递归)
    # ============================================================
    def open_history_chapter(self):
        out_dir = generator.DIRS["out"]
        files = []
        for root_dir, dirs, fnames in os.walk(out_dir):
            dirs[:] = [d for d in dirs if d != '.backup']
            for fname in fnames:
                if self._is_official_chapter_file(fname):
                    files.append(os.path.join(root_dir, fname))
        files.sort(key=self._extract_chap_num_from_path)

        if not files:
            messagebox.showinfo("提示", "当前没有已保存的章节文件。")
            return

        win = tk.Toplevel(self.root)
        win.title("历史章节")
        win.geometry("400x500")
        win.grab_set()

        listbox = tk.Listbox(win, font=("微软雅黑", 10))
        listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        display_names = [os.path.basename(f) for f in files]
        for name in display_names:
            listbox.insert(tk.END, name)

        def load_selected():
            sel = listbox.curselection()
            if not sel:
                return
            fpath = files[sel[0]]
            content = generator.read_text_safe(fpath)
            self.result_text.delete(1.0, tk.END)
            self.result_text.insert(tk.END, content)
            self.update_word_count()
            win.destroy()

        ttk.Button(win, text="加载到编辑区", command=load_selected).pack(pady=10)

    # ============================================================
    # 一键打包导出
    # ============================================================
    def export_current_result(self):
        _, _, _, latest_chap, _ = generator.get_latest_chapter_info()
        planned_end = self._get_story_planned_end_chapter()
        self.export_book(preview=bool(planned_end and latest_chap < planned_end))

    def export_book(self, preview=False):
        publish_dir = generator.DIRS.get("publish", "")
        official_dir = generator.DIRS.get("out", "output")
        official_files = []
        if os.path.isdir(official_dir):
            for root_dir, dirs, files in os.walk(official_dir):
                dirs[:] = [d for d in dirs if d != '.backup']
                official_files.extend(
                    os.path.join(root_dir, name)
                    for name in files if self._is_official_chapter_file(name)
                )
        publish_files = (
            glob.glob(os.path.join(publish_dir, "第*.txt"))
            if publish_dir and os.path.isdir(publish_dir) else []
        )
        official_numbers = {self._extract_chap_num_from_path(path) for path in official_files}
        publish_numbers = {self._extract_chap_num_from_path(path) for path in publish_files}
        latest_statuses = self._load_latest_chapter_statuses()
        evidence_issues = []
        layout = generator.audit_chapter_layout()
        if layout.get("missing"):
            evidence_issues.append(
                "正式章节断号：第"
                + "、".join(str(item) for item in layout["missing"][:12])
                + "章"
            )
        if layout.get("duplicates"):
            evidence_issues.append(
                "正式章节存在重复文件：第"
                + "、".join(
                    str(item) for item in sorted(layout["duplicates"])[:12]
                )
                + "章"
            )
        if layout.get("misplaced"):
            evidence_issues.append("正式章节分卷位置错误")
        latest_official_number = max(official_numbers, default=0)
        for gate in commercial_reviewer.hard_gate_chapters(
            self.config, self._get_volume_ranges()
        ):
            if gate > latest_official_number:
                continue
            evidence_issues.extend(
                f"第{gate}章硬闸门：{issue}"
                for issue in self._hard_gate_integrity_issues(gate)
            )
        for path in official_files:
            chapter = self._extract_chap_num_from_path(path)
            row = latest_statuses.get(chapter) or {}
            chapter_text = generator.read_text_safe(path)
            current_hash = state_ledger.chapter_sha256(chapter_text)
            if not chapter_text.strip():
                evidence_issues.append(f"第{chapter}章正文为空或无法读取")
                continue
            if not row.get("status"):
                evidence_issues.append(f"第{chapter}章缺少状态")
            elif str(row.get("chapter_sha256") or "") != current_hash:
                evidence_issues.append(f"第{chapter}章状态与正文不匹配")
            if (
                self._strict_release_mode()
                and self.config.get("narrative_guard_enabled", True)
            ):
                try:
                    narrative_guard.verify_persisted_audit(
                        generator.DIRS["plot"],
                        chapter,
                        chapter_text,
                        expected_audit_id=str(
                            row.get("narrative_audit_id") or ""
                        ),
                        expected_review_model=self._narrative_review_model_for_chapter(
                            chapter, row
                        ),
                        min_score=int(
                            self.config.get("narrative_guard_min_score", 75)
                            or 75
                        ),
                    )
                except Exception as exc:
                    evidence_issues.append(f"第{chapter}章叙事审计无效：{exc}")

        if official_numbers and publish_numbers == official_numbers:
            official_by_number = {
                self._extract_chap_num_from_path(path): path
                for path in official_files
            }
            publish_by_number = {
                self._extract_chap_num_from_path(path): path
                for path in publish_files
            }
            for chapter in sorted(official_numbers):
                official_hash = state_ledger.chapter_sha256(
                    generator.read_text_exact(official_by_number[chapter])
                )
                publish_hash = state_ledger.chapter_sha256(
                    generator.read_text_exact(publish_by_number[chapter])
                )
                if official_hash != publish_hash:
                    evidence_issues.append(f"第{chapter}章发布稿与正式正文不一致")
        if evidence_issues:
            messagebox.showerror(
                "导出已阻止",
                "章节发布证据不完整，不能导出整本：\n"
                + "\n".join(evidence_issues[:20]),
            )
            return
        review_numbers = []
        for chapter in sorted(official_numbers):
            row = latest_statuses.get(chapter) or {}
            status = row.get("status")
            if status != "正式可用":
                review_numbers.append(chapter)
        if review_numbers:
            preview = "、".join(str(chapter) for chapter in review_numbers[:20])
            suffix = "..." if len(review_numbers) > 20 else ""
            messagebox.showerror(
                "导出已阻止",
                f"当前有 {len(review_numbers)} 章尚未标记为正式可用：第{preview}{suffix}章。\n\n"
                "请先修复并通过全部硬审，待复核章节不能进入整本成品。",
            )
            return
        planned_end = self._get_story_planned_end_chapter()
        latest_official = max(official_numbers, default=0)
        if not preview and planned_end and latest_official < planned_end:
            messagebox.showerror(
                "整本导出已阻止",
                f"当前只写到第{latest_official}章，规划终章是第{planned_end}章。\n\n"
                "请使用“导出当前结果”生成试写稿；未到终章不能标记为整本成品。",
            )
            return
        # 发布目录只有覆盖全部正式章节时才作为导出源，避免静默漏掉需复核章。
        out_dir = publish_dir if official_numbers and publish_numbers == official_numbers else official_dir
        if not os.path.exists(out_dir):
            messagebox.showwarning("提示", "当前没有输出文件夹！")
            return

        chapter_files = []
        for root_dir, dirs, files in os.walk(out_dir):
            dirs[:] = [d for d in dirs if d != '.backup']
            for f in files:
                if self._is_official_chapter_file(f):
                    chapter_files.append(os.path.join(root_dir, f))
        chapter_files.sort(key=self._extract_chap_num_from_path)

        if not chapter_files:
            messagebox.showinfo("提示", "当前没有写好的章节。")
            return

        export_label = "试写稿" if preview else "全书"
        export_path = filedialog.asksaveasfilename(
            title=f"一键排版导出{export_label}为 TXT",
            defaultextension=".txt",
            filetypes=[("Text file", "*.txt")],
            initialfile=f"我的小说_{export_label}_{datetime.now().strftime('%Y%m%d')}.txt"
        )
        if not export_path:
            return

        try:
            with open(export_path, "w", encoding="utf-8") as out_f:
                for fpath in chapter_files:
                    content = generator.read_text_safe(fpath)
                    if not content.strip():
                        raise RuntimeError(
                            f"无法读取第{self._extract_chap_num_from_path(fpath)}章，"
                            "已停止导出以避免生成缺章文件"
                        )
                    lines = content.split('\n')
                    formatted_lines = []
                    title_written = False
                    for line in lines:
                        clean_line = line.strip()
                        if not clean_line:
                            continue
                        if not title_written and clean_line.startswith("第") and "章" in clean_line:
                            formatted_lines.append(f"\n\n\n{clean_line}\n")
                            title_written = True
                        elif clean_line.startswith("---") or clean_line.startswith("==="):
                            formatted_lines.append(f"\n{clean_line}\n")
                        else:
                            formatted_lines.append(f"\u3000\u3000{clean_line}")
                    out_f.write("\n".join(formatted_lines))
                    out_f.write("\n")
            messagebox.showinfo("成功", f"{export_label}已一键排版导出至：\n{export_path}")
        except Exception as e:
            messagebox.showerror("导出失败", f"导出过程中遇到错误: {str(e)}")

    # ============================================================
    # Feature 1: 健康检查
    # ============================================================
    def _run_health_check_internal(self, future_window=None):
        """执行健康检查，返回 [(检查项, "PASS"/"FAIL"/"WARN"), ...]"""
        results = []
        try:
            recovered = self._recover_pending_chapter_commit()
            if recovered.get("status") == "RECOVERED":
                results.append((
                    f"崩溃恢复: 已补完第{recovered.get('chapter')}章提交",
                    "PASS",
                ))
            elif recovered.get("status") == "DISCARDED":
                results.append(("崩溃恢复: 已清理未写入的准备日志", "PASS"))
            else:
                results.append(("章节提交事务", "PASS"))
        except Exception as exc:
            results.append((f"章节提交事务恢复失败: {str(exc)[:100]}", "FAIL"))
        _, next_chap, _, latest_chap, _ = generator.get_latest_chapter_info()

        if self._project_config_error:
            results.append((self._project_config_error, "FAIL"))
        else:
            results.append(("project_config.json", "PASS"))

        rules_synced, rules_message = self._project_rules_sync_status()
        if rules_synced:
            results.append((rules_message, "PASS"))
        else:
            planned_end = max(0, int(self._get_story_planned_end_chapter() or 0))
            completed = bool(planned_end and latest_chap >= planned_end)
            results.append((
                rules_message + "；请使用“生成发布规则”重建",
                "WARN" if completed else "FAIL",
            ))
        if self.config.get("trial_gate_migration_required"):
            results.append((
                "旧项目缺少新版逐章证据链：必须逐章重审/重建后再通过30章闸门，当前保持冻结",
                "FAIL",
            ))

        layout = generator.audit_chapter_layout()
        if layout["missing"]:
            preview = "、".join(str(item) for item in layout["missing"][:8])
            results.append((f"正式章节断号: 第{preview}章", "FAIL"))
        if layout["duplicates"]:
            preview = "、".join(str(item) for item in sorted(layout["duplicates"])[:8])
            results.append((f"正式章节重复文件: 第{preview}章", "FAIL"))
        if layout["misplaced"]:
            preview = "、".join(str(item[0]) for item in layout["misplaced"][:8])
            results.append((f"正式章节分卷位置错误: 第{preview}章", "FAIL"))
        if not any((layout["missing"], layout["duplicates"], layout["misplaced"])):
            results.append(("正式章节断点与分卷布局", "PASS"))

        official_paths = self._official_chapter_paths_by_number()
        latest_statuses = self._load_latest_chapter_statuses()
        status_issues = []
        for chapter, path in sorted(official_paths.items()):
            row = latest_statuses.get(chapter) or {}
            if not row.get("status"):
                status_issues.append(f"第{chapter}章缺状态")
                continue
            if row.get("status") != "正式可用":
                status_issues.append(
                    f"第{chapter}章状态为{row.get('status')}，不是正式可用"
                )
            source_hash = state_ledger.chapter_sha256(generator.read_text_exact(path))
            if str(row.get("chapter_sha256") or "") != source_hash:
                status_issues.append(f"第{chapter}章状态哈希不匹配")
        if status_issues:
            results.append(("章节发布证据: " + "；".join(status_issues[:8]), "FAIL"))
        else:
            results.append(("章节发布状态与正文哈希", "PASS"))

        semantic_config = semantic_consistency_guard.load_semantic_invariants(
            self.project_dir or os.path.dirname(generator.DIRS["plot"])
        )
        if not semantic_config.get("valid"):
            results.append((self._semantic_guard_message(semantic_config), "FAIL"))
        elif official_paths:
            latest_semantic_chapter = max(official_paths)
            semantic_report = self._run_semantic_consistency_guard(
                generator.read_text_safe(official_paths[latest_semantic_chapter]),
                latest_semantic_chapter,
            )
            if semantic_report.get("passed"):
                results.append((
                    f"全量语义一致性硬门({len(official_paths)}章)", "PASS"
                ))
            else:
                results.append((
                    "全量语义一致性硬门: "
                    + self._semantic_guard_message(semantic_report, limit=4),
                    "FAIL",
                ))
        else:
            results.append(("机器可执行语义真相合同", "PASS"))

        hard_gate_targets = [
            gate for gate in commercial_reviewer.hard_gate_chapters(
                self.config, self._get_volume_ranges()
            ) if gate <= latest_chap
        ]
        for gate in hard_gate_targets:
            integrity_issues = self._hard_gate_integrity_issues(gate)
            if integrity_issues:
                results.append((
                    f"第{gate}章确定性硬闸门: "
                    + "；".join(integrity_issues[:4]),
                    "FAIL",
                ))
            else:
                results.append((f"第{gate}章确定性硬闸门清单", "PASS"))

        # Only the selected provider participates in readiness.  A missing
        # DeepSeek key must never make a Codex project unhealthy (and vice
        # versa).
        try:
            provider = self.get_model_provider()
            provider_name = self._provider_display_name()
        except ModelProviderConfigError as exc:
            provider = ""
            provider_name = "未知提供方"
            results.append((str(exc), "FAIL"))

        if provider:
            if not self._has_configured_model_provider():
                missing = (
                    "DeepSeek API Key"
                    if provider == PROVIDER_DEEPSEEK
                    else "项目内 Codex CLI 或访问权限"
                )
                results.append((f"{provider_name}: 缺少{missing}", "FAIL"))
            else:
                preflight = self._get_cached_provider_preflight()
                if preflight.get("status") == "PASS":
                    results.append((f"{provider_name} 当前提供方预检", "PASS"))
                elif preflight.get("status") == "FAIL":
                    results.append((
                        f"{provider_name} 预检失败: {preflight.get('message', '')[:100]}",
                        "FAIL",
                    ))
                else:
                    results.append((f"{provider_name} 尚未执行真实预检", "WARN"))

            generation_model = self.get_model_name()
            review_model = self.get_review_model_name()
            for label, model_name in (
                ("正文生成模型", generation_model),
                ("质检/审稿模型", review_model),
            ):
                if provider == PROVIDER_DEEPSEEK and model_name in LEGACY_DEEPSEEK_MODELS:
                    results.append((f"{label}: {model_name}（旧别名）", "FAIL"))
                elif provider == PROVIDER_CODEX_SOL and model_name != CODEX_SOL_MODEL:
                    results.append((f"{label}: 必须为 exact {CODEX_SOL_MODEL}", "FAIL"))
                else:
                    results.append((f"{label}: {model_name}", "PASS"))

            if provider == PROVIDER_CODEX_SOL:
                results.append((
                    "Codex 套餐调用：token/次数记账，本地无可核验单价，费用估算¥0",
                    "PASS",
                ))
            else:
                cost_limit = self._configured_cost_limit_cny()
                project_cost = self._project_estimated_cost_cny()
                if cost_limit <= 0:
                    results.append(("本书 DeepSeek 费用止损未设置", "FAIL"))
                elif project_cost >= cost_limit:
                    results.append((
                        f"本书估算费用已达止损: ¥{project_cost:.2f}/¥{cost_limit:.2f}",
                        "FAIL",
                    ))
                elif project_cost >= cost_limit * 0.8:
                    results.append((
                        f"本书估算费用接近止损: ¥{project_cost:.2f}/¥{cost_limit:.2f}",
                        "WARN",
                    ))
                else:
                    results.append((
                        f"本书费用止损: ¥{project_cost:.2f}/¥{cost_limit:.2f}",
                        "PASS",
                    ))

        if (
            self._strict_release_mode()
            and self.config.get("narrative_guard_enabled", True)
        ):
            latest_path = official_paths.get(latest_chap, "")
            latest_text = (
                generator.read_text_safe(latest_path) if latest_path else ""
            )
            latest_hash = (
                state_ledger.chapter_sha256(latest_text)
                if latest_path else ""
            )
            latest_status = latest_statuses.get(latest_chap) or {}
            narrative_status = narrative_guard.audit_status(
                generator.DIRS["plot"],
                latest_chap,
                expected_chapter_sha256=latest_hash,
                chapter_text=latest_text,
                expected_audit_id=str(
                    latest_status.get("narrative_audit_id") or ""
                ),
                min_score=int(
                    self.config.get("narrative_guard_min_score", 75) or 75
                ),
            )
            results.append((
                f"逐章主线与可读性硬审: {narrative_status.get('message', '')}",
                narrative_status.get("status", "FAIL"),
            ))
            audit_issues = []
            audited_count = 0
            required_audit_chapters = [
                chapter
                for chapter in sorted(official_paths)
                if self._chapter_requires_current_evidence(chapter)
            ]
            for chapter, path in sorted(official_paths.items()):
                if chapter not in required_audit_chapters:
                    continue
                row = latest_statuses.get(chapter) or {}
                expected_id = str(row.get("narrative_audit_id") or "")
                audit_path = os.path.join(
                    generator.DIRS["plot"],
                    "narrative_audits",
                    f"chapter_{chapter:04d}.json",
                )
                if not expected_id and not os.path.exists(audit_path):
                    audit_issues.append(f"第{chapter}章缺少叙事审计")
                    continue
                audited_count += 1
                try:
                    narrative_guard.verify_persisted_audit(
                        generator.DIRS["plot"],
                        chapter,
                        generator.read_text_exact(path),
                        expected_audit_id=expected_id,
                        expected_review_model=self._narrative_review_model_for_chapter(
                            chapter, row
                        ),
                        min_score=int(
                            self.config.get("narrative_guard_min_score", 75) or 75
                        ),
                    )
                except Exception as exc:
                    audit_issues.append(str(exc))
            if audit_issues:
                results.append((
                    "逐章叙事审计覆盖损坏: " + "；".join(audit_issues[:4]),
                    "FAIL",
                ))
            elif audited_count == len(required_audit_chapters):
                boundary = self._legacy_evidence_exempt_through_chapter()
                suffix = f"（第1—{boundary}章历史封存）" if boundary else ""
                results.append((
                    f"逐章叙事审计覆盖: {audited_count}章{suffix}",
                    "PASS",
                ))
            elif required_audit_chapters:
                results.append((
                    "逐章叙事审计覆盖不足: "
                    f"{audited_count}/{len(required_audit_chapters)}章",
                    "FAIL",
                ))
        elif self._strict_release_mode():
            results.append(("逐章主线与可读性硬审已关闭", "FAIL"))
        else:
            results.append(("简单入库模式：逐章模型审稿不启用", "PASS"))

        if self.config.get("semantic_arc_audit_fail_closed", True):
            results.append(("跨章语义审计异常时禁止落正式稿", "PASS"))
        else:
            results.append(("跨章语义审计仍允许异常降级", "FAIL"))

        if self.config.get("commercial_review_enabled", True):
            try:
                milestones = sorted({
                    int(item)
                    for item in self.config.get("commercial_review_milestones", [3, 10, 30])
                    if int(item) > 0
                })
                lookback = int(self.config.get("commercial_review_lookback", 30) or 30)
                fulltext_chars = int(
                    self.config.get("commercial_review_fulltext_chars", 120000) or 120000
                )
                if not milestones or lookback < 3 or fulltext_chars < 24000:
                    raise ValueError("里程碑、回看章节数或全文预算无效")
                volume_note = "+卷末" if self.config.get("commercial_review_volume_end", True) else ""
                hard_gates = commercial_reviewer.hard_gate_chapters(
                    self.config, self._get_volume_ranges()
                )
                results.append((
                    f"阶段商业审稿: 第{'/'.join(map(str, milestones))}章{volume_note}；"
                    f"硬闸门第{'/'.join(map(str, hard_gates))}章",
                    "PASS",
                ))
            except (TypeError, ValueError):
                results.append(("阶段商业审稿配置无效", "FAIL"))
        else:
            hard_gates = commercial_reviewer.hard_gate_chapters(
                self.config, self._get_volume_ranges()
            )
            if hard_gates:
                results.append((
                    "阶段商业审稿已关闭，但硬闸门仍启用；正式生成将被阻止",
                    "FAIL",
                ))
            else:
                results.append(("阶段商业审稿已关闭", "WARN"))

        if self.config.get("revision_debt_enabled", True):
            results.append(self._revision_debt_health_status(next_chap))
        else:
            results.append(("阶段修订债务闭环已关闭", "WARN"))

        if self.config.get("commercial_project_gate_enabled", True):
            blueprint = self._load_commercial_blueprint()
            min_score = max(
                60,
                min(95, int(self.config.get("commercial_project_min_score", 78) or 78)),
            )
            if blueprint:
                blueprint_issues = commercial_planner.validate_blueprint(
                    blueprint, min_score=min_score, require_gate=True
                )
                if blueprint_issues:
                    results.append((
                        "商业立项未通过: " + "；".join(blueprint_issues[:3]),
                        "FAIL" if not self._project_has_official_chapters() else "WARN",
                    ))
                else:
                    results.append((
                        f"商业立项: {blueprint.get('commercial_score')}分 / 读者承诺已锁定",
                        "PASS",
                    ))
            elif self._project_has_official_chapters():
                results.append(("旧项目没有开书商业立项记录，系统不会自动改写既有方向", "WARN"))
            else:
                results.append(("商业立项尚未生成", "FAIL"))
        else:
            results.append(("开书商业立项门槛已关闭", "WARN"))

        # 大纲文件检查
        for fname in ("全书大纲.txt", "当前卷大纲.txt", "全局备忘录.txt"):
            fpath = os.path.join(generator.DIRS["plot"], fname)
            if os.path.exists(fpath) and os.path.getsize(fpath) > 10:
                results.append((f"大纲: {fname}", "PASS"))
            else:
                results.append((f"大纲: {fname}", "WARN"))

        # 严格模式仍要求旧式文本锚点；简单模式已有故事圣经、正史、
        # 语义合同和事实库时，不再因缺少两份重复镜像阻塞写作。
        for fname in ("时间线锚点.txt", "伏笔与因果追踪表.txt"):
            status = self._truth_anchor_health_status(fname)
            suffix = (
                ""
                if os.path.exists(os.path.join(generator.DIRS["plot"], fname))
                else "（由故事圣经、正史、语义合同和事实库替代）"
            )
            results.append((f"真相锚点: {fname}{suffix}", status))

        # 结构化正史状态必须与正式章节进度一致。否则滚动细纲可能从旧状态续写。
        if self.config.get("canon_guard_enabled", True):
            canon = continuity_guard.load_canon(generator.DIRS["plot"])
            if not canon:
                results.append(("正史状态: canon_state.json", "FAIL"))
            else:
                canon_chapter = int(canon.get("current_chapter") or 0)
                canon_realm = str(canon.get("current_realm") or "未记录")
                if canon_chapter == latest_chap:
                    results.append((f"正史状态: 第{canon_chapter}章 / {canon_realm}", "PASS"))
                else:
                    results.append((
                        f"正史状态进度不一致: 正史第{canon_chapter}章 / 正文第{latest_chap}章",
                        "FAIL",
                    ))

        if self.config.get("structured_state_enabled", True):
            ledger_audit = state_ledger.audit_state(generator.DIRS["plot"], latest_chap)
            ledger_status = ledger_audit.get("status", "FAIL")
            ledger_message = ledger_audit.get("message", "长期正史账本状态未知")
            if ledger_status == "WARN":
                ledger_message += "（启动整本任务时将自动补齐，不会带着旧记忆续写）"
            results.append((f"证据正史账本: {ledger_message}", ledger_status))
            if self.config.get("temporal_memory_enabled", True):
                temporal_audit = temporal_memory.audit(
                    generator.DIRS["plot"],
                    max(0, int(ledger_audit.get("state_chapter", 0) or 0)),
                )
                temporal_status = temporal_audit.get("status", "FAIL")
                temporal_message = temporal_audit.get("message", "时序记忆状态未知")
                if ledger_status == "WARN" and temporal_status == "PASS":
                    temporal_status = "WARN"
                    temporal_message += "（历史正史补齐时将同步扩建索引）"
                results.append((f"时序记忆: {temporal_message}", temporal_status))
            else:
                results.append(("SQLite 时序记忆已关闭", "FAIL"))
            try:
                warn_after = max(3, int(self.config.get("subplot_stale_warn_chapters", 8) or 8))
                fail_after = max(
                    warn_after + 1,
                    int(self.config.get("subplot_stale_fail_chapters", 20) or 20),
                )
                ledger_state = state_ledger.load_state(generator.DIRS["plot"])
                stale_rows = []
                for item in ledger_state.get("subplots", {}).values():
                    if item.get("status") == "RESOLVED":
                        continue
                    age = next_chap - int(
                        item.get("last_advanced_chapter", item.get("opened_chapter", 0))
                    )
                    if age >= warn_after:
                        stale_rows.append((age, item.get("label", item.get("id", "未命名支线"))))
                stale_rows.sort(reverse=True)
                severe = [row for row in stale_rows if row[0] >= fail_after]
                if severe:
                    preview = "；".join(f"{label}({age}章)" for age, label in severe[:4])
                    results.append((f"支线长期停滞: {preview}（已强制注入后续细纲）", "WARN"))
                elif stale_rows:
                    preview = "；".join(f"{label}({age}章)" for age, label in stale_rows[:4])
                    results.append((f"支线待推进: {preview}", "WARN"))
                else:
                    results.append(("支线停滞门禁", "PASS"))
            except Exception as exc:
                results.append((f"证据正史账本读取失败: {str(exc)[:100]}", "FAIL"))
            if self.config.get("state_delta_independent_audit", True):
                results.append(("状态增量独立证据审计", "PASS"))
            else:
                results.append(("状态增量独立证据审计已关闭", "WARN"))
            if self.config.get("pre_save_continuity_audit_enabled", True):
                results.append(("正式保存前结构化连续性审计", "PASS"))
            else:
                results.append(("正式保存前结构化连续性审计已关闭", "FAIL"))
            if self.config.get("context_trace_enabled", True):
                results.append(("逐章上下文来源哈希追踪", "PASS"))
            else:
                results.append(("逐章上下文来源哈希追踪已关闭", "WARN"))
        elif self._strict_release_mode():
            results.append(("证据正史账本已关闭", "FAIL"))
        else:
            results.append(("简单入库模式：结构化证据账本不阻塞正文", "PASS"))

        for bible_issue in story_architect.audit_story_bible(generator.DIRS["plot"], self.config):
            status = bible_issue.get("status", "WARN")
            results.append((f"故事圣经: {bible_issue.get('message', '')}", status))

        stage_context = story_architect.build_stage_truth_context(
            generator.DIRS["plot"], next_chap, max_chars=10000
        )
        blocked_terms = story_architect.blocked_terms_for_chapter(
            generator.DIRS["plot"], next_chap
        )
        leaked_terms = [term for term in blocked_terms if term and term in stage_context]
        if leaked_terms:
            results.append((
                f"阶段真相隔离泄漏: {', '.join(leaked_terms[:5])}",
                "FAIL",
            ))
        else:
            results.append((f"阶段真相隔离: 第{next_chap}章", "PASS"))

        rewrite_floor = int(self.config.get(
            "word_count_rewrite_floor", repair_engine.DEFAULT_WORD_COUNT_REWRITE_FLOOR
        ) or repair_engine.DEFAULT_WORD_COUNT_REWRITE_FLOOR)
        salvage_floor = int(self.config.get(
            "min_review_continue_chars", repair_engine.DEFAULT_MIN_SALVAGE_CHARS
        ) or repair_engine.DEFAULT_MIN_SALVAGE_CHARS)
        hard_min = int(self.config.get("chapter_char_min", 3200) or 3200)
        if not (2400 <= salvage_floor <= rewrite_floor <= hard_min):
            results.append((
                f"短章阈值关系异常: 续跑{salvage_floor}/重写{rewrite_floor}/硬下限{hard_min}",
                "FAIL",
            ))
        else:
            results.append((
                f"短章阈值: 续跑{salvage_floor}/重写{rewrite_floor}/硬下限{hard_min}",
                "PASS",
            ))

        try:
            review_streak_limit = int(self.config.get("max_consecutive_review_chapters", 5) or 5)
        except (TypeError, ValueError):
            review_streak_limit = 0
        if 1 <= review_streak_limit <= 20:
            results.append((f"连续复核熔断: {review_streak_limit}章", "PASS"))
        else:
            results.append(("连续复核熔断配置无效", "FAIL"))

        if self.config.get("cross_chapter_high_policy", "pause") == "pause":
            results.append(("跨章 HIGH 问题强制暂停", "PASS"))
        else:
            results.append(("跨章 HIGH 问题强制暂停（旧弱配置已忽略）", "PASS"))

        # 逐章细纲
        outline_files = self._get_outline_candidate_files(next_chap)
        if outline_files:
            results.append((f"逐章细纲文件({len(outline_files)}个)", "PASS"))
        else:
            results.append(("逐章细纲文件", "WARN"))

        next_outline = self._extract_chapter_outline(next_chap)
        if next_outline:
            results.append((f"下一章细纲覆盖: 第{next_chap}章", "PASS"))
            if self.config.get("canon_guard_enabled", True):
                canon_outline = continuity_guard.validate_outline(
                    next_outline,
                    next_chap,
                    next_chap,
                    generator.DIRS["plot"],
                )
                if canon_outline["status"] == "PASS":
                    results.append((f"下一章细纲正史连续性: 第{next_chap}章", "PASS"))
                else:
                    results.append((
                        f"下一章细纲正史连续性: {canon_outline['summary'][:80]}",
                        "FAIL",
                    ))
            outline_guard = self._run_outline_reveal_guard(next_outline, next_chap)
            outline_status = outline_guard["status"]
            if outline_status == "WARN":
                reveal_rules = self._load_reveal_rules()
                if reveal_rules and reveal_rules.get("strict_mode", False):
                    outline_status = "FAIL"
            outline_summary = outline_guard.get("summary", "")
            if outline_status == "PASS":
                results.append((f"下一章细纲真相节奏: 第{next_chap}章", "PASS"))
            else:
                results.append((
                    f"下一章细纲真相节奏: 第{next_chap}章 - {outline_summary[:80]}",
                    outline_status
                ))
        else:
            if self.config.get("auto_outline_from_volume", False):
                results.append((f"下一章细纲覆盖: 第{next_chap}章（将自动生成）", "WARN"))
            else:
                results.append((f"下一章细纲覆盖: 第{next_chap}章", "FAIL"))

        # 托管连续性检查：历史缺口或超前草稿仍需阻止；恰好属于下一章的
        # 待审稿是可重试断点，正式稿已有的同章待审稿只是旧副本，均不应
        # 让“一键生成”陷入无法自行续跑的死锁。
        retryable_pending, blocking_pending = self._classify_pending_review_paths(
            next_chap
        )
        if blocking_pending:
            results.append((f"待审稿存在非当前章缺口({len(blocking_pending)}个)", "FAIL"))
        elif retryable_pending:
            results.append((f"下一章待审稿可自动重试({len(retryable_pending)}个)", "WARN"))
        else:
            results.append(("待审稿状态", "PASS"))

        repetition_problems = self._find_official_repetition_issues(latest_chap=latest_chap, lookback=40)
        if repetition_problems:
            preview = "、".join(str(chap) for chap, _, _ in repetition_problems[:8])
            suffix = "..." if len(repetition_problems) > 8 else ""
            results.append((f"正式章节重复结构: 第{preview}{suffix}章", "FAIL"))
        else:
            results.append(("正式章节重复结构", "PASS"))

        # 托管窗口检查：批量生成前不能只看下一章，否则几十章后才发现细纲断档。
        future_window = int(future_window or self.config.get("health_check_future_chapters", 30) or 30)
        future_start = next_chap
        requested_future_end = next_chap + future_window - 1
        planned_end = self._get_story_planned_end_chapter()
        if planned_end and future_start > planned_end:
            results.append((f"全书规划已至终章: 第{planned_end}章", "WARN"))
            future_end = planned_end
        elif planned_end and requested_future_end > planned_end:
            future_end = planned_end
            results.append((f"未来{future_window}章检查已截到终章: 第{future_start}-{future_end}章", "PASS"))
        else:
            future_end = requested_future_end
        missing_outlines = []
        no_volume = []
        for chap in range(future_start, future_end + 1):
            if not self._get_story_volume_name(chap):
                no_volume.append(chap)
                continue
            if not self._extract_chapter_outline(chap):
                missing_outlines.append(chap)
        if no_volume:
            span = f"{no_volume[0]}-{no_volume[-1]}" if len(no_volume) > 1 else str(no_volume[0])
            results.append((f"未来{future_window}章卷规划缺失: 第{span}章", "FAIL"))
        if missing_outlines:
            preview = "、".join(str(c) for c in missing_outlines[:8])
            suffix = "..." if len(missing_outlines) > 8 else ""
            if self.config.get("auto_outline_from_volume", False):
                results.append((f"未来{future_window}章细纲缺失: 第{preview}{suffix}章（托管时滚动生成）", "WARN"))
            else:
                results.append((f"未来{future_window}章细纲缺失: 第{preview}{suffix}章", "FAIL"))
        elif not no_volume:
            if future_start <= future_end:
                results.append((f"未来{future_window}章细纲覆盖: 第{future_start}-{future_end}章", "PASS"))
        else:
            results.append((f"未来{future_window}章细纲覆盖: 已检查有卷规划章节", "WARN"))

        expected_volume = self._get_story_volume_name(next_chap)
        current_outline = generator.read_text_safe(os.path.join(generator.DIRS["plot"], "当前卷大纲.txt"))
        if expected_volume and expected_volume in current_outline[:200]:
            results.append((f"当前卷大纲匹配: {expected_volume}", "PASS"))
        elif expected_volume:
            results.append((f"当前卷大纲匹配: 预期{expected_volume}", "WARN"))

        # 技能文件检查
        skills_dir = os.path.join(_exe_dir, "skills")
        if not os.path.isdir(skills_dir):
            skills_dir = os.path.join(self.project_dir, "skills")
        # 纯托管模式只需这些技能
        required_skills = {"quality_gate"}
        expected_skills = ["quality_gate", "memory_compressor", "entity_extractor",
                           "chronicle_keeper", "foreshadow_hunter"]
        for skill_id in expected_skills:
            skill_path = os.path.join(skills_dir, f"{skill_id}.json")
            if os.path.exists(skill_path):
                try:
                    with open(skill_path, "r", encoding="utf-8") as f:
                        json.load(f)
                    results.append((f"技能: {skill_id}", "PASS"))
                except Exception:
                    results.append((f"技能: {skill_id} (JSON无效)", "FAIL"))
            else:
                results.append((f"技能: {skill_id}", "FAIL" if skill_id in required_skills else "WARN"))

        # 输出目录
        out_dir = generator.DIRS["out"]
        if os.path.isdir(out_dir):
            results.append(("输出目录", "PASS"))
        else:
            results.append(("输出目录", "FAIL"))

        # 唯一真相设定表
        canon_path = os.path.join(generator.DIRS["plot"], "唯一真相设定表.md")
        canon_text = generator.read_text_safe(canon_path)
        if os.path.exists(canon_path) and book_initializer.has_meaningful_content(canon_text):
            results.append(("唯一真相设定表", "PASS"))
        else:
            results.append(("唯一真相设定表", "FAIL"))

        # reveal_rules.json
        reveal_path = os.path.join(generator.DIRS["plot"], "reveal_rules.json")
        if os.path.exists(reveal_path):
            try:
                with open(reveal_path, "r", encoding="utf-8") as f:
                    rr = json.load(f)
                topics = rr.get("topics", [])
                if topics:
                    results.append((f"reveal_rules.json ({len(topics)}个主题)", "PASS"))
                else:
                    results.append(("reveal_rules.json (0个主题)", "WARN"))
            except Exception:
                results.append(("reveal_rules.json (JSON无效)", "FAIL"))
        else:
            results.append(("reveal_rules.json", "WARN"))

        # cross_chapter_scanner 可用性
        scanner_path = os.path.join(_exe_dir, "cross_chapter_scanner.py")
        if os.path.exists(scanner_path):
            results.append(("跨章扫描器", "PASS"))
            active_rule_count = sum((
                len(cross_chapter_scanner.EVENT_PATTERNS),
                len(cross_chapter_scanner.FORBIDDEN_TERMS),
                len(cross_chapter_scanner.CHAPTER_GATED_TERMS),
                len(cross_chapter_scanner.OLD_NAME_PATTERNS),
            ))
            if active_rule_count:
                results.append((f"跨章扫描有效规则({active_rule_count}条)", "PASS"))
            else:
                results.append((
                    "项目自定义扫描规则为空（仍会运行通用时间线扫描）",
                    "WARN",
                ))
        else:
            results.append(("跨章扫描器", "WARN"))

        # 关键词一致性检查表
        kw_check_path = os.path.join(generator.DIRS["plot"], "关键词一致性检查表.txt")
        if os.path.exists(kw_check_path) and os.path.getsize(kw_check_path) > 10:
            results.append(("关键词一致性检查表", "PASS"))
        else:
            results.append(("关键词一致性检查表", "WARN"))

        # 每10章审稿清单
        review_path = os.path.join(generator.DIRS["plot"], "每10章审稿清单.txt")
        if os.path.exists(review_path) and os.path.getsize(review_path) > 10:
            results.append(("每10章审稿清单", "PASS"))
        else:
            results.append(("每10章审稿清单", "WARN"))

        # 当前进度
        results.append((f"当前进度: 第{self.current_vol}卷 第{latest_chap}章", "PASS"))

        return results

    def _revision_debt_health_status(self, next_chap):
        debt_path = os.path.join(generator.DIRS["plot"], "revision_debt.json")
        try:
            debt = commercial_reviewer.load_revision_debt(debt_path)
        except commercial_reviewer.RevisionDebtError as exc:
            return (str(exc), "FAIL")
        open_debts = [item for item in debt["items"] if item.get("status") == "OPEN"]
        overdue = [
            item for item in open_debts
            if next_chap > int(item.get("due_by") or 10**9)
        ]
        if overdue:
            preview = "；".join(item.get("action", "")[:50] for item in overdue[:3])
            return (f"阶段修订债务逾期，将强制注入后续章节: {preview}", "WARN")
        if open_debts:
            return (f"阶段修订债务: {len(open_debts)}项待下次审稿核销", "PASS")
        return ("阶段修订债务闭环", "PASS")

    def _commercial_review_report_dir(self):
        project_root = self.project_dir or os.path.dirname(generator.DIRS["out"])
        return os.path.join(project_root, "review_reports")

    def _persist_commercial_review_actions(self, result, chap_num):
        # Validate before writing even the derived action plan. A corrupt
        # ledger requires recovery, not an empty replacement on the next review.
        if self.config.get("revision_debt_enabled", True):
            commercial_reviewer.load_revision_debt(
                os.path.join(generator.DIRS["plot"], "revision_debt.json")
            )
        action_path = commercial_reviewer.save_action_plan(
            os.path.join(generator.DIRS["plot"], "商业审稿行动单.md"),
            int(chap_num),
            result,
        )
        result["action_plan_path"] = action_path
        if self.config.get("revision_debt_enabled", True):
            debt_path = os.path.join(generator.DIRS["plot"], "revision_debt.json")
            debt = commercial_reviewer.update_revision_debt(
                debt_path,
                int(chap_num),
                result,
                horizon=max(3, int(self.config.get("revision_debt_horizon", 10) or 10)),
            )
            result["revision_debt_path"] = debt_path
            result["open_revision_debt"] = len(
                [item for item in debt.get("items", []) if item.get("status") == "OPEN"]
            )
        return result

    def _run_commercial_stage_review(
        self,
        chapter_content,
        chap_num,
        persist_actions=False,
        persist_report=True,
    ):
        """运行低频阶段商业审稿；未保存草稿可选择暂缓报告落盘。"""
        volume_ranges = self._get_volume_ranges()
        review_config = self._commercial_review_runtime_config()
        hard_gate = commercial_reviewer.is_hard_gate(
            chap_num, review_config, volume_ranges
        )
        stage_calls = 0
        if hard_gate:
            lookback = max(
                3,
                int(review_config.get("commercial_review_lookback", 30) or 30),
            )
            expected = commercial_reviewer.expected_hard_gate_chapters(
                chap_num,
                lookback,
                volume_ranges,
                target_total_chapters=self._get_story_planned_end_chapter(),
            )
            blocks = commercial_reviewer.collect_expected_chapters(
                generator.DIRS["out"],
                expected,
                current_chapter=int(chap_num),
                current_content=chapter_content or "",
            )
            chunks = commercial_reviewer.partition_review_blocks(
                blocks,
                max(
                    24000,
                    int(
                        review_config.get(
                            "commercial_review_fulltext_chars", 120000
                        )
                        or 120000
                    ),
                ),
            )
            stage_calls = len(chunks) + (1 if len(chunks) > 1 else 0)
        if hard_gate:
            with self._model_call_lock:
                self._commercial_review_call_active = True
                self._commercial_review_stage_remaining = max(1, stage_calls)
                self._commercial_review_fallback_call_active = False
                self._commercial_review_fallback_eligible = 0
        review_max_tokens = 2600
        if hard_gate and self.config.get("deepseek_review_thinking", True):
            thinking_min = max(
                1,
                int(
                    self.config.get(
                        "deepseek_review_thinking_min_tokens", 6000
                    )
                    or 6000
                ),
            )
            review_max_tokens = max(review_max_tokens, thinking_min + 2600)

        def _retry_malformed_review(system_prompt, user_prompt):
            with self._model_call_lock:
                self._commercial_review_fallback_call_active = True
            try:
                return self.call_llm_review(
                    system_prompt,
                    user_prompt,
                    temp=0.15,
                    max_tokens=review_max_tokens,
                )
            finally:
                with self._model_call_lock:
                    self._commercial_review_fallback_call_active = False

        try:
            result = commercial_reviewer.run_review(
                output_dir=generator.DIRS["out"],
                report_dir=self._commercial_review_report_dir(),
                chapter_num=int(chap_num),
                current_content=chapter_content or "",
                config=review_config,
                volume_ranges=volume_ranges,
                story_context=story_architect.build_story_context(
                    generator.DIRS["plot"], int(chap_num), max_chars=3000
                ),
                canon_context=continuity_guard.build_canon_context(
                    generator.DIRS["plot"], max_chars=2800
                ),
                llm_call=lambda system_prompt, user_prompt: self.call_llm_review(
                    system_prompt,
                    user_prompt,
                    temp=0.15,
                    max_tokens=review_max_tokens,
                ),
                model_name=self.get_review_model_name(),
                commercial_context=self._commercial_contract_context(max_chars=5200),
                persist=persist_report,
                llm_retry=(
                    _retry_malformed_review if hard_gate else None
                ),
            )
        finally:
            if hard_gate:
                with self._model_call_lock:
                    self._commercial_review_call_active = False
                    self._commercial_review_stage_remaining = 0
                    self._commercial_review_fallback_call_active = False
                    self._commercial_review_fallback_eligible = 0
        if persist_actions:
            self._persist_commercial_review_actions(result, chap_num)
        return result

    def commercial_review_current(self):
        """手动审查当前已保存进度；耗时调用放到后台线程。"""
        if self._is_busy():
            messagebox.showwarning("阶段商业审稿", "当前有生成或审稿任务正在运行，请稍后再试。")
            return

        _, _, _, latest_chap, latest_path = generator.get_latest_chapter_info()
        if latest_chap <= 0 or not latest_path or not os.path.exists(latest_path):
            messagebox.showinfo("阶段商业审稿", "尚无已保存的正式章节可供审稿。")
            return
        if not self._begin_project_task("商业审稿"):
            return

        chapter_content = generator.read_text_safe(latest_path)
        self._commercial_review_running = True
        self.is_generating = True
        self.disable_buttons()
        self._ui_progress_append(
            f"正在用 {self.get_review_model_name()} 审查截至第 {latest_chap} 章的商业可读性...\n",
            clear=True,
        )

        def _worker():
            try:
                result = self._run_commercial_stage_review(
                    chapter_content, latest_chap, persist_actions=True
                )
                report = (
                    f"阶段商业审稿：第 {latest_chap} 章\n"
                    f"结论：{result.get('status')} / {result.get('action')}\n"
                    f"当前章：{result.get('current')}\n"
                    f"摘要：{result.get('summary', '')}\n"
                    f"报告：{result.get('report_path', '')}\n\n"
                    f"{result.get('raw', '')}"
                )
                self._ui_clear(report)
                self._ui(lambda: messagebox.showinfo(
                    "阶段商业审稿完成",
                    f"{result.get('status')} / {result.get('action')}\n\n"
                    f"{result.get('summary', '')[:240]}\n\n完整报告已保存。",
                ))
            except Exception as exc:
                error_text = str(exc)
                self._ui(lambda: messagebox.showerror(
                    "阶段商业审稿失败", f"审稿服务调用失败：{error_text}"
                ))
            finally:
                self._commercial_review_running = False
                self.is_generating = False
                self._finish_project_task()

        threading.Thread(target=_worker, daemon=True).start()

    @staticmethod
    def _parse_semantic_arc_audit_response(raw):
        """Accept explicit JSON or labelled decisions, never partial/ambiguous enums."""
        text = str(raw or "").strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
        if fenced:
            text = fenced.group(1)
        if text.startswith("{"):
            def unique_fields(pairs):
                result = {}
                for key, value in pairs:
                    key = key.upper()
                    if key in result:
                        raise ValueError("重复审计字段")
                    result[key] = value
                return result
            try:
                fields = json.loads(text, object_pairs_hook=unique_fields)
            except (ValueError, TypeError):
                return None
        else:
            fields = {}
            for key in ("FINAL", "CURRENT", "REASON"):
                values = re.findall(
                    rf"^[ \t]*{key}:[ \t]*(.*)$", text, re.MULTILINE | re.IGNORECASE
                )
                if len(values) > 1:
                    return None
                fields[key] = values[0].strip() if values else ""
        final = fields.get("FINAL")
        current = fields.get("CURRENT")
        reason = fields.get("REASON", "")
        if not all(isinstance(value, str) for value in (final, current, reason)):
            return None
        final, current = final.strip().upper(), current.strip().upper()
        if final not in {"PASS", "WARN", "FAIL"} or current not in {"PASS", "FAIL"}:
            return None
        return {"status": final, "current": current, "summary": reason.strip() or text[:300]}

    def _run_semantic_arc_audit(self, chapter_content, chap_num):
        """Low-frequency semantic continuity audit over compact chapter slices."""
        lookback = max(5, int(self.config.get("semantic_arc_audit_lookback", 20) or 20))
        chapter_rows = []
        for root_dir, dirs, fnames in os.walk(generator.DIRS["out"]):
            dirs[:] = [d for d in dirs if d != ".backup"]
            for fname in fnames:
                if not self._is_official_chapter_file(fname):
                    continue
                path = os.path.join(root_dir, fname)
                number = self._extract_chap_num_from_path(path)
                if max(1, chap_num - lookback + 1) <= number < chap_num:
                    chapter_rows.append((number, generator.read_text_safe(path)))
        chapter_rows.sort(key=lambda item: item[0])
        chapter_rows.append((chap_num, chapter_content or ""))

        # Review complete recent chapters, newest first within a fixed budget.
        # Head/tail sampling repeatedly missed contradictions introduced in the
        # middle of a chapter (injury use, item provenance, and false memories).
        max_review_chars = max(
            24000,
            int(self.config.get("semantic_arc_audit_fulltext_chars", 52000) or 52000),
        )
        selected = []
        used_chars = 0
        for number, text in reversed(chapter_rows[-lookback:]):
            clean = (text or "").strip()
            block = f"===== 第{number}章全文 =====\n{clean}"
            if selected and used_chars + len(block) > max_review_chars:
                break
            selected.append(block)
            used_chars += len(block)
        slices = list(reversed(selected))

        canon_context = continuity_guard.build_canon_context(generator.DIRS["plot"], max_chars=2200)
        story_context = story_architect.build_story_context(generator.DIRS["plot"], chap_num, max_chars=2200)
        system_prompt = (
            "你是长篇小说连续性总审。只检查可验证的跨章逻辑，不评价文采。"
            "你收到的是最近章节全文，不是首尾摘要。必须检查全文中的每次取物、移动、战斗和信息获得。"
            "重点检查：已完成事件重演、人物知识状态倒退、物品重复获得或消耗后复现、"
            "伤势与动作冲突、境界层级混淆、地点和时间跳跃、无来源情报、把猜测写成事实、"
            "当前阶段任务漂移。不要把合理回顾判成重复。"
        )
        user_prompt = f"""审核最近章节切片，重点判断第{chap_num}章是否制造新的连续性错误。

【正史】
{canon_context}

【阶段契约】
{story_context}

【最近章节全文】
{chr(10).join(slices)}

必须按以下格式回答：
FINAL: PASS/WARN/FAIL
CURRENT: PASS/FAIL
REASON: 一句话说明；如果只是历史问题，CURRENT必须为PASS。
"""
        try:
            raw = self.call_llm_review(system_prompt, user_prompt, max_tokens=1200)
        except Exception as exc:
            fail_closed = bool(
                self.config.get("semantic_arc_audit_fail_closed", True)
            )
            return {
                "status": "FAIL" if fail_closed else "WARN",
                "current": "FAIL" if fail_closed else "PASS",
                "unavailable": True,
                "summary": f"语义审计调用失败：{str(exc)[:100]}",
            }
        parsed = self._parse_semantic_arc_audit_response(raw)
        if parsed is None:
            fail_closed = bool(
                self.config.get("semantic_arc_audit_fail_closed", True)
            )
            return {
                "status": "FAIL" if fail_closed else "WARN",
                "current": "FAIL" if fail_closed else "PASS",
                "malformed": True,
                "summary": "语义审计返回格式无效：FINAL/CURRENT缺失、重复或取值非法",
                "raw": raw,
            }
        return {**parsed, "raw": raw}

    def _run_cross_chapter_check(self, chapter_content, chap_num, chapter_filepath):
        """
        生成后跨章一致性快速检查。
        针对单章做禁词/旧名/超前揭示扫描，每10章做一次事件去重。
        """
        issues = []
        try:
            # 将单章数据包装成 scanner 需要的格式
            chapters_data = [(chap_num, "", chapter_content)]

            # 1. 禁词扫描（每次生成都跑）
            forbidden = cross_chapter_scanner.scan_forbidden(chapters_data)
            issues.extend(forbidden)

            # 2. 旧名扫描（每次生成都跑）
            old_names = cross_chapter_scanner.scan_old_names(chapters_data)
            issues.extend(old_names)

            # 3. 每10章跑一次事件去重、时间线和跨章叙述模板检查
            if chap_num % 10 == 0:
                self._ui_progress_append(f"   第{chap_num}章触发定期跨章扫描...\n")
                # 扫描最近30章，跨卷读取正式稿；当前未保存章节直接从内存加入。
                start_scan = max(1, chap_num - 30)
                scan_files = cross_chapter_scanner.get_chapter_files_recursive(
                    generator.DIRS["out"], start_scan, chap_num - 1
                )
                scan_data = []
                for fp in scan_files:
                    num, title, text = cross_chapter_scanner.read_chapter(fp)
                    if num is not None:
                        scan_data.append((num, title, text))
                current_title = (chapter_content or "").strip().split("\n", 1)[0]
                scan_data.append((chap_num, current_title, chapter_content or ""))
                scan_data.sort(key=lambda x: x[0])

                events = cross_chapter_scanner.scan_events(scan_data)
                issues.extend(events)
                timeline = cross_chapter_scanner.scan_timeline_jumps(scan_data)
                issues.extend(timeline)
                rhetorical = cross_chapter_scanner.scan_rhetorical_templates(scan_data)
                issues.extend(rhetorical)

            # 输出结果
            if issues:
                high_issues = [i for i in issues if i.get('severity') == 'HIGH']
                med_issues = [i for i in issues if i.get('severity') == 'MEDIUM']
                if high_issues:
                    self._ui_progress_append(f"  [WARN] 跨章扫描发现 {len(high_issues)} 个高优先级问题:\n")
                    for iss in high_issues[:3]:  # 最多显示3条
                        self._ui_progress_append(f"     {iss['message'][:120]}\n")
                if med_issues:
                    self._ui_progress_append(f"  [INFO] 跨章扫描发现 {len(med_issues)} 个中优先级问题\n")
            else:
                if chap_num % 10 == 0:
                    self._ui_progress_append(f"  [OK] 跨章扫描通过\n")

            return issues

        except Exception as e:
            self._ui_progress_append(f"  [FAIL] 跨章扫描异常: {str(e)[:80]}\n")
            return [{
                "type": "scanner_unavailable",
                "severity": "HIGH",
                "chap": int(chap_num),
                "message": f"跨章扫描异常，已按失败处理：{str(e)[:160]}",
            }]

    def health_check(self):
        try:
            self._run_provider_preflight(force=True)
        except Exception as exc:
            failure = {
                "status": "FAIL",
                "message": str(exc),
                "checked_at_epoch": time.time(),
            }
            try:
                cache_key = self._provider_preflight_cache_key()
                self._provider_preflight_cache = dict(
                    getattr(self, "_provider_preflight_cache", {}) or {}
                )
                self._provider_preflight_cache[cache_key] = failure
            except Exception:
                pass
            if (self.config or {}).get("model_provider", PROVIDER_DEEPSEEK) == PROVIDER_DEEPSEEK:
                self._deepseek_preflight_cache = failure
        results = self._run_health_check_internal()
        report_lines = ["健康检查报告\n" + "=" * 40 + "\n"]
        for item, status in results:
            icon = "[OK]" if status == "PASS" else "[FAIL]" if status == "FAIL" else "[WARN]"
            report_lines.append(f"  {icon} {item}")
        report = "\n".join(report_lines)
        self.progress_text.delete(1.0, tk.END)
        self.progress_text.insert(tk.END, report)
        # health_check() performs a forced API preflight before building the
        # report. Refresh the summary label afterwards so it does not keep the
        # stale "Key 尚未联网预检" warning count from before the check.
        self._refresh_readiness_status()

    # ============================================================
    # Feature 4: 实体追踪面板
    # ============================================================
    def show_entity_panel(self):
        entity_path = os.path.join(generator.DIRS["plot"], "实体状态表.txt")
        if not os.path.exists(entity_path):
            messagebox.showinfo("提示", "实体状态表尚未生成。请先运行批量生成以自动创建。")
            return

        content = generator.read_text_safe(entity_path)
        win = tk.Toplevel(self.root)
        win.title("实体追踪面板")
        win.geometry("700x500")

        notebook = ttk.Notebook(win)
        notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 按 ## 分段创建标签页
        sections = re.split(r'^## ', content, flags=re.MULTILINE)
        for section in sections:
            if not section.strip():
                continue
            lines = section.strip().split("\n")
            tab_name = lines[0].strip()
            tab_content = "\n".join(lines[1:]).strip()

            tab = ttk.Frame(notebook)
            notebook.add(tab, text=tab_name[:10])

            # 尝试解析 Markdown 表格
            table_lines = [l for l in tab_content.split("\n") if "|" in l and not l.strip().startswith("|---")]
            if len(table_lines) >= 2:
                headers = [h.strip() for h in table_lines[0].split("|") if h.strip()]
                tree = ttk.Treeview(tab, columns=headers, show="headings")
                for h in headers:
                    tree.heading(h, text=h)
                    tree.column(h, width=120)
                for row_line in table_lines[1:]:
                    values = [v.strip() for v in row_line.split("|") if v.strip()]
                    if values:
                        tree.insert("", tk.END, values=values)
                tree.pack(fill=tk.BOTH, expand=True)
            else:
                text_widget = scrolledtext.ScrolledText(tab, wrap=tk.WORD, font=("微软雅黑", 10))
                text_widget.pack(fill=tk.BOTH, expand=True)
                text_widget.insert(tk.END, tab_content)

        ttk.Button(win, text="刷新", command=lambda: (win.destroy(), self.show_entity_panel())).pack(pady=5)

    # ============================================================
    # Feature 5: 回滚上一步
    # ============================================================
    def show_rollback_panel(self):
        out_dir = generator.DIRS["out"]
        backups = []
        for root_dir, dirs, files in os.walk(out_dir):
            if ".backup" in root_dir:
                for fname in files:
                    fpath = os.path.join(root_dir, fname)
                    backups.append((fpath, os.path.getmtime(fpath)))
        backups.sort(key=lambda x: x[1], reverse=True)
        backups = backups[:20]

        if not backups:
            messagebox.showinfo("提示", "没有可回滚的备份。")
            return

        win = tk.Toplevel(self.root)
        win.title("回滚上一步")
        win.geometry("600x400")
        win.grab_set()

        listbox = tk.Listbox(win, font=("Consolas", 9))
        listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        for fpath, mtime in backups:
            display = f"{os.path.basename(fpath)}  ({datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')})"
            listbox.insert(tk.END, display)

        info_lbl = ttk.Label(win, text="选择一个备份查看详情", font=("微软雅黑", 9))
        info_lbl.pack(padx=10)

        def on_select(event):
            sel = listbox.curselection()
            if not sel:
                return
            backup_path = backups[sel[0]][0]
            backup_content = generator.read_text_safe(backup_path)
            backup_chars = len(re.findall(r'[\u4e00-\u9fff]', backup_content))

            # 找到对应的当前版本
            basename = os.path.basename(backup_path)
            chap_match = re.search(r'第(\d+)章', basename)
            if chap_match:
                chap_num = int(chap_match.group(1))
                vol_dir = os.path.dirname(os.path.dirname(backup_path))
                current_path = os.path.join(vol_dir, f"第{str(chap_num).zfill(4)}章.txt")
                if os.path.exists(current_path):
                    current_content = generator.read_text_safe(current_path)
                    current_chars = len(re.findall(r'[\u4e00-\u9fff]', current_content))
                    info_lbl.config(text=f"备份: {backup_chars}字 | 当前: {current_chars}字")
                else:
                    info_lbl.config(text=f"备份: {backup_chars}字 | 当前版本未找到")
            else:
                info_lbl.config(text=f"备份: {backup_chars}字")

        listbox.bind("<<ListboxSelect>>", on_select)

        def do_rollback():
            sel = listbox.curselection()
            if not sel:
                return
            backup_path = backups[sel[0]][0]
            basename = os.path.basename(backup_path)
            chap_match = re.search(r'第(\d+)章', basename)
            if not chap_match:
                messagebox.showerror("错误", "无法解析章节号")
                return

            chap_num = int(chap_match.group(1))
            current_path = self._official_chapter_paths_by_number().get(chap_num)
            if not current_path or not os.path.isfile(current_path):
                messagebox.showerror("回滚已阻止", f"找不到第{chap_num}章当前正式稿")
                return

            if messagebox.askyesno("确认回滚", f"确定要将第{chap_num}章恢复到备份版本吗？"):
                backup_content = generator.read_text_safe(backup_path)
                try:
                    self._commit_manual_chapter(
                        chap_num,
                        current_path,
                        backup_content,
                        "备份回滚",
                    )
                except Exception as exc:
                    messagebox.showerror(
                        "回滚未通过",
                        "备份必须重新通过字数、语义、叙事、正史和商业闸门，"
                        f"不能直接覆盖正式稿。\n\n{str(exc)}",
                    )
                    return
                messagebox.showinfo("成功", f"第{chap_num}章已通过全部闸门并回滚！")
                win.destroy()

        ttk.Button(win, text="确认回滚", command=do_rollback).pack(pady=10)


# ============================================================
# 启动
# ============================================================
if __name__ == "__main__":
    root = tk.Tk()
    app = NovelGeneratorGUI(root)
    root.mainloop()
