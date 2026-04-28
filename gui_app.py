# -*- coding: utf-8 -*-
"""
千章小说创作系统 GUI 桌面版 v3.3 (纯一键托管版)
==============================================
功能：写新章 / 续写 / 一键托管批量生成 / 伏笔追踪 / 历史回看 / 自动保存 / 记忆压缩
      健康检查 / 实体面板 / 回滚 / 批量控制台 / 一键导出排版
托管链路：生成 → 本地快检 → 质检(硬伤优先) → 设定总校 → 跨章检 → 保存 → 记忆维护
"""
import os
import sys
import re
import json
import glob
import time
import shutil
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog, simpledialog
from datetime import datetime
from openai import OpenAI

try:
    import tiktoken
except Exception:
    tiktoken = None

# 模型预设（仅存储默认配置，Key 从 config.json 加载）
MODEL_PRESETS = {
    "DeepSeek-V3.2": {
        "config_key_field": "api_key",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat"
    },
    "MiniMax-M2.7": {
        "config_key_field": "minimax_api_key",
        "base_url": "https://api.minimaxi.com/v1",
        "model": "MiniMax-M2.7"
    }
}

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

# 持久化配置：记住上次打开的项目文件夹
LAST_PROJECT_FILE = os.path.join(_exe_dir, "gui_last_project.json")

def load_last_project():
    if os.path.exists(LAST_PROJECT_FILE):
        try:
            with open(LAST_PROJECT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("last_project_dir", "")
        except Exception:
            pass
    return ""

def save_last_project(path):
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
    }
    for d in generator.DIRS.values():
        os.makedirs(d, exist_ok=True)

# ============================================================
# 中文数字工具 (Fix6: _cn_to_num + 9.1: _num_to_cn_chapter)
# ============================================================
_CN_DIGIT = {'零': 0, '一': 1, '二': 2, '三': 3, '四': 4,
             '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}

def _cn_to_num(s):
    """中文数字转阿拉伯数字，支持到千位"""
    result = 0
    current = 0
    for ch in s:
        if ch in _CN_DIGIT:
            current = _CN_DIGIT[ch]
        elif ch == '十':
            result += (current if current else 1) * 10
            current = 0
        elif ch == '百':
            result += current * 100
            current = 0
        elif ch == '千':
            result += current * 1000
            current = 0
    result += current
    return result

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
        # 格式: (起始章, 结束章, "卷名")
        # 示例: (1, 50, "第一卷"), (51, 100, "第二卷"),
    ]
    HIGH_RISK_WATCH_KEYWORDS = {
        # 格式: "关键词1", "关键词2", ...
        # 示例: "车祸", "死亡", "暴力",
    }
    STYLE_RISK_KEYWORDS = {
        "章尾短句堆叠", "章尾破折号过密", "模糊词过密",
    }

    def __init__(self, root):
        self.root = root
        self.root.title("千章小说创作系统 v3.3 - 桌面版")
        self.root.geometry("1100x850")

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

        self.create_widgets()

        # 尝试加载上次的项目文件夹
        last_dir = load_last_project()
        if last_dir and os.path.isdir(last_dir):
            self.switch_project(last_dir)
        else:
            default_project = os.path.join(_exe_dir, "我的小说")
            os.makedirs(default_project, exist_ok=True)
            self.switch_project(default_project)
            quickstart_path = os.path.join(default_project, "plot", "快速上手指南.txt")
            if not os.path.exists(quickstart_path):
                with open(quickstart_path, "w", encoding="utf-8") as f:
                    f.write(
                        "欢迎使用千章小说创作系统！\n\n"
                        "【第一步】点击左上角「⚙ 设置」，填入你的 DeepSeek API Key。\n"
                        "【第二步】在 characters/ 文件夹里放入角色设定（每个角色一个 .txt 文件）。\n"
                        "【第三步】在 world_building/ 文件夹里放入世界观设定。\n"
                        "【第四步】在 plot/ 文件夹里放入大纲或细纲。\n"
                        "【第五步】在右侧「写作要求」框里输入第一章的情节，点击「写新章」即可！\n"
                    )
            self.refresh_knowledge_base()
            messagebox.showinfo("欢迎使用！",
                                "系统已自动创建了一个「我的小说」项目文件夹。\n\n"
                                "请先点击左上角「⚙ 设置」填入你的 API Key！")

        self.update_word_count()
        self.root.after(60000, self.auto_save_loop)

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

    def _ensure_batch_log_window(self):
        if self.batch_log_win and self.batch_log_win.winfo_exists():
            return

        self.batch_log_win = tk.Toplevel(self.root)
        self.batch_log_win.title("批量生成进度")
        self.batch_log_win.geometry("780x420")

        ttk.Label(self.batch_log_win, text="批量生成日志", font=("微软雅黑", 10, "bold")).pack(anchor=tk.W, padx=10, pady=(10, 4))
        self.batch_log_text = scrolledtext.ScrolledText(self.batch_log_win, wrap=tk.WORD, font=("微软雅黑", 10))
        self.batch_log_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        def _on_close():
            try:
                self.batch_log_win.destroy()
            finally:
                self.batch_log_win = None
                self.batch_log_text = None

        self.batch_log_win.protocol("WM_DELETE_WINDOW", _on_close)

    def _ui_progress_append(self, text, clear=False):
        """批量生成时写入日志窗，其他场景回退到正文框。"""
        def _do():
            if self.is_batch_running:
                self._ensure_batch_log_window()
                if self.batch_log_text:
                    if clear:
                        self.batch_log_text.delete(1.0, tk.END)
                    self.batch_log_text.insert(tk.END, text)
                    self.batch_log_text.see(tk.END)
                return

            if clear:
                self.result_text.delete(1.0, tk.END)
            self.result_text.insert(tk.END, text)
            self.result_text.see(tk.END)

        self.root.after(0, _do)

    def _get_story_volume_name(self, chap_num):
        for start, end, volume_name in self.STORY_VOLUME_RANGES:
            if start <= chap_num <= end:
                return volume_name
        return None

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
        volume_header_pattern = re.compile(r'^=+|^▌第.+卷')

        for line in lines:
            stripped = line.strip()
            if f"▌{volume_name}" in stripped or stripped.startswith(volume_name + "：") or stripped.startswith(volume_name + ":"):
                capturing = True
                result_lines = [line]
                continue
            if capturing:
                if (stripped.startswith("▌第") and volume_name not in stripped) or (stripped.startswith("第") and "卷" in stripped and volume_name not in stripped and "章节：" in stripped):
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
        folder = filedialog.askdirectory(title="选择小说项目文件夹")
        if folder:
            self.switch_project(folder)

    def switch_project(self, folder):
        self.project_dir = folder
        apply_project_dir(folder)
        save_last_project(folder)
        generator.init_demo_files()
        self.current_vol, self.next_chap, self.filepath, self.latest_chap, self.latest_filepath = generator.get_latest_chapter_info()
        # 加载项目级配置 (Feature 2)
        self._load_project_config(folder)
        self.refresh_knowledge_base()
        self.refresh_status()
        self.folder_lbl.config(text=f"📁 {os.path.basename(folder)}")
        self.root.title(f"千章小说创作系统 v3.2 - {os.path.basename(folder)}")

    def _load_project_config(self, folder):
        """加载项目级配置 (Feature 2: 项目级配置)"""
        pconfig_path = os.path.join(folder, "project_config.json")
        if os.path.exists(pconfig_path):
            try:
                with open(pconfig_path, "r", encoding="utf-8") as f:
                    pconfig = json.load(f)
                # 隔离字段同步
                for key in ("model", "temperature", "current_volume", "enabled_skills", "tone_rules"):
                    if key in pconfig:
                        self.config[key] = pconfig[key]
                # 同步 UI 模型选择
                if "model" in pconfig:
                    for preset_name, preset in MODEL_PRESETS.items():
                        if preset["model"] == pconfig["model"]:
                            self.model_var.set(preset_name)
                            break
            except Exception:
                pass
        else:
            # 不存在则从当前配置创建
            try:
                pconfig = {k: self.config.get(k) for k in ("model", "temperature", "current_volume", "enabled_skills", "tone_rules") if k in self.config}
                with open(pconfig_path, "w", encoding="utf-8") as f:
                    json.dump(pconfig, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    def get_client(self):
        preset = MODEL_PRESETS.get(self.model_var.get(), list(MODEL_PRESETS.values())[0])
        key_field = preset.get("config_key_field", "api_key")
        api_key = self.config.get(key_field, "")
        if not api_key:
            # Fix: 线程安全弹窗
            self._ui(lambda kf=key_field: messagebox.showwarning("提示", f"请先在'设置'中填写 {kf} ！"))
            return None
        return OpenAI(api_key=api_key, base_url=preset["base_url"], timeout=120)

    def get_model_name(self):
        preset = MODEL_PRESETS.get(self.model_var.get(), list(MODEL_PRESETS.values())[0])
        return preset["model"]

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

    def on_model_change(self, event=None):
        self.refresh_status()

    # ============================================================
    # UI 构建
    # ============================================================
    def create_widgets(self):
        # ---- 顶部信息栏 ----
        top_frame = ttk.Frame(self.root, padding=8)
        top_frame.pack(fill=tk.X)

        self.info_lbl = ttk.Label(top_frame, text=self.get_status_text(), font=("微软雅黑", 10, "bold"))
        self.info_lbl.pack(side=tk.LEFT)

        self.word_count_lbl = ttk.Label(top_frame, text="字数: 0", font=("微软雅黑", 10))
        self.word_count_lbl.pack(side=tk.RIGHT, padx=15)

        # ---- 顶部按钮栏 ----
        toolbar = ttk.Frame(self.root, padding=(10, 2))
        toolbar.pack(fill=tk.X)

        ttk.Button(toolbar, text="⚙ 设置", command=self.open_settings).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="🔄 刷新状态", command=self.refresh_status).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="🧠 提炼记忆", command=self.compress_memory).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="🔮 伏笔追踪", command=self.track_foreshadowing).pack(side=tk.LEFT, padx=3)

        ttk.Button(toolbar, text="📜 进度编年史", command=self.update_chronicle).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="📂 打开历史章节", command=self.open_history_chapter).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="📑 一键导出排版", command=self.export_book).pack(side=tk.LEFT, padx=3)
        # Feature 1, 4, 5: 新增工具栏按钮
        ttk.Button(toolbar, text="🩺 健康检查", command=self.health_check).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="👤 实体面板", command=self.show_entity_panel).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="⏪ 回滚上一步", command=self.show_rollback_panel).pack(side=tk.LEFT, padx=3)

        # ---- 主体布局 ----
        main_paned = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main_paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # ====== 左侧：知识库选择面板 (可滚动) ======
        left_outer = ttk.Frame(main_paned)
        main_paned.add(left_outer, weight=1)

        left_canvas = tk.Canvas(left_outer, highlightthickness=0)
        left_scrollbar = ttk.Scrollbar(left_outer, orient=tk.VERTICAL, command=left_canvas.yview)
        self.left_scrollable = ttk.Frame(left_canvas)

        self.left_scrollable.bind("<Configure>", lambda e: left_canvas.configure(scrollregion=left_canvas.bbox("all")))
        left_canvas.create_window((0, 0), window=self.left_scrollable, anchor=tk.NW)
        left_canvas.configure(yscrollcommand=left_scrollbar.set)

        left_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        left_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        def _on_mousewheel(event):
            left_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        def _bind_mousewheel(event):
            left_canvas.bind_all("<MouseWheel>", _on_mousewheel)
        def _unbind_mousewheel(event):
            left_canvas.unbind_all("<MouseWheel>")
        left_canvas.bind("<Enter>", _bind_mousewheel)
        left_canvas.bind("<Leave>", _unbind_mousewheel)

        # 项目文件夹选择器
        folder_frame = ttk.Frame(self.left_scrollable)
        folder_frame.pack(fill=tk.X, pady=(5, 10), padx=5)
        self.folder_lbl = ttk.Label(folder_frame, text="📁 未选择", font=("微软雅黑", 9))
        self.folder_lbl.pack(side=tk.LEFT)
        ttk.Button(folder_frame, text="切换项目", command=self.select_project_folder).pack(side=tk.RIGHT)

        # 模型选择器
        model_frame = ttk.Frame(self.left_scrollable)
        model_frame.pack(fill=tk.X, pady=(5, 10), padx=5)
        ttk.Label(model_frame, text="🤖 AI模型:", font=("微软雅黑", 9, "bold")).pack(side=tk.LEFT)
        self.model_var = tk.StringVar(value=list(MODEL_PRESETS.keys())[0])
        model_combo = ttk.Combobox(model_frame, textvariable=self.model_var, values=list(MODEL_PRESETS.keys()), state="readonly", width=15)
        model_combo.pack(side=tk.RIGHT)
        model_combo.bind("<<ComboboxSelected>>", self.on_model_change)

        ttk.Separator(self.left_scrollable, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=5, pady=5)

        # 出场人物
        chars_header = ttk.Frame(self.left_scrollable)
        chars_header.pack(fill=tk.X, pady=(5, 3), padx=5)
        ttk.Label(chars_header, text="📌 出场人物:", font=("微软雅黑", 9, "bold")).pack(side=tk.LEFT)
        ttk.Button(chars_header, text="全选", width=4, command=lambda: self.toggle_all(self.chars_vars, True)).pack(side=tk.RIGHT, padx=1)
        ttk.Button(chars_header, text="清空", width=4, command=lambda: self.toggle_all(self.chars_vars, False)).pack(side=tk.RIGHT, padx=1)
        self.chars_frame = ttk.Frame(self.left_scrollable)
        self.chars_frame.pack(fill=tk.X, anchor=tk.W, padx=5)
        self.chars_vars = {}

        # 世界观设定
        world_header = ttk.Frame(self.left_scrollable)
        world_header.pack(fill=tk.X, pady=(12, 3), padx=5)
        ttk.Label(world_header, text="🌍 世界观/设定:", font=("微软雅黑", 9, "bold")).pack(side=tk.LEFT)
        ttk.Button(world_header, text="全选", width=4, command=lambda: self.toggle_all(self.world_vars, True)).pack(side=tk.RIGHT, padx=1)
        ttk.Button(world_header, text="清空", width=4, command=lambda: self.toggle_all(self.world_vars, False)).pack(side=tk.RIGHT, padx=1)
        self.world_frame = ttk.Frame(self.left_scrollable)
        self.world_frame.pack(fill=tk.X, anchor=tk.W, padx=5)
        self.world_vars = {}

        # 剧情大纲与备忘录
        plot_header = ttk.Frame(self.left_scrollable)
        plot_header.pack(fill=tk.X, pady=(12, 3), padx=5)
        ttk.Label(plot_header, text="📋 大纲与备忘录:", font=("微软雅黑", 9, "bold")).pack(side=tk.LEFT)
        ttk.Button(plot_header, text="全选", width=4, command=lambda: self.toggle_all(self.plot_vars, True)).pack(side=tk.RIGHT, padx=1)
        ttk.Button(plot_header, text="清空", width=4, command=lambda: self.toggle_all(self.plot_vars, False)).pack(side=tk.RIGHT, padx=1)
        self.plot_frame = ttk.Frame(self.left_scrollable)
        self.plot_frame.pack(fill=tk.X, anchor=tk.W, padx=5)
        self.plot_vars = {}

        ttk.Button(self.left_scrollable, text="🔄 刷新知识库列表", command=self.refresh_knowledge_base).pack(pady=15)

        # ====== 右侧：写作 + 阅读 双面板 ======
        right_paned = ttk.PanedWindow(main_paned, orient=tk.VERTICAL)
        main_paned.add(right_paned, weight=3)

        # ---- 上半：写作控制与生成区 ----
        write_frame = ttk.Frame(right_paned)
        right_paned.add(write_frame, weight=2)

        ttk.Label(write_frame, text="本章写作要求 / 事件发展 / 细纲:", font=("微软雅黑", 9, "bold")).pack(anchor=tk.W)
        self.prompt_text = tk.Text(write_frame, height=3, font=("微软雅黑", 10), wrap=tk.WORD)
        self.prompt_text.pack(fill=tk.X, pady=3)

        btn_frame = ttk.Frame(write_frame)
        btn_frame.pack(fill=tk.X, pady=3)

        self.btn_new = ttk.Button(btn_frame, text="📝 写新章", command=self.generate_new_chapter)
        self.btn_new.pack(side=tk.LEFT, padx=3)
        self.btn_continue = ttk.Button(btn_frame, text="📎 续写", command=self.continue_chapter)
        self.btn_continue.pack(side=tk.LEFT, padx=3)
        self.btn_batch = ttk.Button(btn_frame, text="🚀 批量挂机写", command=self.batch_generate)
        self.btn_batch.pack(side=tk.LEFT, padx=3)
        self.btn_stop = ttk.Button(btn_frame, text="⏹ 停止", command=self.stop_batch, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=3)

        ttk.Label(write_frame, text="AI 生成正文 (可手动微调后保存):", font=("微软雅黑", 9, "bold")).pack(anchor=tk.W, pady=(5, 0))
        self.result_text = scrolledtext.ScrolledText(write_frame, wrap=tk.WORD, font=("微软雅黑", 11))
        self.result_text.pack(fill=tk.BOTH, expand=True, pady=3)
        self.result_text.bind("<KeyRelease>", lambda e: self.update_word_count())

        self.create_context_menu(self.result_text)

        save_frame = ttk.Frame(write_frame)
        save_frame.pack(fill=tk.X, pady=3)
        self.btn_save_new = ttk.Button(save_frame, text="💾 保存为新章", command=self.save_new_chapter, state=tk.DISABLED)
        self.btn_save_new.pack(side=tk.LEFT, padx=3)
        self.btn_save_append = ttk.Button(save_frame, text="📌 追加到旧章", command=self.save_append_chapter, state=tk.DISABLED)
        self.btn_save_append.pack(side=tk.LEFT, padx=3)

        # ---- 下半：章节阅读器 ----
        reader_frame = ttk.Frame(right_paned)
        right_paned.add(reader_frame, weight=1)

        reader_bar = ttk.Frame(reader_frame)
        reader_bar.pack(fill=tk.X, pady=3)
        ttk.Label(reader_bar, text="📖 章节阅读器:", font=("微软雅黑", 9, "bold")).pack(side=tk.LEFT)
        ttk.Button(reader_bar, text="◀ 上一章", command=self.reader_prev).pack(side=tk.LEFT, padx=5)
        ttk.Button(reader_bar, text="▶ 下一章", command=self.reader_next).pack(side=tk.LEFT, padx=3)
        ttk.Label(reader_bar, text="选章节").pack(side=tk.LEFT, padx=(8, 2))
        self.reader_pick_var = tk.StringVar()
        self.reader_pick_combo = ttk.Combobox(
            reader_bar,
            textvariable=self.reader_pick_var,
            state="readonly",
            width=16
        )
        self.reader_pick_combo.pack(side=tk.LEFT, padx=3)
        self.reader_pick_combo.bind("<<ComboboxSelected>>", self.reader_jump_to_selected)
        ttk.Button(reader_bar, text="🔄 刷新", command=self.reader_refresh_current).pack(side=tk.LEFT, padx=3)
        self.reader_lbl = ttk.Label(reader_bar, text="未加载", font=("微软雅黑", 9))
        self.reader_lbl.pack(side=tk.LEFT, padx=10)
        ttk.Button(reader_bar, text="📋 全选复制", command=self.reader_copy_all).pack(side=tk.RIGHT, padx=3)

        self.reader_text = scrolledtext.ScrolledText(reader_frame, wrap=tk.WORD, font=("微软雅黑", 10))
        self.reader_text.pack(fill=tk.BOTH, expand=True, pady=3)
        self.reader_chapter_idx = 0
        self.reader_chapter_files = []

        self.create_context_menu(self.reader_text)

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
    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("⚙ 系统设置")
        win.geometry("520x480")
        win.grab_set()

        ttk.Label(win, text="DeepSeek API Key:", font=("微软雅黑", 10)).pack(anchor=tk.W, padx=15, pady=(15, 3))
        api_entry = ttk.Entry(win, width=60, show="*")
        api_entry.pack(padx=15, fill=tk.X)
        api_entry.insert(0, self.config.get("api_key", ""))

        ttk.Label(win, text="MiniMax API Key (可选):", font=("微软雅黑", 10)).pack(anchor=tk.W, padx=15, pady=(10, 3))
        minimax_entry = ttk.Entry(win, width=60, show="*")
        minimax_entry.pack(padx=15, fill=tk.X)
        minimax_entry.insert(0, self.config.get("minimax_api_key", ""))

        ttk.Label(win, text="API Base URL:", font=("微软雅黑", 10)).pack(anchor=tk.W, padx=15, pady=(10, 3))
        url_entry = ttk.Entry(win, width=60)
        url_entry.pack(padx=15, fill=tk.X)
        url_entry.insert(0, self.config.get("base_url", "https://api.deepseek.com"))

        ttk.Label(win, text="模型名称:", font=("微软雅黑", 10)).pack(anchor=tk.W, padx=15, pady=(10, 3))
        model_entry = ttk.Entry(win, width=60)
        model_entry.pack(padx=15, fill=tk.X)
        model_entry.insert(0, self.config.get("model", "deepseek-chat"))

        ttk.Label(win, text="Temperature (创意度 0.0-1.5):", font=("微软雅黑", 10)).pack(anchor=tk.W, padx=15, pady=(10, 3))
        temp_entry = ttk.Entry(win, width=20)
        temp_entry.pack(anchor=tk.W, padx=15)
        temp_entry.insert(0, str(self.config.get("temperature", 0.8)))

        def save_settings():
            # Fix Bug5+自审: 空值不覆盖已有 Key
            new_api_key = api_entry.get().strip()
            new_minimax_key = minimax_entry.get().strip()
            if new_api_key:
                self.config["api_key"] = new_api_key
            if new_minimax_key:
                self.config["minimax_api_key"] = new_minimax_key
            self.config["base_url"] = url_entry.get().strip()
            self.config["model"] = model_entry.get().strip()
            try:
                self.config["temperature"] = float(temp_entry.get().strip())
            except Exception:
                self.config["temperature"] = 0.8
            # Fix Bug5: 使用 _exe_dir 而非 __file__
            config_path = os.path.join(_exe_dir, "config.json")
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
            # 同步 generator.config
            generator.config = self.config
            messagebox.showinfo("成功", "设置已保存！")
            win.destroy()

        ttk.Button(win, text="💾 保存设置", command=save_settings).pack(pady=20)

    # ============================================================
    # 知识库加载
    # ============================================================
    def get_status_text(self):
        msg = f"进度：第 {self.current_vol} 卷"
        if self.latest_chap > 0:
            msg += f" | 已写至：第 {self.latest_chap} 章 | 下一章：第 {self.next_chap} 章"
        else:
            msg += " | 尚未开始，准备写第一章"
        return msg

    def update_word_count(self):
        text = self.result_text.get(1.0, tk.END).strip()
        count = len(re.findall(r'[\u4e00-\u9fff]', text))
        self.word_count_lbl.config(text=f"字数: {count}")

    def refresh_status(self):
        self.current_vol, self.next_chap, self.filepath, self.latest_chap, self.latest_filepath = generator.get_latest_chapter_info()
        self.info_lbl.config(text=self.get_status_text())

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

        self.plot_map = generator.list_files_in_dir(generator.DIRS["plot"])
        self.plot_vars.clear()

        # Plot 区默认采用“最小安全集”：
        # 1) 只默认勾状态/约束类文件
        # 2) 只自动勾当前章节所属的活动细纲段
        # 3) 历史段、未来段、总纲、方法论、审查报告默认关闭
        safe_default_files = {
            "基调铁律.txt",
            "核心设定.txt",
            "全书大纲.txt",
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

    def refresh_reader_files(self):
        self.reader_chapter_files = []
        out_dir = generator.DIRS.get("out", "output")
        if not os.path.exists(out_dir):
            self._update_reader_selector()
            return
        for root_dir, dirs, files in os.walk(out_dir):
            dirs[:] = [d for d in dirs if d != '.backup']
            for f in files:
                if f.endswith(".txt") and f.startswith("第") and "章" in f:
                    self.reader_chapter_files.append(os.path.join(root_dir, f))
        self.reader_chapter_files.sort(key=self._extract_chap_num_from_path)
        self._update_reader_selector()
        if self.reader_chapter_files:
            self.reader_chapter_idx = len(self.reader_chapter_files) - 1
            self.reader_load()

    def reader_load(self):
        if not self.reader_chapter_files:
            self.reader_lbl.config(text="暂无章节")
            return
        idx = self.reader_chapter_idx
        filepath = self.reader_chapter_files[idx]
        filename = os.path.basename(filepath)
        self.reader_lbl.config(text=f"{filename}  ({idx + 1}/{len(self.reader_chapter_files)})")
        self.reader_pick_var.set(filename)
        content = generator.read_text_safe(filepath)
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
        old_idx = self.reader_chapter_idx
        self.reader_chapter_files = []
        out_dir = generator.DIRS.get("out", "output")
        if not os.path.exists(out_dir):
            self._update_reader_selector()
            return
        for root_dir, dirs, files in os.walk(out_dir):
            dirs[:] = [d for d in dirs if d != '.backup']
            for f in files:
                if f.endswith(".txt") and f.startswith("第") and "章" in f:
                    self.reader_chapter_files.append(os.path.join(root_dir, f))
        self.reader_chapter_files.sort(key=self._extract_chap_num_from_path)
        self.reader_chapter_idx = min(old_idx, max(0, len(self.reader_chapter_files) - 1))
        self._update_reader_selector()

    def _update_reader_selector(self):
        values = [os.path.basename(p) for p in self.reader_chapter_files]
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
        values = [os.path.basename(p) for p in self.reader_chapter_files]
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

    def reader_copy_all(self):
        content = ""
        if self.reader_chapter_files:
            idx = min(max(0, self.reader_chapter_idx), len(self.reader_chapter_files) - 1)
            content = generator.read_text_safe(self.reader_chapter_files[idx])
        if not content:
            content = self.reader_text.get(1.0, tk.END)
        body = self._extract_chapter_body_for_copy(content)
        if body:
            self.root.clipboard_clear()
            self.root.clipboard_append(body)
            base_text = self.reader_lbl.cget("text").split(" ✅已复制(纯正文)")[0]
            self.reader_lbl.config(text=base_text + " ✅已复制(纯正文)")

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
            "你是一名顶尖的网络小说首发网站白金作家，正在连载一部千章量级的长篇巨著。\n"
            "请严格遵守提供的数据库设定，你的目标是输出极具吸引力、行文流畅的【小说正文】。\n"
            "【绝对规则】：\n"
            "1. 只输出小说正文和章节标题！严禁用任何形式与读者互动、不准加注释、不准写摘要。\n"
            "2. 严禁使用任何Markdown格式！不准用#号标题、不准用**加粗、不准用*斜体，输出纯文本。禁止在标题前加#号！\n"
            "3. 不要使用过于翻译腔或播音腔的词汇，需符合网文阅读爽感与节奏。\n"
            "4. 如当前写作要求与既有设定冲突，以【时间线锚点/唯一真相设定/当前卷大纲/全局备忘录/伏笔表】为最高优先级；当前写作要求只能补充表现方式，不能推翻既有真相。\n"
            "5. 以下是你的全部记忆库，请严格遵循相应的设定标签。\n"
        )

        truth_context = self._load_source_of_truth_context()
        if truth_context:
            prompt_parts.append(
                "<source_of_truth>\n"
                + truth_context +
                "\n【执行铁律】\n"
                "1. 不得改写已经发生过的核心事件顺序，尤其是第一次/第二次死机、倒计时是否冻结、父母生死与身世时间线。\n"
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

        # 批量模式：白名单喂料，只喂核心设定文件
        # 手动模式：仍按用户勾选
        _batch_whitelist = (
            "唯一真相", "时间线锚点", "当前卷大纲", "基调铁律",
        )
        selected_plot = []
        for name, var in self.plot_vars.items():
            if self.is_batch_running:
                # 白名单：只有包含白名单关键词的文件才入 prompt
                if not any(w in name for w in _batch_whitelist):
                    continue
            else:
                if not var.get():
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
        for name, var in self.chars_vars.items():
            if var.get():
                content = generator.read_text_safe(self.chars_map[name])
                rag.add_document(f"char_{doc_idx}", self.chars_map[name], name, content)
                doc_idx += 1
        for name, var in self.world_vars.items():
            if var.get():
                content = generator.read_text_safe(self.world_map[name])
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

        prompt_parts.append(
            "\n\n<chapter_quality_rules>\n"
            "【单章质量控制与多线防丢指令】(极度重要)：\n"
            "1. 格式要求：正文第一行必须是章节标题，格式为 第X章 标题（4-8字直接概括核心事件，绝不能与上一章标题相似）。禁止使用任何Markdown格式，不要在标题前加#号。\n"
            "2. 严禁注水与循环：如果不满字数，必须主动推进大纲的下一个节点！\n"
            "3. 杜绝流水账套路：禁止使用千篇一律的开头。\n"
            "4. 人设绝对锁定(反OOC)：角色的行为逻辑必须严格符合<character_profiles>的设定。\n"
            "5. 本章正文中文字数目标在3500-5500字之间，不要太短也不要太长。\n"
            "6. 视角统一：同一个场景内请保持主视角统一。\n"
            "7. 严禁说教与强行升华。\n"
            "8. 文笔要求：对话口语化有个性；避免连续三句以上相同句式；段落长短错落。\n"
            "9. 情节与悬念：本章需有2-3次情绪波动，结尾强制设置1个具体的悬念钩子。\n"

            "11. 【多线剧情防遗漏】：如果备忘录中有分兵/分头行动的角色，必须简要交代他们的当前处境。\n"
            "12. 逻辑连贯：要时刻思考其他角色在同一时间轴下在做什么。\n"
            "13. 【反代词堆叠与高频词控制】：禁止连续3句以上用'他/她/它'开头；'大脑'一词每章不超过4次（可换脑子/意识/思维）；'角色名+的'结构每章不超过8次。\n"
            "14. 【反AI腔节奏】：禁止把一句完整意思拆成3-5个独立短段来强行制造电影感；章尾悬念最多保留1-2个短锤句，其余必须用正常叙述落地。\n"
            "15. 【反诗化堆词】：慎用'不是……是……''某种……''像是……'和破折号——来硬造神秘感；除对白停顿外，破折号要克制，优先用动作、场景和细节制造压迫感。\n"
            "</chapter_quality_rules>\n"
        )
        return "\n".join(prompt_parts)

    def _load_source_of_truth_context(self):
        plot_dir = generator.DIRS["plot"]
        preferred_files = [
            "唯一真相设定表.txt",
            "唯一真相设定表.md",
            "时间线锚点.txt",
            "当前卷大纲.txt",
            "全局备忘录.txt",
            "伏笔与因果追踪表.txt",
        ]
        chunks = []
        added = set()
        spoiler_filter_files = {"全局备忘录.txt", "伏笔与因果追踪表.txt"}

        for fname in preferred_files:
            fpath = os.path.join(plot_dir, fname)
            if os.path.exists(fpath):
                content = generator.read_text_safe(fpath)
                if content:
                    added.add(os.path.normcase(fpath))
                    if fname in spoiler_filter_files:
                        content = self._filter_spoilers_from_text(content, self.next_chap)
                    chunks.append(f"【{fname}】\n{content[:2500]}")

        for pattern in ("*唯一真相*", "*时间线锚点*", "*设定总表*"):
            for fpath in glob.glob(os.path.join(plot_dir, pattern)):
                norm = os.path.normcase(fpath)
                if norm in added or not os.path.isfile(fpath):
                    continue
                content = generator.read_text_safe(fpath)
                if content:
                    chunks.append(f"【{os.path.basename(fpath)}】\n{content[:2500]}")
                    added.add(norm)

        # 不再自动拼入全部细纲候选。当前章细纲已在 batch_worker 里
        # 作为 outline_block 强制注入 user_prompt，source_of_truth 只保留
        # 设定类文件，避免未来卷细纲污染输入。
        # (原来的 outline candidates 自动注入已移除)

        return "\n\n".join(chunks)

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

            lines.append(f"[{topic['label']}] 最高 {level_names.get(max_lv, f'L{max_lv}')}")
            for ex in topic.get("allowed_examples", [])[:2]:
                lines.append(f"  ✅ 可以写：{ex}")
            for ex in topic.get("forbidden_examples", [])[:2]:
                lines.append(f"  ❌ 不能写：{ex}")
            lines.append("")

        lines.append("【铁律】如果不确定某句话是否超出预算，宁可写得更模糊。")
        lines.append("</reveal_budget>")
        return "\n".join(lines)

    def _filter_spoilers_from_text(self, text, chap_num):
        """过滤文本中超前于当前章节的剧透内容"""
        rules = self._load_reveal_rules()
        if not rules:
            return text

        # 分级过滤：不是只拦 earliest_hint 前，而是在所有尚未 hard 放开的阶段
        # 都拦截对应的 hard_patterns。这样即使进入 hint/soft 区间，
        # L4/L5 级机制描述也不会从备忘录侧喂给模型。
        blocked_patterns = []
        for topic in rules.get("topics", []):
            max_lv = self._get_max_reveal_level(topic, chap_num)
            if max_lv < 4:  # 只有 L4+ 才允许机制描述，否则拦截
                blocked_patterns.extend(topic.get("hard_patterns", []))

        if not blocked_patterns:
            return text

        filtered_lines = []
        for line in text.split("\n"):
            hit = False
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

        # 构建上下文
        extra_parts = []
        if chapter_outline:
            extra_parts.append(f"【本章细纲】\n{chapter_outline}")
        if prev_content:
            extra_parts.append(f"【上一章结尾】\n{prev_content[-200:]}")
        chars_dir = generator.DIRS.get("chars", "")
        main_char_path = os.path.join(chars_dir, "沈默.txt")
        if os.path.exists(main_char_path):
            char_card = generator.read_text_safe(main_char_path)
            extra_parts.append(f"【角色卡】\n{char_card[:1000]}")
        extra_context = "\n\n".join(extra_parts)

        def llm_fn(sys_p, user_p, temp):
            return self.call_llm_review(sys_p, user_p, temp=temp, max_tokens=1200)

        result = skill_engine.execute_skill(
            gate_config, chapter_content, llm_fn, extra_context=extra_context
        )
        return result.strip()

    def _run_release_guard(self, chapter_text, chap_num, prev_content=""):
        issues = generator.run_consistency_check(chapter_text, chap_num)
        forbidden = [i for i in issues if i["level"] == "🚫 禁止"]
        high_risk = [i for i in issues if i["keyword"] in self.HIGH_RISK_WATCH_KEYWORDS]
        style_risk = [i for i in issues if i["keyword"] in self.STYLE_RISK_KEYWORDS]

        if not forbidden and not high_risk and not style_risk:
            return {"status": "PASS", "summary": "未触发高风险设定拦截。", "issues": issues}

        truth_context = self._load_source_of_truth_context()
        gate_prompt = (
            "你是这本小说的'发布前设定总校'。你的职责不是润色，而是判断当前章节是否会"
            "改写既有真相、破坏时间线、把角色误判写成客观事实，或把同一核心事件重复写成新发生一次。\n"
            "请重点检查：\n"
            "1. 第一次/第二次死机是否被提前或重复书写\n"
            "2. 倒计时是否被错误冻结/重启/改数值\n"
            "3. 父母生死、车祸、左眼异常起点等身世锚点是否冲突\n"
            "4. 新组织/新Boss/新机制是否空降\n"
            "5. 章尾是否出现连续短句堆叠、破折号滥用、模糊词硬造氛围等明显AI腔\n\n"
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
            lines = content.split("\n")
            capturing = False
            result_lines = []
            body_started = False
            for line in lines:
                is_match = False
                if re.search(rf'第{cn_chap}章|第{chap_num}章', line):
                    is_match = True
                else:
                    range_match = re.search(r'第(\d+)章?-第(\d+)章', line)
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
                    next_chap_match = re.search(r'^第(\d+)章', line)
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
                return "\n".join(result_lines).strip()
        return ""

    # ============================================================
    # LLM 调用 (含 max_tokens Fix + None guard)
    # ============================================================
    def disable_buttons(self):
        for btn in [self.btn_new, self.btn_continue, self.btn_save_new, self.btn_save_append, self.btn_batch]:
            btn.config(state=tk.DISABLED)

    def enable_buttons(self, is_new=True):
        self.btn_new.config(state=tk.NORMAL)
        self.btn_continue.config(state=tk.NORMAL)
        self.btn_batch.config(state=tk.NORMAL)
        if is_new:
            self.btn_save_new.config(state=tk.NORMAL)
        else:
            self.btn_save_append.config(state=tk.NORMAL)

    def stream_call_llm(self, system_prompt, final_user_prompt, is_new=True):
        self.is_generating = True
        self._ui_clear("🚀 正在燃烧算力生成中，请稍候...\n\n")

        client = self.get_client()
        if client is None:
            self._ui(lambda: self.enable_buttons(is_new))
            self.is_generating = False
            return

        try:
            system_prompt, final_user_prompt, prompt_stats = self._apply_generation_prompt_budget(
                system_prompt, final_user_prompt
            )
            stream = client.chat.completions.create(
                model=self.get_model_name(),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": final_user_prompt}
                ],
                temperature=self.config.get("temperature", 0.8),
                max_tokens=self.config.get("max_tokens", 8192),
                stream=True
            )

            self._ui_clear()
            self.generated_content = ""
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    text_chunk = chunk.choices[0].delta.content
                    self.generated_content += text_chunk
                    self._ui_append(text_chunk)
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
            self._ui(lambda: self.enable_buttons(is_new))
            self._ui(self.update_word_count)

    def start_generation_thread(self, system_prompt, final_user_prompt, is_new):
        self.disable_buttons()
        thread = threading.Thread(target=self.stream_call_llm, args=(system_prompt, final_user_prompt, is_new))
        thread.daemon = True
        thread.start()

    def call_llm_non_stream(self, system_prompt, user_prompt, temp=0.3, max_tokens=None):
        client = self.get_client()
        if client is None:
            raise RuntimeError("未配置 API Key")
        token_limit = self.config.get("max_tokens", 8192)
        if max_tokens is not None:
            token_limit = min(token_limit, max_tokens)
        response = client.chat.completions.create(
            model=self.get_model_name(),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=temp,
            max_tokens=token_limit,
        )
        return self.strip_markdown_artifacts(response.choices[0].message.content or "")

    def call_llm_review(self, system_prompt, user_prompt, temp=0.15, max_tokens=1200):
        """用于质检/审核的 LLM 调用"""
        return self.call_llm_non_stream(system_prompt, user_prompt, temp=temp, max_tokens=max_tokens)

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
        # ★ 项目专属：将你的小说禁词添加在这里
        # 示例: "旧角色名", "旧地名",
        # 平台敏感词（通用）
        "共产党", "国家领导", "政府阴谋", "邪教",
        # 过度血腥（通用）
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
            return f"⚠ 未能从全书大纲中提取 {current_vol_name} 内容，请检查全书大纲。"

        if current_outline.strip() == desired_outline.strip():
            return None

        with open(outline_path, "w", encoding="utf-8") as f:
            f.write(desired_outline)

        return f"📋 已同步当前卷大纲为{current_vol_name}"

    def _check_content_drift(self, content):
        """快速内容漂移检测：禁词 + Markdown残留"""
        hits = [kw for kw in self.BANNED_KEYWORDS if kw in content]
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

    def batch_generate(self):
        # Feature 1: 批量前自动健康检查
        issues = self._run_health_check_internal()
        fail_items = [(desc, st) for desc, st in issues if st == "FAIL"]
        if fail_items:
            fail_detail = "\n".join(f"  ❌ {desc}" for desc, _ in fail_items)
            messagebox.showerror(
                "批量生成被阻止",
                f"健康检查发现 {len(fail_items)} 项 FAIL，必须先修复才能启动批量生成：\n\n{fail_detail}"
            )
            return

        count = simpledialog.askinteger("批量生成", "请输入要自动生成的章节数量：", minvalue=1, maxvalue=100)
        if not count:
            return

        self._stop_event.clear()
        self.is_batch_running = True
        self._ensure_batch_log_window()
        self._ui_progress_append(f"🚀 批量生成启动，共计划 {count} 章。\n\n", clear=True)
        self.disable_buttons()
        self.btn_stop.config(state=tk.NORMAL)

        thread = threading.Thread(target=self.batch_worker, args=(count,), daemon=True)
        thread.start()

    def batch_worker(self, total_count):
        client = self.get_client()
        if client is None:
            self._ui_progress_append("\n❌ 未配置 API Key，无法批量生成。\n")
            self.is_batch_running = False
            self._ui(lambda: self.enable_buttons(True))
            self._ui(lambda: self.btn_stop.config(state=tk.DISABLED))
            return

        batch_end_message = "🎉 批量生成完毕！"
        skipped_chapters = []

        for i in range(total_count):
            if self._stop_event.is_set():
                self._ui_progress_append(f"\n\n⏹ 已手动停止，共完成 {i} 章。\n")
                batch_end_message = "⏸ 批量生成已停止。"
                break

            # 刷新章节信息
            self.current_vol, self.next_chap, self.filepath, self.latest_chap, self.latest_filepath = generator.get_latest_chapter_info()
            chap_num = self.next_chap
            # Bug3: 锁定本轮存盘路径
            chapter_filepath = self.filepath

            vol_switch_msg = self._auto_switch_volume_outline(chap_num)
            if vol_switch_msg:
                # P1修复: 跨卷首章重新获取路径
                self.current_vol, self.next_chap, self.filepath, self.latest_chap, self.latest_filepath = generator.get_latest_chapter_info()
                chapter_filepath = self.filepath

            self._ui_clear()
            self._ui_progress_append(f"📝 批量模式 [{i + 1}/{total_count}] — 正在生成第 {chap_num} 章...\n")

            # 读取上一章内容
            prev_content = ""
            if self.latest_filepath and os.path.exists(self.latest_filepath):
                raw = generator.read_text_safe(self.latest_filepath)
                if "---" in raw:
                    raw = raw[:raw.rfind("---")].rstrip()
                prev_content = raw[-800:] if len(raw) > 800 else raw

            sys_prompt = self.build_system_prompt_gui(
                current_prompt=f"写第{chap_num}章内容。前情提要：{prev_content[-200:] if prev_content else ''}")

            # 提取细纲 (Section 10.1: 细纲强制注入)
            chapter_outline = self._extract_chapter_outline(chap_num)
            if not chapter_outline:
                volume_name = self._get_story_volume_name(chap_num) or "当前阶段"
                self._ui_progress_append(
                    f"  ⏸ 未找到第 {chap_num} 章逐章细纲，已暂停批量生成。\n"
                    f"  当前章节属于：{volume_name}\n"
                    f"  请先在 plot 目录补充对应细纲文件（例如“{volume_name}逐章细纲.txt”），再继续自动生成。\n"
                )
                break
            outline_guard = self._run_outline_reveal_guard(chapter_outline, chap_num)
            if outline_guard["status"] == "FAIL":
                self._ui_progress_append(
                    f"  ⚠ 第 {chap_num} 章细纲触发真相护栏，跳过此章。\n"
                    f"    🔒 {outline_guard['summary']}\n"
                )
                # 占位文件，防止死循环
                with open(chapter_filepath, "w", encoding="utf-8") as f:
                    f.write(f"第{chap_num}章 [待重写]\n\n本章因细纲触发真相护栏被跳过，需人工修复细纲后重新生成。\n")
                skipped_chapters.append(chap_num)
                continue
            elif outline_guard["status"] == "WARN":
                reveal_rules = self._load_reveal_rules()
                if reveal_rules and reveal_rules.get("strict_mode", False):
                    self._ui_progress_append(
                        f"  ⚠ 第 {chap_num} 章细纲触发真相护栏（严格模式），跳过此章。\n"
                        f"    🔒 {outline_guard['summary']}\n"
                    )
                    with open(chapter_filepath, "w", encoding="utf-8") as f:
                        f.write(f"第{chap_num}章 [待重写]\n\n本章因细纲触发真相护栏被跳过，需人工修复细纲后重新生成。\n")
                    skipped_chapters.append(chap_num)
                    continue
                else:
                    self._ui_progress_append(
                        f"  ⚠ 第 {chap_num} 章逐章细纲存在真相预警，但当前为非严格模式，继续生成。\n"
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
                    f"3. 章末悬念必须在最后200字内体现\n"
                    f"4. 禁止自创细纲中没有的新角色/势力/设定\n"
                    f"5. 以细纲为准，不自行修正细纲内容\n"
                    f"6. 【最高优先级】细纲就是你的剧本，你是演员不是编剧！\n"
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

            plan_prompt = f"""你现在要写第 {chap_num} 章。

请根据 <plot_and_memo> 标签中的大纲和备忘录，结合上一章结尾的剧情走向，直接输出本章的小说正文。

格式要求（极重要）：
- 第一行必须是章节标题，格式为"第{chap_num}章 标题"（标题简洁明了4-8字）
- 标题后空一行再写正文
- 禁止使用任何Markdown格式，不要在标题前加#号
{title_hint}
内容要求：
1. 紧接上一章的剧情自然展开，不要重复已经发生的事。
2. 【反重复铁律】：凡是已经发生过的事件，本章绝对禁止再写一遍！
3. 本章需推进至少1个主线事件，制造1个悬念钩子。
4. 【最高优先级】细纲就是你的剧本，你是演员不是编剧！
5. 本章正文中文字数目标在3500-5500字之间，不要太短也不要太长。
6. 【严禁回绕】：写完就停，绝对不要重复前面已经写过的段落或场景！
7. 【反AI腔】不要把一句完整意思拆成多行短句；章尾悬念最多保留1-2个短锤句，不要整段都写成“他知道。她看见。它在等。”。
8. 【反诗化堆词】少用“某种”“像是”“不是……是……”和破折号——来硬造神秘感，优先用动作、细节、场景推进压迫感。
9. 【反幻觉·最高优先级】严禁引用前文中没有出现过的角色、对话、实体或事件。如果你不确定某事是否已经发生，就不要提它。严禁编造"某角色说过XXX""之前发现了XXX"等虚假回忆。只使用上一章结尾片段和细纲中明确给出的信息。
{outline_block}

【上一章结尾片段（请衔接）】：
{prev_content if prev_content else "（这是第一章，无上文）"}"""

            max_retries = 3
            retry_count = 0
            chapter_content = ""
            _quality_gate_skip_content = ""
            success = False
            blocked_by_quality_gate = False
            fatal_api_stop_msg = ""
            _retry_feedback = []  # 收集失败原因，重试时回注给模型

            while retry_count < max_retries and not success and not self._stop_event.is_set():
                try:
                    self._ui_clear()
                    sys_prompt_run, plan_prompt_run, prompt_stats = self._apply_generation_prompt_budget(
                        sys_prompt, plan_prompt
                    )
                    if retry_count == 0:
                        self._ui_progress_append(
                            f"  📏 输入估算：{prompt_stats['final']['input_tokens']} tokens"
                            f"（system {prompt_stats['final']['system_chars']} chars / "
                            f"user {prompt_stats['final']['user_chars']} chars）\n"
                        )
                    elif prompt_stats["trimmed"]:
                        self._ui_progress_append(
                            f"  ✂ 重试前裁剪上下文：{prompt_stats['original']['input_tokens']} → "
                            f"{prompt_stats['final']['input_tokens']} tokens\n"
                        )
                    stream = client.chat.completions.create(
                        model=self.get_model_name(),
                        messages=[
                            {"role": "system", "content": sys_prompt_run},
                            {"role": "user", "content": plan_prompt_run}
                        ],
                        temperature=self.config.get("temperature", 0.8),
                        max_tokens=self.config.get("max_tokens", 8192),
                        stream=True
                    )

                    if retry_count > 0:
                        self._ui_progress_append(f"  ↻ 第 {retry_count} 次重试中...\n")
                        # 将失败原因回注给模型
                        if _retry_feedback:
                            feedback_block = (
                                "\n\n━━━━━━ 上一次生成失败，请严格规避以下问题 ━━━━━━\n"
                                + "\n".join(_retry_feedback)
                                + "\n━━━━━━ 绝对禁止再犯以上问题 ━━━━━━\n"
                            )
                            plan_prompt += feedback_block
                            _retry_feedback.clear()

                    chapter_content = ""
                    for chunk in stream:
                        if chunk.choices and chunk.choices[0].delta.content:
                            text_chunk = chunk.choices[0].delta.content
                            chapter_content += text_chunk
                            self._ui_append(text_chunk)

                    chapter_content = self.strip_markdown_artifacts(chapter_content)

                    # Markdown 标题清理 (Section 11.2)
                    lines = chapter_content.split("\n", 1)
                    if lines and lines[0].strip().startswith("#"):
                        lines[0] = lines[0].strip().lstrip("#").strip()
                        chapter_content = "\n".join(lines)

                    # 自动去重：检测并截断重复内容块
                    chapter_content = self._dedup_chapter(chapter_content)

                    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', chapter_content))

                    # 自动扩写 (目标: 3500字, 仅触发一次)
                    if chinese_chars < 3500 and not self._stop_event.is_set():
                        self._ui_progress_append(f"  ⚠ 字数不足（{chinese_chars}字），触发自动扩写...\n")
                        expand_prompt = plan_prompt + f"\n\n你刚生成的章节内容如下：\n{chapter_content}\n\n【强烈指令】：以上内容字数严重不足。请以这段内容为基础，继续向后发展剧情并展开深入描写，输出完整合规的本章正文。不要带'接着前文'等废话。"
                        sys_prompt_expand, expand_prompt_run, expand_stats = self._apply_generation_prompt_budget(
                            sys_prompt, expand_prompt
                        )
                        self._ui_progress_append(
                            f"  📏 扩写输入估算：{expand_stats['final']['input_tokens']} tokens\n"
                        )
                        expand_stream = client.chat.completions.create(
                            model=self.get_model_name(),
                            messages=[
                                {"role": "system", "content": sys_prompt_expand},
                                {"role": "user", "content": expand_prompt_run}
                            ],
                            temperature=self.config.get("temperature", 0.8),
                            max_tokens=self.config.get("max_tokens", 8192),
                            stream=True
                        )
                        expanded_content = ""
                        for chunk in expand_stream:
                            if chunk.choices and chunk.choices[0].delta.content:
                                text_chunk = chunk.choices[0].delta.content
                                expanded_content += text_chunk
                                self._ui_append(text_chunk)
                        chapter_content += "\n\n" + self.strip_markdown_artifacts(expanded_content)

                    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', chapter_content))

                    # 超长截断: 5800字以上按段落边界截到5200-5500
                    if chinese_chars > 5800:
                        chapter_content = self._truncate_chapter(chapter_content, 5300)
                        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', chapter_content))
                        self._ui_progress_append(f"  ✂ 字数过长，已截断至 {chinese_chars} 字\n")

                    # 丢弃重试下限: 3200字(软容差下限)
                    if chinese_chars < 3200:
                        self._ui_progress_append(f"  ⚠ 字数不足（{chinese_chars}字），丢弃重试...\n")
                        _retry_feedback.append(f"【字数不足】上次只写了{chinese_chars}字，请确保本次正文中文字数在3500-5500字之间。")
                        retry_count += 1
                        time.sleep(3)
                        continue

                    # 内容漂移防火墙
                    drift_hits = self._check_content_drift(chapter_content)
                    if drift_hits and retry_count < max_retries - 1:
                        self._ui_progress_append(f"  🛡️ 漂移防火墙：检测到禁词 {drift_hits}，丢弃重新生成...\n")
                        _retry_feedback.append(f"【内容漂移】上次命中禁词：{', '.join(drift_hits)}。本次写作中严禁出现这些词语。")
                        retry_count += 1
                        time.sleep(3)
                        continue

                    # 标题去重
                    first_line = chapter_content.strip().split("\n")[0].lstrip("#").strip()
                    new_title = re.sub(r'^第\d+章\s*', '', first_line)
                    if new_title and new_title in all_titles_set and retry_count < max_retries - 1:
                        self._ui_progress_append(f"  🔄 标题重复：'{new_title}'，丢弃重新生成...\n")
                        plan_prompt += f"\n\n【紧急】标题'{new_title}'已被使用过，严禁再用！"
                        retry_count += 1
                        time.sleep(3)
                        continue

                    # 标题章节号校验
                    title_chap_match = re.match(r'第(\d+)章', first_line)
                    if title_chap_match and int(title_chap_match.group(1)) != chap_num:
                        chapter_content = re.sub(r'^第\d+章', f'第{chap_num}章', chapter_content, count=1)

                    # ---- 质检审核官 (使用 quality_gate.json，FAIL 触发重试) ----
                    self._ui_progress_append(f"  🛡️ 质检审核中...")
                    try:
                        gate_result = self._run_quality_gate(chapter_content, chapter_outline, prev_content)
                        if gate_result.startswith("FAIL"):
                            if retry_count < 2:
                                self._ui_progress_append(f" FAIL！丢弃重写\n    {gate_result[:200]}...\n")
                                _retry_feedback.append(f"【质检失败】{gate_result[:300]}")
                                retry_count += 1
                                time.sleep(3)
                                continue
                            else:
                                self._ui_progress_append(f" FAIL（重试{retry_count}次仍未通过，保存草稿并跳过）\n    {gate_result[:200]}...\n")
                                blocked_by_quality_gate = True
                                _quality_gate_skip_content = chapter_content
                                retry_count = max_retries
                                break
                        else:
                            self._ui_progress_append(f" PASS ✅\n")
                    except Exception as e:
                        self._ui_progress_append(f" ⚠ 质检报错(跳过): {str(e)[:80]}\n")

                    # ---- 发布前设定总校 ----
                    release_guard = self._run_release_guard(chapter_content, chap_num, prev_content=prev_content)
                    if release_guard["status"] == "FAIL":
                        self._ui_progress_append(f"\n  🧱 设定拦截：{release_guard['summary']}\n")
                        if release_guard.get("raw"):
                            self._ui_progress_append("    " + release_guard["raw"][:400] + "\n")
                        if retry_count < 2:
                            _retry_feedback.append(f"【设定拦截】{release_guard['summary']}")
                            retry_count += 1
                            time.sleep(3)
                            continue
                        else:
                            self._ui_progress_append(f"  🧱 设定拦截连续{retry_count}次，保存草稿并跳过\n")
                            blocked_by_quality_gate = True
                            _quality_gate_skip_content = chapter_content
                            retry_count = max_retries
                            break
                    if release_guard["status"] == "WARN":
                        self._ui_progress_append(f"\n  ⚠ 设定预警：{release_guard['summary']}\n")
                        if release_guard.get("raw"):
                            self._ui_progress_append("    " + release_guard["raw"][:300] + "\n")

                    # ---- 真相分级放行检查 (Reveal Guard v2) ----
                    self._ui_progress_append(f"  🔒 真相节奏检查中...")
                    truth_guard = self._run_truth_reveal_guard(chapter_content, chap_num)
                    if truth_guard["status"] == "FAIL":
                        self._ui_progress_append(f" FAIL\n    🔒 真相越界：{truth_guard['summary']}\n")
                        _retry_feedback.append(
                            f"【真相越界】{truth_guard['summary']}\n"
                            f"严禁在本章中提及以下内容：{truth_guard.get('raw', '')[:200]}"
                        )
                        retry_count += 1
                        time.sleep(3)
                        continue
                    elif truth_guard["status"] == "WARN":
                        reveal_rules = self._load_reveal_rules()
                        if reveal_rules and reveal_rules.get("strict_mode", False):
                            self._ui_progress_append(f" FAIL(严格模式)\n    🔒 真相越界：{truth_guard['summary']}\n")
                            _retry_feedback.append(
                                f"【真相越界-严格模式】{truth_guard['summary']}\n"
                                f"严禁在本章中提及以下内容：{truth_guard.get('raw', '')[:200]}"
                            )
                            retry_count += 1
                            time.sleep(3)
                            continue
                        else:
                            self._ui_progress_append(f" WARN（已放行）\n    ⚠ {truth_guard['summary']}\n")
                    else:
                        self._ui_progress_append(f" PASS ✅\n")

                    success = True

                except Exception as e:
                    err_msg = str(e)
                    err_lower = err_msg.lower()
                    is_balance_error = (
                        "insufficient balance" in err_lower
                        or "余额不足" in err_msg
                        or "error code: 402" in err_lower
                    )
                    if is_balance_error:
                        fatal_api_stop_msg = "余额不足（402 Insufficient Balance），本轮已停止。请充值、检查 API 账户，或切换可用模型后再继续。"
                        self._ui_progress_append(f"\n  ❌ 生成中断: {fatal_api_stop_msg}\n")
                        retry_count = max_retries
                        break

                    if "maximum context length" in err_lower:
                        try:
                            self._ui_progress_append(
                                f"\n  ❌ 上下文超限：估算输入 {prompt_stats['final']['input_tokens']} tokens"
                                f"（system {prompt_stats['final']['system_chars']} chars / "
                                f"user {prompt_stats['final']['user_chars']} chars）\n"
                            )
                        except Exception:
                            self._ui_progress_append("\n  ❌ 上下文超限。\n")

                    self._ui_progress_append(f"\n  ❌ 生成中断: {err_msg[:80]}\n  等待5秒后重试...\n")
                    retry_count += 1
                    time.sleep(5)

            if not success:
                if self._stop_event.is_set():
                    self._ui_progress_append("⏸ 用户停止，当前章节未成功生成，跳过保存。")
                    batch_end_message = "⏸ 批量生成已停止。"
                    break

                if fatal_api_stop_msg:
                    self._ui_progress_append(f"\n\n❌ {fatal_api_stop_msg}\n")
                    batch_end_message = "❌ 批量生成已中止（余额不足）。"
                    break

                if blocked_by_quality_gate:
                    # 质检/设定拦截未通过，但仍存为正式章节保持叙事连贯
                    save_content = _quality_gate_skip_content or chapter_content
                    if save_content:
                        with open(chapter_filepath, "w", encoding="utf-8") as f:
                            f.write(save_content)
                        chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', save_content))
                        self._ui_progress_append(
                            f"\n  ⚠ 第 {chap_num} 章质检未通过，但已强制保存({chinese_chars}字)以保持连贯\n"
                            f"  ▶ 继续生成下一章...\n\n"
                        )
                        skipped_chapters.append(chap_num)
                        # 也跑记忆维护，保证后续上下文不断
                        chapter_content = save_content
                        self._run_skill_pipeline(chap_num, chapter_filepath, chapter_content, prev_content)
                        continue
                    # 如果连内容都没有，跳过
                    skipped_chapters.append(chap_num)
                    continue

                # 通用失败兜底：同样存为正式文件
                if chapter_content:
                    with open(chapter_filepath, "w", encoding="utf-8") as f:
                        f.write(chapter_content)
                    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', chapter_content))
                    self._ui_progress_append(
                        f"\n  ⚠ 第 {chap_num} 章连续失败{max_retries}次，已强制保存({chinese_chars}字)以保持连贯\n"
                        f"  ▶ 继续生成下一章...\n\n"
                    )
                    skipped_chapters.append(chap_num)
                    self._run_skill_pipeline(chap_num, chapter_filepath, chapter_content, prev_content)
                    continue
                skipped_chapters.append(chap_num)
                continue

            # ---- 保存章节 ----
            with open(chapter_filepath, "w", encoding="utf-8") as f:
                f.write(chapter_content)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            history_path = os.path.join(generator.DIRS["hist"], f"Ch{chap_num}_{timestamp}.txt")
            with open(history_path, "w", encoding="utf-8") as f:
                f.write(f"[批量生成] 第{chap_num}章\n\n" + "-" * 40 + "\n\n" + chapter_content)

            self._ui_clear(chapter_content)
            self._ui_progress_append(f"  ✅ 第 {chap_num} 章已保存！({chinese_chars}字)\n")
            self._ui(self.refresh_reader_files_silent)

            # ---- 跨章一致性快检 ----
            self._run_cross_chapter_check(chapter_content, chap_num, chapter_filepath)

            # ---- 技能流水线 (Section 9.2) ----
            self._run_skill_pipeline(chap_num, chapter_filepath, chapter_content, prev_content)

            # 自动完结检测
            ending_markers = ["（全书完）", "（完）", "全书完", "—— 全文完 ——", "（大结局）", "the end", "【完结】", "（全文完）"]
            last_200 = chapter_content[-200:].lower()
            if any(m.lower() in last_200 for m in ending_markers):
                self._ui_progress_append(f"\n\n🎉 AI已写出完结标志，全书完结！共 {chap_num} 章。")
                break

            # 章节间冷却 + 停止检查 (P0修复: 保存完毕后才响应停止)
            if self._stop_event.is_set():
                self._ui_progress_append(f"\n⏸ 用户停止，第 {chap_num} 章已完整保存，停止批量生成。")
                break
            time.sleep(2)

        # 完成
        self._ui(self.refresh_status)
        if skipped_chapters:
            skip_str = '、'.join(str(c) for c in skipped_chapters)
            self._ui_progress_append(f"\n\n⚠ 以下章节质检未通过，已保存为 _待审.txt 草稿，需人工处理：第 {skip_str} 章\n")
        self._ui_progress_append(f"\n{'=' * 40}\n{batch_end_message}\n")
        self.is_batch_running = False
        self._ui(lambda: self.btn_new.config(state=tk.NORMAL))
        self._ui(lambda: self.btn_continue.config(state=tk.NORMAL))
        self._ui(lambda: self.btn_batch.config(state=tk.NORMAL))
        self._ui(lambda: self.btn_stop.config(state=tk.DISABLED))
        self.is_generating = False
        self._ui(self.update_word_count)

    def _dedup_chapter(self, content):
        """检测并截断AI回绕重复的内容（中途出现第二个章节标题）"""
        lines = content.split('\n')
        title_pat = re.compile(r'^第[一二三四五六七八九十百零\d]+章[\s\u3000]')
        title_positions = []
        for i, line in enumerate(lines):
            stripped = line.strip().replace('\r', '')
            if title_pat.match(stripped):
                title_positions.append(i)

        if len(title_positions) <= 1:
            return content  # 没有重复

        # 截断到第一个标题段
        kept = lines[:title_positions[1]]
        while kept and not kept[-1].strip():
            kept.pop()

        new_content = '\n'.join(kept)
        new_chars = len(re.findall(r'[\u4e00-\u9fff]', new_content))

        if new_chars >= 3200:
            self._ui_progress_append(f"  🔧 检测到AI回绕重复，已截断 (保留{new_chars}字)\n")
            return new_content
        return content  # 截断后太短，保留原文

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

    def _run_skill_pipeline(self, chap_num, chapter_filepath, chapter_content, prev_content):
        """技能流水线：润色→台词教练→钩子→多维审查→实体追踪→编年史→伏笔→记忆压缩"""
        try:
            import skill_engine
        except ImportError:
            self._ui_progress_append("\n⚠ skill_engine 未找到，跳过技能流水线\n")
            return

        skills_dir = os.path.join(_exe_dir, "skills")
        if not os.path.isdir(skills_dir):
            skills_dir = os.path.join(self.project_dir, "skills")
        if not os.path.isdir(skills_dir):
            return

        # 按顺序执行的技能列表
        # 托管批量优先保证“不断线”和“省 token”，编辑类技能留给手动精修
        if self.is_batch_running:
            pipeline_skills = ["memory_compressor"]
            if chap_num % 3 == 0:
                pipeline_skills.extend([
                    "entity_extractor",
                    "chronicle_keeper",
                    "foreshadow_hunter",
                ])
            self._ui_progress_append("\n  ⚙ 托管模式：跳过润色/钩子，仅保留记忆维护链路")
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
            self._ui_progress_append(f"\n  🔧 {skill_name}...")

            # 构建上下文
            extra_context = ""
            if skill_config.get("requires_context"):
                memo_path = os.path.join(generator.DIRS["plot"], "全局备忘录.txt")
                extra_context = generator.read_text_safe(memo_path)
                if prev_content:
                    extra_context += f"\n\n【上一章结尾200字】\n{prev_content[-200:]}"

            # 构建 LLM 调用函数（匹配 skill_engine.execute_skill 签名）
            def _make_llm_fn():
                _client = self.get_client()
                _model = self.get_model_name()
                _max_tokens = min(
                    self.config.get("max_tokens", 8192),
                    skill_config.get("max_tokens", skill_max_tokens.get(skill_id, 1500))
                )
                def llm_fn(system_prompt, user_prompt, temperature):
                    if _client is None:
                        raise RuntimeError("未配置 API Key")
                    response = _client.chat.completions.create(
                        model=_model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        temperature=temperature,
                        max_tokens=_max_tokens,
                    )
                    return self.strip_markdown_artifacts(response.choices[0].message.content or "")
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
                self._ui_progress_append(f" ❌ 失败: {str(e)[:60]}")
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

                # 备忘录损坏保护
                if save_filename == "全局备忘录.txt" and len(result.strip()) < 50:
                    self._ui_progress_append(" ⚠ 压缩结果过短，保留原备忘录")
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
                self._ui_progress_append(" ✅")

            elif output_type in ("replace_editor", "editor_replace"):
                # P0 修复：批量托管模式禁止任何技能替换正文
                if self.is_batch_running:
                    self._ui_progress_append(f" ⏭ 托管模式跳过正文替换")
                    continue
                # replace_editor: 润色/台词/钩子 — 替换章节内容
                result_chars = len(re.findall(r'[\u4e00-\u9fff]', result))
                original_chars = len(re.findall(r'[\u4e00-\u9fff]', current_content))

                # 字数回滚保护 (Section 10.3)
                if result_chars < 1800 or result_chars < original_chars * 0.8:
                    self._ui_progress_append(f" ⚠️ 字数暴跌({original_chars}→{result_chars})，回滚")
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

                    current_content = result
                    with open(chapter_filepath, "w", encoding="utf-8") as f:
                        f.write(current_content)
                    self._ui_progress_append(" ✅")
            else:
                # popup 或其他: 只显示不替换
                self._ui_progress_append(" ✅")

            time.sleep(2)  # 技能间冷却

        if current_content != chapter_content:
            self._ui_clear(current_content)

    def stop_batch(self):
        self._stop_event.set()
        self.btn_stop.config(state=tk.DISABLED)
        self._ui_progress_append("\n\n⏸ 正在停止，当前章节完成后会安全停下...\n")

    # ============================================================
    # 保存功能
    # ============================================================
    def save_new_chapter(self):
        content = self.strip_markdown_artifacts(self.result_text.get(1.0, tk.END).strip())
        if not content:
            return
        guard = self._run_release_guard(content, self.next_chap)
        if guard["status"] == "FAIL":
            messagebox.showerror("设定拦截", f"检测到高风险设定冲突，已阻止保存。\n\n{guard['summary']}")
            return
        if guard["status"] == "WARN":
            detail = ("\n\n详细提示：\n" + guard["raw"][:500]) if guard.get("raw") else ""
            if not messagebox.askyesno("设定预警", f"检测到潜在设定风险：\n\n{guard['summary']}{detail}\n\n仍然继续保存吗？"):
                return
        with open(self.filepath, "w", encoding="utf-8") as f:
            f.write(content)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        history_path = os.path.join(generator.DIRS["hist"], f"Ch{self.next_chap}_{timestamp}.txt")
        with open(history_path, "w", encoding="utf-8") as f:
            f.write(self.current_req + "\n\n" + "-" * 40 + "\n\n" + content)
        messagebox.showinfo("成功", f"第 {self.next_chap} 章保存成功！")
        self.btn_save_new.config(state=tk.DISABLED)
        self.refresh_status()

    def save_append_chapter(self):
        content = self.strip_markdown_artifacts(self.result_text.get(1.0, tk.END).strip())
        if not content:
            return
        existing = generator.read_text_safe(self.latest_filepath) if self.latest_filepath else ""
        combined = (existing.rstrip() + "\n\n" + content).strip()
        guard = self._run_release_guard(combined, self.latest_chap, prev_content=existing)
        if guard["status"] == "FAIL":
            messagebox.showerror("设定拦截", f"检测到高风险设定冲突，已阻止追加保存。\n\n{guard['summary']}")
            return
        if guard["status"] == "WARN":
            detail = ("\n\n详细提示：\n" + guard["raw"][:500]) if guard.get("raw") else ""
            if not messagebox.askyesno("设定预警", f"检测到潜在设定风险：\n\n{guard['summary']}{detail}\n\n仍然继续追加保存吗？"):
                return
        with open(self.latest_filepath, "a", encoding="utf-8") as f:
            f.write("\n\n" + content)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        history_path = os.path.join(generator.DIRS["hist"], f"Ch{self.latest_chap}_续写_{timestamp}.txt")
        with open(history_path, "w", encoding="utf-8") as f:
            f.write(self.current_req + "\n\n" + "-" * 40 + "\n\n" + content)
        messagebox.showinfo("成功", f"第 {self.latest_chap} 章续写追加成功！")
        self.btn_save_append.config(state=tk.DISABLED)

    # ============================================================
    # 自动保存
    # ============================================================
    def auto_save_loop(self):
        if not self.is_generating:
            current_text = self.result_text.get(1.0, tk.END).strip()
            if current_text and current_text != self.last_saved_text and not current_text.startswith("🚀"):
                autosave_path = os.path.join(generator.DIRS["out"], "autosave_draft.txt")
                try:
                    with open(autosave_path, "w", encoding="utf-8") as f:
                        f.write(current_text)
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
            self.disable_buttons()
            self._ui_clear("🧠 正在提炼记忆...\n")
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
                self._ui_clear(f"✅ 记忆备忘录已更新！\n\n{memo_content}")
                self._ui(lambda: messagebox.showinfo("成功", "记忆备忘录已更新！"))
            except Exception as e:
                self._ui(lambda: messagebox.showerror("错误", f"提炼记忆失败: {str(e)}"))
            finally:
                self._ui(lambda: self.enable_buttons(True))

        threading.Thread(target=do_compress, daemon=True).start()

    # ============================================================
    # 工具箱：伏笔追踪
    # ============================================================
    def track_foreshadowing(self):
        if not self.latest_filepath or not os.path.exists(self.latest_filepath):
            messagebox.showwarning("提示", "尚无已写章节！")
            return
        text = generator.read_text_safe(self.latest_filepath)
        old_table = generator.read_text_safe(os.path.join(generator.DIRS["plot"], "伏笔与因果追踪表.txt"))

        sys_prompt = "你是长篇小说的\"伏笔与因果追踪系统\"。"
        user_prompt = f"""请从以下最新章节中提取所有伏笔、未解悬念、已收回的伏笔，并与旧表格合并更新。

输出格式（Markdown表格）：
| 编号 | 伏笔描述 | 埋设章节 | 状态(未解/已收) | 收回章节 | 备注 |

【旧伏笔追踪表】：{old_table if old_table else "暂无。"}

【最新章节内容】：{text}"""

        def do_track():
            self.disable_buttons()
            self._ui_clear("🔮 正在追踪伏笔...\n")
            try:
                table = self.call_llm_non_stream(sys_prompt, user_prompt, temp=0.2)
                path = os.path.join(generator.DIRS["plot"], "伏笔与因果追踪表.txt")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(table)
                self._ui_clear(f"✅ 伏笔追踪表已更新！\n\n{table}")
                self._ui(lambda: messagebox.showinfo("成功", "伏笔追踪表已更新！"))
            except Exception as e:
                self._ui(lambda: messagebox.showerror("错误", f"伏笔追踪失败: {str(e)}"))
            finally:
                self._ui(lambda: self.enable_buttons(True))

        threading.Thread(target=do_track, daemon=True).start()

    # ============================================================
    # 工具箱：进度编年史
    # ============================================================
    def update_chronicle(self):
        if not self.latest_filepath or not os.path.exists(self.latest_filepath):
            messagebox.showwarning("提示", "尚无已写章节！")
            return

        text = generator.read_text_safe(self.latest_filepath)
        chronicle_path = os.path.join(generator.DIRS["plot"], "世界编年史.txt")
        old_chronicle = generator.read_text_safe(chronicle_path)

        sys_prompt = "你是这本小说的'时间轴与编年史管理员'。"
        user_prompt = f"""请分析【最新章节内容】，提取时间流逝和重大里程碑事件，追加更新编年史。
格式：[相对时间戳] 第X章发生的里程碑简述。

【已有编年史参考】：{old_chronicle if old_chronicle else "（暂无）"}

【第 {self.latest_chap} 章最新内容】：{text}"""

        def do_update():
            self.disable_buttons()
            self._ui_clear("📜 正在推演时间轴...\n")
            try:
                new_entry = self.call_llm_non_stream(sys_prompt, user_prompt, temp=0.1)
                with open(chronicle_path, "a", encoding="utf-8") as f:
                    if not old_chronicle:
                        f.write("=== 小世界核心编年史 ===\n\n")
                    f.write(f"{new_entry}\n")
                self._ui_clear(f"✅ 编年史已更新：\n\n{new_entry}")
                self._ui(lambda: messagebox.showinfo("成功", "世界编年史已更新！"))
            except Exception as e:
                self._ui(lambda: messagebox.showerror("错误", f"编年史更新失败: {str(e)}"))
            finally:
                self._ui(lambda: self.enable_buttons(True))

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
                if fname.endswith(".txt") and fname.startswith("第") and "章" in fname:
                    files.append(os.path.join(root_dir, fname))
        files.sort(key=self._extract_chap_num_from_path)

        if not files:
            messagebox.showinfo("提示", "当前没有已保存的章节文件。")
            return

        win = tk.Toplevel(self.root)
        win.title("📂 历史章节")
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

        ttk.Button(win, text="📖 加载到编辑区", command=load_selected).pack(pady=10)

    # ============================================================
    # 一键打包导出
    # ============================================================
    def export_book(self):
        out_dir = generator.DIRS.get("out", "output")
        if not os.path.exists(out_dir):
            messagebox.showwarning("提示", "当前没有输出文件夹！")
            return

        chapter_files = []
        for root_dir, dirs, files in os.walk(out_dir):
            dirs[:] = [d for d in dirs if d != '.backup']
            for f in files:
                if f.endswith(".txt") and f.startswith("第") and "章" in f:
                    chapter_files.append(os.path.join(root_dir, f))
        chapter_files.sort(key=self._extract_chap_num_from_path)

        if not chapter_files:
            messagebox.showinfo("提示", "当前没有写好的章节。")
            return

        export_path = filedialog.asksaveasfilename(
            title="一键排版导出全书为 TXT",
            defaultextension=".txt",
            filetypes=[("Text file", "*.txt")],
            initialfile=f"我的小说_精排版_{datetime.now().strftime('%Y%m%d')}.txt"
        )
        if not export_path:
            return

        try:
            with open(export_path, "w", encoding="utf-8") as out_f:
                for fpath in chapter_files:
                    try:
                        content = generator.read_text_safe(fpath)
                    except Exception:
                        continue
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
            messagebox.showinfo("成功", f"全书已一键排版导出至：\n{export_path}")
        except Exception as e:
            messagebox.showerror("导出失败", f"导出过程中遇到错误: {str(e)}")

    # ============================================================
    # Feature 1: 健康检查
    # ============================================================
    def _run_health_check_internal(self):
        """执行健康检查，返回 [(检查项, "PASS"/"FAIL"/"WARN"), ...]"""
        results = []
        _, next_chap, _, latest_chap, _ = generator.get_latest_chapter_info()

        # API Key 检查
        for preset_name, preset in MODEL_PRESETS.items():
            key_field = preset.get("config_key_field", "api_key")
            if self.config.get(key_field):
                results.append((f"{preset_name} API Key", "PASS"))
            else:
                results.append((f"{preset_name} API Key", "FAIL"))

        # 大纲文件检查
        for fname in ("全书大纲.txt", "当前卷大纲.txt", "全局备忘录.txt"):
            fpath = os.path.join(generator.DIRS["plot"], fname)
            if os.path.exists(fpath) and os.path.getsize(fpath) > 10:
                results.append((f"大纲: {fname}", "PASS"))
            else:
                results.append((f"大纲: {fname}", "WARN"))

        # 关键真相锚点（缺失应为 FAIL，这两个是防设定串线的核心文件）
        for fname in ("时间线锚点.txt", "伏笔与因果追踪表.txt"):
            fpath = os.path.join(generator.DIRS["plot"], fname)
            if os.path.exists(fpath) and os.path.getsize(fpath) > 10:
                results.append((f"真相锚点: {fname}", "PASS"))
            else:
                results.append((f"真相锚点: {fname}", "FAIL"))

        # 逐章细纲
        outline_files = self._get_outline_candidate_files(next_chap)
        if outline_files:
            results.append((f"逐章细纲文件({len(outline_files)}个)", "PASS"))
        else:
            results.append(("逐章细纲文件", "WARN"))

        next_outline = self._extract_chapter_outline(next_chap)
        if next_outline:
            results.append((f"下一章细纲覆盖: 第{next_chap}章", "PASS"))
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
            results.append((f"下一章细纲覆盖: 第{next_chap}章", "FAIL"))

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
                results.append((f"技能: {skill_id}", "WARN"))

        # 输出目录
        out_dir = generator.DIRS["out"]
        if os.path.isdir(out_dir):
            results.append(("输出目录", "PASS"))
        else:
            results.append(("输出目录", "FAIL"))

        # 唯一真相设定表
        canon_path = os.path.join(generator.DIRS["plot"], "唯一真相设定表.md")
        if os.path.exists(canon_path) and os.path.getsize(canon_path) > 100:
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
                results.append((f"reveal_rules.json ({len(topics)}个主题)", "PASS"))
            except Exception:
                results.append(("reveal_rules.json (JSON无效)", "FAIL"))
        else:
            results.append(("reveal_rules.json", "WARN"))

        # cross_chapter_scanner 可用性
        scanner_path = os.path.join(_exe_dir, "cross_chapter_scanner.py")
        if os.path.exists(scanner_path):
            results.append(("跨章扫描器", "PASS"))
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

            # 3. 每10章跑一次事件去重和时间线检查
            if chap_num % 10 == 0:
                self._ui_progress_append(f"  🔍 第{chap_num}章触发定期跨章扫描...\n")
                # 扫描最近30章
                start_scan = max(1, chap_num - 30)
                vol_dir = os.path.dirname(chapter_filepath)
                scan_files = cross_chapter_scanner.get_chapter_files(vol_dir, start_scan, chap_num)
                scan_data = []
                for fp in scan_files:
                    num, title, text = cross_chapter_scanner.read_chapter(fp)
                    if num is not None:
                        scan_data.append((num, title, text))
                scan_data.sort(key=lambda x: x[0])

                events = cross_chapter_scanner.scan_events(scan_data)
                issues.extend(events)
                timeline = cross_chapter_scanner.scan_timeline_jumps(scan_data)
                issues.extend(timeline)

            # 输出结果
            if issues:
                high_issues = [i for i in issues if i.get('severity') == 'HIGH']
                med_issues = [i for i in issues if i.get('severity') == 'MEDIUM']
                if high_issues:
                    self._ui_progress_append(f"  ⚠ 跨章扫描发现 {len(high_issues)} 个高优先级问题:\n")
                    for iss in high_issues[:3]:  # 最多显示3条
                        self._ui_progress_append(f"     {iss['message'][:120]}\n")
                if med_issues:
                    self._ui_progress_append(f"  ℹ 跨章扫描发现 {len(med_issues)} 个中优先级问题\n")
            else:
                if chap_num % 10 == 0:
                    self._ui_progress_append(f"  ✅ 跨章扫描通过\n")

            return issues

        except Exception as e:
            self._ui_progress_append(f"  ⚠ 跨章扫描异常: {str(e)[:80]}\n")
            return []

    def health_check(self):
        results = self._run_health_check_internal()
        report_lines = ["🩺 健康检查报告\n" + "=" * 40 + "\n"]
        for item, status in results:
            icon = "✅" if status == "PASS" else "❌" if status == "FAIL" else "⚠️"
            report_lines.append(f"  {icon} [{status}] {item}")
        report = "\n".join(report_lines)
        self.result_text.delete(1.0, tk.END)
        self.result_text.insert(tk.END, report)

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
        win.title("👤 实体追踪面板")
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

        ttk.Button(win, text="🔄 刷新", command=lambda: (win.destroy(), self.show_entity_panel())).pack(pady=5)

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
        win.title("⏪ 回滚上一步")
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
            vol_dir = os.path.dirname(os.path.dirname(backup_path))
            current_path = os.path.join(vol_dir, f"第{str(chap_num).zfill(4)}章.txt")

            if messagebox.askyesno("确认回滚", f"确定要将第{chap_num}章恢复到备份版本吗？"):
                backup_content = generator.read_text_safe(backup_path)
                with open(current_path, "w", encoding="utf-8") as f:
                    f.write(backup_content)
                messagebox.showinfo("成功", f"第{chap_num}章已回滚！")
                win.destroy()

        ttk.Button(win, text="⏪ 确认回滚", command=do_rollback).pack(pady=10)


# ============================================================
# 启动
# ============================================================
if __name__ == "__main__":
    root = tk.Tk()
    app = NovelGeneratorGUI(root)
    root.mainloop()
