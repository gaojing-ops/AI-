# -*- coding: utf-8 -*-
"""
千章小说创作系统 GUI 桌面版 v3.0
================================
功能：写新章 / 续写 / 润色 / 逻辑检查 / 伏笔追踪 / 历史回看 / 自动保存 / 记忆压缩
"""
import os
import sys
import json
import shutil
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog, simpledialog
from datetime import datetime
from openai import OpenAI
import re

# 模型预设（仅存储默认配置，Key 从 config.json 加载）
MODEL_PRESETS = {
    "DeepSeek": {
        "config_key_field": "api_key",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat"
    },
    "MiniMax-M2.5": {
        "config_key_field": "minimax_api_key",
        "base_url": "https://api.minimaxi.com/v1",
        "model": "MiniMax-M2.5"
    }
}

# ============================================================
# PyInstaller 打包兼容
# ============================================================
if getattr(sys, 'frozen', False):
    _exe_dir = os.path.dirname(sys.executable)
else:
    _exe_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _exe_dir)

import generator
import rag_engine

# 持久化配置：记住上次打开的项目文件夹
LAST_PROJECT_FILE = os.path.join(_exe_dir, "gui_last_project.json")

def load_last_project():
    if os.path.exists(LAST_PROJECT_FILE):
        try:
            with open(LAST_PROJECT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("last_project_dir", "")
        except:
            pass
    return ""

def save_last_project(path):
    with open(LAST_PROJECT_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_project_dir": path}, f, ensure_ascii=False)

def apply_project_dir(project_dir):
    """\u5c06 generator 的所有目录指向指定的项目文件夹"""
    os.chdir(project_dir)
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
# 主应用
# ============================================================
class NovelGeneratorGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("千章小说创作系统 v3.0 - 桌面版")
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
        
        self.create_widgets()
        
        # 尝试加载上次的项目文件夹
        last_dir = load_last_project()
        if last_dir and os.path.isdir(last_dir):
            self.switch_project(last_dir)
        else:
            # 首次启动：自动在工具所在目录创建默认项目，无需用户手动选择
            default_project = os.path.join(_exe_dir, "我的小说")
            os.makedirs(default_project, exist_ok=True)
            self.switch_project(default_project)
            # 写一个"快速上手"提示文件
            quickstart_path = os.path.join(default_project, "plot", "快速上手指南.txt")
            if not os.path.exists(quickstart_path):
                with open(quickstart_path, "w", encoding="utf-8") as f:
                    f.write(
                        "欢迎使用千章小说创作系统！\n\n"
                        "【第一步】点击左上角「⚙ 设置」，填入你的 DeepSeek API Key。\n"
                        "【第二步】在 characters/ 文件夹里放入角色设定（每个角色一个 .txt 文件）。\n"
                        "【第三步】在 world_building/ 文件夹里放入世界观设定。\n"
                        "【第四步】在 plot/ 文件夹里放入大纲或细纲。\n"
                        "【第五步】在右侧「写作要求」框里输入第一章的情节，点击「📝 写新章」即可！\n\n"
                        "提示：你可以随时点击左侧「切换项目」来打开/创建其他小说项目。\n"
                        "提示：在 plot/ 里放一个「基调铁律.txt」可以控制小说的整体风格（比如禁止暴力等）。\n"
                    )
            self.refresh_knowledge_base()
            messagebox.showinfo(
                "欢迎使用！",
                "系统已自动创建了一个「我的小说」项目文件夹。\n\n"
                "📌 请先点击左上角「⚙ 设置」填入你的 API Key！\n"
                "📌 然后把你的角色设定、世界观、大纲放到对应文件夹里就能开始写了。\n\n"
                "左侧知识库里有一份「快速上手指南」，可以看看。"
            )
        
        self.update_word_count()
        # 自动保存定时器 (5分钟)
        self.root.after(300000, self.auto_save_loop)

    def prompt_select_folder(self):
        messagebox.showinfo("欢迎", "请选择您的小说项目文件夹\n（包含 characters/ world_building/ plot/ 的那个文件夹）")
        self.select_project_folder()

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
        self.refresh_knowledge_base()
        self.refresh_status()
        self.folder_lbl.config(text=f"📁 {os.path.basename(folder)}")
        self.root.title(f"千章小说创作系统 v3.0 - {os.path.basename(folder)}")

    def get_client(self):
        preset = MODEL_PRESETS.get(self.model_var.get(), MODEL_PRESETS["DeepSeek"])
        key_field = preset.get("config_key_field", "api_key")
        api_key = self.config.get(key_field, "")
        if not api_key:
            messagebox.showwarning("提示", f"请先在‘设置’中填写 {key_field} ！")
        return OpenAI(api_key=api_key, base_url=preset["base_url"], timeout=120)
    
    def get_model_name(self):
        preset = MODEL_PRESETS.get(self.model_var.get(), MODEL_PRESETS["DeepSeek"])
        return preset["model"]
    
    def clean_think_tags(self, text):
        """清除MiniMax等模型返回的<think>标签内容"""
        return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    
    def on_model_change(self, event=None):
        model_name = self.model_var.get()
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
        ttk.Button(toolbar, text="✨ 润色正文", command=self.polish_text).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="🔍 逻辑检查", command=self.logic_check).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="📜 进度编年史", command=self.update_chronicle).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="📂 打开历史章节", command=self.open_history_chapter).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="📑 一键导出排版", command=self.export_book).pack(side=tk.LEFT, padx=3)

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
        
        # 鼠标滚轮支持（仅在左侧面板区域内生效）
        def _on_mousewheel(event):
            left_canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        def _bind_mousewheel(event):
            left_canvas.bind_all("<MouseWheel>", _on_mousewheel)
        def _unbind_mousewheel(event):
            left_canvas.unbind_all("<MouseWheel>")
        left_canvas.bind("<Enter>", _bind_mousewheel)
        left_canvas.bind("<Leave>", _unbind_mousewheel)

        # 📂 项目文件夹选择器
        folder_frame = ttk.Frame(self.left_scrollable)
        folder_frame.pack(fill=tk.X, pady=(5, 10), padx=5)
        self.folder_lbl = ttk.Label(folder_frame, text="📁 未选择", font=("微软雅黑", 9))
        self.folder_lbl.pack(side=tk.LEFT)
        ttk.Button(folder_frame, text="切换项目", command=self.select_project_folder).pack(side=tk.RIGHT)

        # 🤖 模型选择器
        model_frame = ttk.Frame(self.left_scrollable)
        model_frame.pack(fill=tk.X, pady=(5, 10), padx=5)
        ttk.Label(model_frame, text="🤖 AI模型:", font=("微软雅黑", 9, "bold")).pack(side=tk.LEFT)
        self.model_var = tk.StringVar(value="DeepSeek")
        model_combo = ttk.Combobox(model_frame, textvariable=self.model_var, values=list(MODEL_PRESETS.keys()), state="readonly", width=15)
        model_combo.pack(side=tk.RIGHT)
        model_combo.bind("<<ComboboxSelected>>", self.on_model_change)

        ttk.Separator(self.left_scrollable, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=5, pady=5)

        # 出场人物
        chars_header = ttk.Frame(self.left_scrollable)
        chars_header.pack(fill=tk.X, pady=(5,3), padx=5)
        ttk.Label(chars_header, text="📌 出场人物:", font=("微软雅黑", 9, "bold")).pack(side=tk.LEFT)
        ttk.Button(chars_header, text="全选", width=4, command=lambda: self.toggle_all(self.chars_vars, True)).pack(side=tk.RIGHT, padx=1)
        ttk.Button(chars_header, text="清空", width=4, command=lambda: self.toggle_all(self.chars_vars, False)).pack(side=tk.RIGHT, padx=1)
        self.chars_frame = ttk.Frame(self.left_scrollable)
        self.chars_frame.pack(fill=tk.X, anchor=tk.W, padx=5)
        self.chars_vars = {}

        # 世界观设定
        world_header = ttk.Frame(self.left_scrollable)
        world_header.pack(fill=tk.X, pady=(12,3), padx=5)
        ttk.Label(world_header, text="🌍 世界观/设定:", font=("微软雅黑", 9, "bold")).pack(side=tk.LEFT)
        ttk.Button(world_header, text="全选", width=4, command=lambda: self.toggle_all(self.world_vars, True)).pack(side=tk.RIGHT, padx=1)
        ttk.Button(world_header, text="清空", width=4, command=lambda: self.toggle_all(self.world_vars, False)).pack(side=tk.RIGHT, padx=1)
        self.world_frame = ttk.Frame(self.left_scrollable)
        self.world_frame.pack(fill=tk.X, anchor=tk.W, padx=5)
        self.world_vars = {}
        
        # 剧情大纲与备忘录
        plot_header = ttk.Frame(self.left_scrollable)
        plot_header.pack(fill=tk.X, pady=(12,3), padx=5)
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
        
        ttk.Label(write_frame, text="AI 生成正文 (可手动微调后保存):", font=("微软雅黑", 9, "bold")).pack(anchor=tk.W, pady=(5,0))
        self.result_text = scrolledtext.ScrolledText(write_frame, wrap=tk.WORD, font=("微软雅黑", 11))
        self.result_text.pack(fill=tk.BOTH, expand=True, pady=3)
        self.result_text.bind("<KeyRelease>", lambda e: self.update_word_count())
        
        # 添加右键菜单 (划线精修)
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
        self.reader_lbl = ttk.Label(reader_bar, text="未加载", font=("微软雅黑", 9))
        self.reader_lbl.pack(side=tk.LEFT, padx=10)
        ttk.Button(reader_bar, text="📋 全选复制", command=self.reader_copy_all).pack(side=tk.RIGHT, padx=3)
        
        self.reader_text = scrolledtext.ScrolledText(reader_frame, wrap=tk.WORD, font=("微软雅黑", 10))
        self.reader_text.pack(fill=tk.BOTH, expand=True, pady=3)
        self.reader_chapter_idx = 0
        self.reader_chapter_files = []
        
        # 为阅读器也添加划线精修 (因为作者可能会在阅读器里修改旧章节)
        self.create_context_menu(self.reader_text)

    # ============================================================
    # 划线精修右键菜单
    # ============================================================
    def create_context_menu(self, text_widget):
        context_menu = tk.Menu(text_widget, tearoff=0, font=("微软雅黑", 10))
        context_menu.add_command(label="✨ AI 划线重写/扩写", command=lambda: self.trigger_partial_rewrite(text_widget))
        context_menu.add_separator()
        context_menu.add_command(label="剪切", command=lambda: text_widget.event_generate("<<Cut>>"))
        context_menu.add_command(label="复制", command=lambda: text_widget.event_generate("<<Copy>>"))
        context_menu.add_command(label="粘贴", command=lambda: text_widget.event_generate("<<Paste>>"))
        
        def show_context_menu(event):
            try:
                # 只有选中了文本才可用 AI 精修
                if text_widget.tag_ranges(tk.SEL):
                    context_menu.entryconfig("✨ AI 划线重写/扩写", state=tk.NORMAL)
                else:
                    context_menu.entryconfig("✨ AI 划线重写/扩写", state=tk.DISABLED)
                context_menu.tk_popup(event.x_root, event.y_root)
            finally:
                context_menu.grab_release()
                
        # 绑定右键点击
        text_widget.bind("<Button-3>", show_context_menu)

    def trigger_partial_rewrite(self, text_widget):
        try:
            sel_first = text_widget.index(tk.SEL_FIRST)
            sel_last = text_widget.index(tk.SEL_LAST)
            selected_text = text_widget.get(sel_first, sel_last)
        except tk.TclError:
            return
            
        if not selected_text.strip():
            return
            
        req = simpledialog.askstring(
            "AI 划线精修", 
            f"选中文本（{len(selected_text)}字）。\n请输入你的修改要求：\n（例如：'扩写这段战斗细节' 或 '把语气改得讽刺一点'）",
            parent=self.root
        )
        
        if not req or not req.strip():
            return
            
        # 弹出进度提示框
        progress_win = tk.Toplevel(self.root)
        progress_win.title("AI 处理中")
        progress_win.geometry("300x120")
        progress_win.grab_set()
        
        ttk.Label(progress_win, text="🚀 正在根据要求精修文本...", font=("微软雅黑", 10)).pack(pady=20)
        progress_bar = ttk.Progressbar(progress_win, mode="indeterminate")
        progress_bar.pack(fill=tk.X, padx=20)
        progress_bar.start(10)
        
        def do_rewrite():
            sys_prompt = "你是一名顶级的网文主编和润色大师。你的任务是根据作者的具体要求，对一段小说文本进行【局部重写/扩写/精修】。\n只返回重写后的最终文本片段，不要解释，不要用Markdown代码块包裹，不要添加语气词。"
            user_prompt = f"【待修改的原文片段】：\n{selected_text}\n\n【作者的修改要求】：\n{req}\n\n请直接输出修改后的文本内容："
            
            try:
                new_text = self.call_llm_non_stream(sys_prompt, user_prompt, temp=0.6)
            except Exception as e:
                new_text = None
                messagebox.showerror("生成失败", f"API请求发生错误：{e}", parent=self.root)
                
            def update_ui():
                progress_win.destroy()
                if new_text and new_text.strip():
                    # 替换选中文本
                    text_widget.delete(sel_first, sel_last)
                    text_widget.insert(sel_first, new_text)
                    self.update_word_count()
            
            self.root.after(0, update_ui)
            
        threading.Thread(target=do_rewrite, daemon=True).start()

    # ============================================================
    # 设置面板
    # ============================================================
    def open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("⚙ 系统设置")
        win.geometry("520x480")
        win.grab_set()
        
        ttk.Label(win, text="DeepSeek API Key:", font=("微软雅黑", 10)).pack(anchor=tk.W, padx=15, pady=(15,3))
        api_entry = ttk.Entry(win, width=60, show="*")
        api_entry.pack(padx=15, fill=tk.X)
        api_entry.insert(0, self.config.get("api_key", ""))
        
        ttk.Label(win, text="MiniMax API Key (可选):", font=("微软雅黑", 10)).pack(anchor=tk.W, padx=15, pady=(10,3))
        minimax_entry = ttk.Entry(win, width=60, show="*")
        minimax_entry.pack(padx=15, fill=tk.X)
        minimax_entry.insert(0, self.config.get("minimax_api_key", ""))
        
        ttk.Label(win, text="API Base URL:", font=("微软雅黑", 10)).pack(anchor=tk.W, padx=15, pady=(10,3))
        url_entry = ttk.Entry(win, width=60)
        url_entry.pack(padx=15, fill=tk.X)
        url_entry.insert(0, self.config.get("base_url", "https://api.deepseek.com"))
        
        ttk.Label(win, text="模型名称:", font=("微软雅黑", 10)).pack(anchor=tk.W, padx=15, pady=(10,3))
        model_entry = ttk.Entry(win, width=60)
        model_entry.pack(padx=15, fill=tk.X)
        model_entry.insert(0, self.config.get("model", "deepseek-chat"))
        
        ttk.Label(win, text="Temperature (创意度 0.0-1.5):", font=("微软雅黑", 10)).pack(anchor=tk.W, padx=15, pady=(10,3))
        temp_entry = ttk.Entry(win, width=20)
        temp_entry.pack(anchor=tk.W, padx=15)
        temp_entry.insert(0, str(self.config.get("temperature", 0.8)))
        
        def save_settings():
            self.config["api_key"] = api_entry.get().strip()
            self.config["minimax_api_key"] = minimax_entry.get().strip()
            self.config["base_url"] = url_entry.get().strip()
            self.config["model"] = model_entry.get().strip()
            try:
                self.config["temperature"] = float(temp_entry.get().strip())
            except:
                self.config["temperature"] = 0.8
            
            config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
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
        count = len(text.replace(" ", "").replace("\n", ""))
        self.word_count_lbl.config(text=f"字数: {count}")

    def refresh_status(self):
        self.current_vol, self.next_chap, self.filepath, self.latest_chap, self.latest_filepath = generator.get_latest_chapter_info()
        self.info_lbl.config(text=self.get_status_text())

    def toggle_all(self, var_dict, state):
        for var in var_dict.values():
            var.set(state)

    def refresh_knowledge_base(self):
        for widget in self.chars_frame.winfo_children(): widget.destroy()
        for widget in self.world_frame.winfo_children(): widget.destroy()
        for widget in self.plot_frame.winfo_children(): widget.destroy()
        
        # 人物：全部默认勾选
        self.chars_map = generator.list_files_in_dir(generator.DIRS["chars"])
        self.chars_vars.clear()
        for name in self.chars_map:
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(self.chars_frame, text=name, variable=var).pack(anchor=tk.W)
            self.chars_vars[name] = var
            
        # 世界观：全部默认勾选
        self.world_map = generator.list_files_in_dir(generator.DIRS["world"])
        self.world_vars.clear()
        for name in self.world_map:
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(self.world_frame, text=name, variable=var).pack(anchor=tk.W)
            self.world_vars[name] = var

        # 大纲：全部默认勾选
        self.plot_map = generator.list_files_in_dir(generator.DIRS["plot"])
        self.plot_vars.clear()
        for name in self.plot_map:
            var = tk.BooleanVar(value=True)
            ttk.Checkbutton(self.plot_frame, text=name, variable=var).pack(anchor=tk.W)
            self.plot_vars[name] = var
        
        # 自动刷新阅读器
        self.refresh_reader_files()

    # ============================================================
    # 章节阅读器
    # ============================================================
    def refresh_reader_files(self):
        self.reader_chapter_files = []
        out_dir = generator.DIRS.get("out", "output")
        if not os.path.exists(out_dir):
            return
        for root, dirs, files in os.walk(out_dir):
            for f in sorted(files):
                if f.endswith(".txt") and f.startswith("第") and "章" in f:
                    self.reader_chapter_files.append(os.path.join(root, f))
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
        self.reader_lbl.config(text=f"{filename}  ({idx+1}/{len(self.reader_chapter_files)})")
        content = generator.read_text_safe(filepath)
        self.reader_text.delete(1.0, tk.END)
        self.reader_text.insert(tk.END, content)
        self.reader_text.see(1.0)
    
    def reader_prev(self):
        if self.reader_chapter_files and self.reader_chapter_idx > 0:
            self.reader_chapter_idx -= 1
            self.reader_load()
    
    def reader_next(self):
        old_count = len(self.reader_chapter_files)
        self.refresh_reader_files_silent()
        if self.reader_chapter_files and self.reader_chapter_idx < len(self.reader_chapter_files) - 1:
            self.reader_chapter_idx += 1
            self.reader_load()
    
    def refresh_reader_files_silent(self):
        """刷新文件列表但不改变当前位置"""
        old_idx = self.reader_chapter_idx
        self.reader_chapter_files = []
        out_dir = generator.DIRS.get("out", "output")
        if not os.path.exists(out_dir):
            return
        for root, dirs, files in os.walk(out_dir):
            for f in sorted(files):
                if f.endswith(".txt") and f.startswith("第") and "章" in f:
                    self.reader_chapter_files.append(os.path.join(root, f))
        self.reader_chapter_idx = min(old_idx, max(0, len(self.reader_chapter_files) - 1))
    
    def reader_copy_all(self):
        content = self.reader_text.get(1.0, tk.END).strip()
        if content:
            lines = content.split("\n")
            # 跳过第一行标题（第X章 xxx）和紧跟的空行
            if lines and lines[0].startswith("第") and "章" in lines[0]:
                lines = lines[1:]
                while lines and not lines[0].strip():
                    lines = lines[1:]
            body = "\n".join(lines)
            self.root.clipboard_clear()
            self.root.clipboard_append(body)
            self.reader_lbl.config(text=self.reader_lbl.cget("text") + " ✅已复制(纯正文)")

    # ============================================================
    # 构建 System Prompt
    # ============================================================
    def build_system_prompt_gui(self, current_prompt=""):
        prompt_parts = []
        prompt_parts.append(
            "你是一名顶尖的网络小说首发网站白金作家，正在连载一部千章量级的长篇巨著。\n"
            "请严格遵守提供的数据库设定，你的目标是输出极具吸引力、行文流畅的【小说正文】。\n"
            "【绝对规则】：\n"
            "1. 只输出小说正文和章节标题！严禁用任何形式与读者互动、不准加注释、不准写摘要。\n"
            "2. 严禁使用任何Markdown格式！不准用#号标题、不准用**加粗、不准用*斜体，输出纯文本。\n"
            "3. 不要使用过于翻译腔或播音腔的词汇，需符合网文阅读爽感与节奏。\n"
            "4. 如设定出现冲突，以当前的 <本章写作要求> 为最高优先级。\n"
            "5. 以下是你的全部记忆库，请严格遵循相应的设定标签。\n"
        )
        
        # 加载外部基调铁律文件（如果存在）
        tone_rules_path = os.path.join(generator.DIRS["plot"], "基调铁律.txt")
        tone_rules = generator.read_text_safe(tone_rules_path)
        if tone_rules:
            prompt_parts.append(f"<tone_rules>\n{tone_rules}\n</tone_rules>\n")

        # 固定加载选中的大纲和备忘录
        selected_plot = [f"【{name}】\n{generator.read_text_safe(self.plot_map[name])}" 
                         for name, var in self.plot_vars.items() if var.get()]
        if selected_plot:
            prompt_parts.append("<plot_and_memo>\n" + "\n\n".join(selected_plot) + "\n</plot_and_memo>\n")
            
        # 使用 RAG 引擎智能筛选人物和世界观设定
        rag = rag_engine.SimpleLocalRAG()
        doc_idx = 0
        
        # 将勾选的内容加入 RAG 引擎
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

        # 构建搜索语料：本章要求 + 上一章结尾 (作为额外的推断依据)
        search_query = current_prompt
        if self.latest_filepath and os.path.exists(self.latest_filepath):
            try:
                prev = generator.read_text_safe(self.latest_filepath)
                # 附加上一章最后500字来增强连贯的关联人物检索
                search_query += "\n" + prev[-500:]
            except:
                pass
                
        # 提取 Top 5 个最相关的设定 (防止Token消耗过高)
        results = rag.search(search_query, top_k=5, threshold=0.01)
        
        selected_chars = []
        selected_world = []
        
        for res in results:
            doc_id, score, title, content = res
            if doc_id.startswith("char_"):
                selected_chars.append(f"【{title}】\n{content}")
            else:
                selected_world.append(f"【{title}】\n{content}")
                
        # 如果检索没匹配到（或者引擎为空/query太短），回退为加载所有勾选项来兜底
        if not results:
            selected_chars = [f"【{name}】\n{generator.read_text_safe(self.chars_map[name])}" 
                              for name, var in self.chars_vars.items() if var.get()]
            selected_world = [f"【{name}】\n{generator.read_text_safe(self.world_map[name])}" 
                              for name, var in self.world_vars.items() if var.get()]

        if selected_chars:
            prompt_parts.append("<character_profiles>\n" + "\n\n".join(selected_chars) + "\n</character_profiles>\n")

        if selected_world:
            prompt_parts.append("<world_building_rules>\n" + "\n\n".join(selected_world) + "\n</world_building_rules>\n")

        prompt_parts.append(
            "\n\n<chapter_quality_rules>\n"
            "【单章质量控制与多线防丢指令】(极度重要)：\n"
            "1. 格式要求：正文第一行必须是章节标题，格式为 第X章 标题（4-8字直接概括核心事件，绝不能与上一章标题相似）。\n"
            "2. 严禁注水与循环：如果不满字数，必须主动推进大纲的下一个节点！绝对禁止用无意义的喝茶、寒暄、重复心理活动凑字数。对话必须推动剧情或展现性格。\n"
            "3. 杜绝流水账套路：禁止使用千篇一律的开头（例如每一章都用“另一边”、“此时”起手）。\n"
            "4. 人设绝对锁定(反OOC)：角色的行为逻辑必须严格符合<character_profiles>的设定，高冷绝不舔狗，聪明绝不降智。\n"
            "5. 视角统一：同一个场景内请保持主视角（通常为主角）统一，不要在一个段落内强行切换到其他人的内心活动，避免硬切视角。\n"
            "6. 严禁说教与强行升华：保持网文的娱乐性和爽感，禁止在打脸、冲突后强行加入大段关于人性、社会的道德说教和哲学讨论。\n"
            "7. 文笔要求：对话口语化有个性；避免连续三句以上相同句式；段落长短错落，关键转折用单句成段；严禁翻译腔/播音腔。\n"
            "8. 情节与悬念：本章需有2-3次情绪波动，结尾强制设置1个具体的悬念钩子(Hook)，严禁使用“不知发生了什么不可思议的事”这种敷衍描写。\n"
            "9. 字数要求：严格控制在2500-3000字之间，上下浮动不超出2000-3500范围，保持每章均匀。\n"
            "10. 作者有话说：约每3章随机在正文末尾追加一段'作者有话说'，用'---'隔开（笔名'墨千灯'，风格搞笑幽默，50-100字）。\n"
            "11. 【多线剧情防遗漏（最高优先级）】：如果备忘录或大纲中存在正在分兵/分头行动/处于不同空间的关键队友或重要角色，即使本章他们不在主角身边，也必须在章节中通过转场、远方传音、战场感知等方式简要交代他们的当前处境（苦战中/破阵中/遇到新危机等）。绝对不允许让未死亡的重要角色凭空消失！\n"
            "12. 逻辑连贯：不要只顾着写主角的单线进展，要时刻思考其他角色在同一时间轴下在做什么，确保世界是\"活\"的。\n"
            "</chapter_quality_rules>\n"
        )
        return "\n".join(prompt_parts)

    # ============================================================
    # LLM 调用 (流式)
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
        self.result_text.delete(1.0, tk.END)
        self.result_text.insert(tk.END, "🚀 正在燃烧算力生成中，请稍候...\n\n")
        self.root.update_idletasks()
        
        client = self.get_client()
        
        try:
            stream = client.chat.completions.create(
                model=self.get_model_name(),
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": final_user_prompt}
                ],
                temperature=self.config.get("temperature", 0.8),
                stream=True
            )
            
            self.result_text.delete(1.0, tk.END)
            self.generated_content = ""
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    text_chunk = chunk.choices[0].delta.content
                    self.generated_content += text_chunk
                    self.result_text.insert(tk.END, text_chunk)
                    self.result_text.see(tk.END)
                    self.root.update_idletasks()
            # 清除MiniMax等模型的<think>标签
            cleaned = self.clean_think_tags(self.generated_content)
            if cleaned != self.generated_content:
                self.generated_content = cleaned
                self.result_text.delete(1.0, tk.END)
                self.result_text.insert(tk.END, cleaned)
        except Exception as e:
            messagebox.showerror("生成失败", f"API调用错误: {str(e)}")
        finally:
            self.is_generating = False
            self.enable_buttons(is_new)
            self.update_word_count()

    def start_generation_thread(self, system_prompt, final_user_prompt, is_new):
        self.disable_buttons()
        thread = threading.Thread(target=self.stream_call_llm, args=(system_prompt, final_user_prompt, is_new))
        thread.daemon = True
        thread.start()

    def call_llm_non_stream(self, system_prompt, user_prompt, temp=0.3):
        client = self.get_client()
        response = client.chat.completions.create(
            model=self.get_model_name(),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=temp
        )
        return self.clean_think_tags(response.choices[0].message.content or "")

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
    # 核心功能：批量挂机写
    # ============================================================
    def batch_generate(self):
        count = simpledialog.askinteger("批量生成", "请输入要自动生成的章节数量：", minvalue=1, maxvalue=1100)
        if not count:
            return
        
        self.batch_stop = False
        self.disable_buttons()
        self.btn_stop.config(state=tk.NORMAL)
        
        thread = threading.Thread(target=self.batch_worker, args=(count,), daemon=True)
        thread.start()
    
    # 内容漂移防火墙：禁词列表
    BANNED_KEYWORDS = [
        "灭口", "追杀", "绑架", "变声器", "纵火", "枪战", "地下室暗杀",
        "祭奠纸花", "尸体", "凶手", "鲜血淋漓", "杀人灭口", "追车枪战",
        "通风管道潜入", "失踪案", "刑侦", "法医", "验尸", "弹孔",
        "老地方见", "秘密接头", "线人", "地下停车场见面",
    ]

    def _auto_switch_volume_outline(self, chap_num):
        """根据章节号自动切换当前卷大纲"""
        volume_ranges = {
            (1, 250): "第一卷",
            (251, 500): "第二卷",
            (501, 750): "第三卷",
            (751, 950): "第四卷",
            (951, 1100): "第五卷·终章",
        }
        current_vol_name = None
        for (start, end), name in volume_ranges.items():
            if start <= chap_num <= end:
                current_vol_name = name
                break
        if not current_vol_name:
            return
        
        # 读取当前卷大纲，检查是否已是正确卷
        outline_path = os.path.join(generator.DIRS["plot"], "当前卷大纲.txt")
        current_outline = generator.read_text_safe(outline_path)
        if current_vol_name in current_outline:
            return  # 已经是正确的卷
        
        # 从长线大纲中提取对应卷的内容
        long_outline_path = os.path.join(generator.DIRS["plot"], "1300章长线大纲规划.txt")
        if not os.path.exists(long_outline_path):
            return
        long_outline = generator.read_text_safe(long_outline_path)
        
        # 查找对应卷的段落
        lines = long_outline.split("\n")
        vol_content = []
        capturing = False
        for line in lines:
            if current_vol_name in line or (f"### {current_vol_name}" in line):
                capturing = True
            elif capturing and line.strip().startswith("### ") and current_vol_name not in line:
                break  # 下一卷开始了
            if capturing:
                vol_content.append(line)
        
        if vol_content:
            new_outline = "\n".join(vol_content)
            with open(outline_path, "w", encoding="utf-8") as f:
                f.write(new_outline)
            return f"📋 已自动切换至{current_vol_name}大纲"
        return None

    def _check_content_drift(self, content):
        """检测生成内容是否包含禁词，返回命中的禁词列表"""
        hits = [kw for kw in self.BANNED_KEYWORDS if kw in content]
        return hits

    def batch_worker(self, total_count):
        client = self.get_client()
        import time
        
        for i in range(total_count):
            if self.batch_stop:
                self.result_text.insert(tk.END, f"\n\n⏹ 已手动停止，共完成 {i} 章。\n")
                break
            
            # 刷新章节信息
            self.current_vol, self.next_chap, self.filepath, self.latest_chap, self.latest_filepath = generator.get_latest_chapter_info()
            chap_num = self.next_chap
            
            # 自动换卷大纲
            vol_switch_msg = self._auto_switch_volume_outline(chap_num)
            
            self.result_text.delete(1.0, tk.END)
            if vol_switch_msg:
                self.result_text.insert(tk.END, f"{vol_switch_msg}\n")
            self.result_text.insert(tk.END, f"🚀 批量模式 [{i+1}/{total_count}] — 正在生成第 {chap_num} 章...\n\n")
            self.root.update_idletasks()
            
            # —— 首先读取上一章内容作为衔接参考（剥离"作者有话说"） ——
            prev_content = ""
            if self.latest_filepath and os.path.exists(self.latest_filepath):
                raw = generator.read_text_safe(self.latest_filepath)
                # 剥离末尾的"作者有话说"段落，避免AI把吐槽当正文接着写
                if "---" in raw:
                    raw = raw[:raw.rfind("---")].rstrip()
                # 只取最后800字作为过渡参考，节省 Token
                prev_content = raw[-800:] if len(raw) > 800 else raw

            # ---- 第一步：让 AI 根据大纲决定本章写什么 ----
            # 使用 RAG 的动态 prompt
            sys_prompt = self.build_system_prompt_gui(current_prompt=f"写第{chap_num}章内容。前情提要：{prev_content[-200:] if prev_content else ''}")
                
                
            # 防标题重复：提取全部已有标题（用于生成后硬性去重）
            all_titles = []
            files = []
            for vol_dir in sorted(os.listdir(generator.DIRS["out"])):
                vol_path = os.path.join(generator.DIRS["out"], vol_dir)
                if os.path.isdir(vol_path):
                    for f in sorted(os.listdir(vol_path)):
                        if f.endswith(".txt"):
                            files.append(os.path.join(vol_path, f))
            
            for f in files:
                try:
                    with open(f, "r", encoding="utf-8", errors="ignore") as file:
                        first_line = file.readline().strip().lstrip("#").strip()
                        if first_line:
                            # 提取标题关键词（去掉"第XX章"前缀）
                            import re
                            title_match = re.sub(r'^第\d+章\s*', '', first_line)
                            if title_match:
                                all_titles.append(title_match)
                except Exception:
                    pass
            
            # 给AI提示最近20个标题
            recent_titles = all_titles[-20:] if len(all_titles) > 20 else all_titles
            recent_titles_str = "\n".join(recent_titles)
            title_hint = f"\n【近期已用章节名（本章标题绝对禁止重复或高度相似）】：\n{recent_titles_str}\n" if recent_titles else ""
            # 保存全部标题集合用于生成后硬性去重
            all_titles_set = set(all_titles)
            
            # 自动完结检测
            completion_hint = ""
            TOTAL_TARGET = 1100
            volume_ends = {250: "第一卷", 500: "第二卷", 750: "第三卷", 950: "第四卷", 1100: "终章"}
            
            for end_chap, vol_name in volume_ends.items():
                if end_chap - 10 <= chap_num <= end_chap:
                    remaining = end_chap - chap_num
                    if end_chap == TOTAL_TARGET:
                        completion_hint = f"\n\n【自动完结指令】本书还剩 {remaining} 章即完结（全书共{TOTAL_TARGET}章）。请开始强制收束所有主线伏笔，给出最终结局。本章须推进结局进程。"
                    else:
                        completion_hint = f"\n\n【卷末收束指令】{vol_name}还剩 {remaining} 章结束。请开始收束本卷主线冲突，为卷末高潮做铺垫，但不要完结全书。"
                    break
            
            if chap_num >= TOTAL_TARGET:
                completion_hint = f"\n\n【大结局指令】这是全书的最终章！请收束所有伏笔线索，写出圆满的大结局。结尾需要有余韵。"

            plan_prompt = f"""你现在要写第 {chap_num} 章。

请根据 <plot_and_memo> 标签中的大纲和备忘录，结合上一章结尾的剧情走向，直接输出本章的小说正文。

格式要求（极重要）：
- 第一行必须是章节标题，格式为"第{chap_num}章 标题"（标题简洁明了4-8字，概括本章核心事件）
- 标题后空一行再写正文
{title_hint}
内容要求：
1. 紧接上一章的剧情自然展开，不要重复已经发生的事。
2. 【反重复铁律】：仔细阅读上一章结尾片段和备忘录中的「近期事件纪要」，凡是已经发生过的事件（如某人已汇报、某事已解决），本章绝对禁止再写一遍！必须推进新的剧情节点。
3. 本章需推进至少1个主线事件，制造1个悬念钩子。
4. 如果大纲中有本阶段应该发生的事件，请择机安排。
5. 【多线强制检查】：仔细阅读备忘录中的"队友状态"或"关键人物状态"，如果有分兵/分头行动的角色，本章中必须用转场、远方感知、传讯等方式简要提及他们的处境，绝不能让活着的重要角色凭空消失！
{completion_hint}

【上一章结尾片段（请衔接）】：
{prev_content if prev_content else "（这是第一章，无上文）"}"""

            max_retries = 3
            retry_count = 0
            chapter_content = ""
            success = False
            
            while retry_count < max_retries and not success:
                try:
                    stream = client.chat.completions.create(
                        model=self.get_model_name(),
                        messages=[
                            {"role": "system", "content": sys_prompt},
                            {"role": "user", "content": plan_prompt}
                        ],
                        temperature=self.config.get("temperature", 0.8),
                        stream=True
                    )
                    
                    if retry_count > 0:
                        self.result_text.insert(tk.END, f"\n[第 {retry_count} 次重试中...]\n")
                    self.result_text.insert(tk.END, f"📝 第 {chap_num} 章 [{i+1}/{total_count}]\n\n")
                    self.root.update_idletasks()
                    
                    chapter_content = ""
                    for chunk in stream:
                        if chunk.choices and chunk.choices[0].delta.content:
                            text_chunk = chunk.choices[0].delta.content
                            chapter_content += text_chunk
                            self.result_text.insert(tk.END, text_chunk)
                            self.result_text.see(tk.END)
                            self.root.update_idletasks()
                    
                    if self.batch_stop: break
                    chapter_content = self.clean_think_tags(chapter_content)
                    
                    # 强硬校验：防偷懒导致的字数过少残卷
                    if len(chapter_content.strip()) < 1500 and not self.batch_stop:
                        self.result_text.insert(tk.END, f"\n\n⚠ 警告：字数过少（仅 {len(chapter_content.strip())} 字），触发自动扩写...\n")
                        self.root.update_idletasks()
                        expand_prompt = plan_prompt + f"\n\n你刚生成的章节内容如下：\n{chapter_content}\n\n【强烈指令】：以上内容字数严重不足。请以这段内容为基础，继续向后发展剧情并展开深入描写，输出完整合规的本章正文。不要带'接着前文'等废话，直接自然顺接续写。"
                        
                        expand_stream = client.chat.completions.create(
                            model=self.get_model_name(),
                            messages=[
                                {"role": "system", "content": sys_prompt},
                                {"role": "user", "content": expand_prompt}
                            ],
                            temperature=self.config.get("temperature", 0.8),
                            stream=True
                        )
                        
                        expanded_content = ""
                        for chunk in expand_stream:
                            if chunk.choices and chunk.choices[0].delta.content:
                                text_chunk = chunk.choices[0].delta.content
                                expanded_content += text_chunk
                                self.result_text.insert(tk.END, text_chunk)
                                self.result_text.see(tk.END)
                                self.root.update_idletasks()
                        
                        chapter_content += "\n\n" + self.clean_think_tags(expanded_content)
                        
                    if len(chapter_content.strip()) >= 500:
                        # 内容漂移防火墙检测
                        drift_hits = self._check_content_drift(chapter_content)
                        if drift_hits and retry_count < max_retries - 1:
                            self.result_text.insert(tk.END, f"\n\n🛡️ 漂移防火墙：检测到禁词 {drift_hits}，丢弃本章并重新生成...\n")
                            self.root.update_idletasks()
                            retry_count += 1
                            time.sleep(3)
                            continue
                        
                        # 标题硬性去重检测
                        first_line = chapter_content.strip().split("\n")[0].lstrip("#").strip()
                        new_title = re.sub(r'^第\d+章\s*', '', first_line)
                        if new_title and new_title in all_titles_set and retry_count < max_retries - 1:
                            self.result_text.insert(tk.END, f"\n\n🔄 标题重复：'{new_title}' 已在之前使用过，丢弃并重新生成...\n")
                            self.root.update_idletasks()
                            # 在plan_prompt中追加强制换标题指令
                            plan_prompt += f"\n\n【紧急】：标题'{new_title}'已被使用过，严禁再用！请换一个完全不同的标题。"
                            retry_count += 1
                            time.sleep(3)
                            continue
                        success = True
                    else:
                        retry_count += 1
                        time.sleep(3)
                        
                except Exception as e:
                    self.result_text.insert(tk.END, f"\n\n❌ 生成中断: {str(e)}\n等待5秒后重试...\n")
                    self.root.update_idletasks()
                    retry_count += 1
                    time.sleep(5)
            
            if self.batch_stop: break
            
            if not success:
                self.result_text.insert(tk.END, f"\n\n❌ 连续失败 {max_retries} 次，结束批量生成以防损坏。")
                break
                
            # ---- 第二步：自动保存 ----
            with open(self.filepath, "w", encoding="utf-8") as f:
                f.write(chapter_content)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            history_path = os.path.join(generator.DIRS["hist"], f"Ch{chap_num}_{timestamp}.txt")
            with open(history_path, "w", encoding="utf-8") as f:
                f.write(f"[批量生成] 第{chap_num}章\n\n" + "-"*40 + "\n\n" + chapter_content)
            
            self.result_text.insert(tk.END, f"\n\n✅ 第 {chap_num} 章已保存！")
            self.refresh_reader_files_silent()
            self.root.update_idletasks()
            
            # 自动完结检测：如果AI在正文中写了完结标志，自动停止
            ending_markers = ["（全书完）", "（完）", "全书完", "—— 全文完 ——", "（大结局）", "the end", "【完结】", "（全文完）"]
            last_200 = chapter_content[-200:].lower()
            if any(m in last_200 for m in [x.lower() for x in ending_markers]):
                self.result_text.insert(tk.END, f"\n\n🎉 AI已写出完结标志，全书完结！共 {chap_num} 章。")
                break
            
            # ---- 第三步：自动压缩记忆 ----
            self.result_text.insert(tk.END, f"\n🧠 正在滚动压缩记忆...")
            self.root.update_idletasks()
            
            try:
                old_memo = generator.read_text_safe(os.path.join(generator.DIRS["plot"], "全局备忘录.txt"))
                
                memo_prompt = f"""用不超过600字总结【旧记忆】+【最新章节】。
必须严格保留且更新以下结构，不可擅自删除未死亡的关键队友：
【核心坐标状态】时间/地点/主角状态/关键队友多线状态
【近期事件纪要·已完成】过去几章已确认发生并完结的事件（标记为✓，下一章不可重复写这些事件）
【近期事件纪要·进行中】正在发生但尚未结束的事件
【主线线索与遗留伏笔】之前未解决的悬念和伏笔
【下一步主线导向】首要短期目标与未解悬念

【旧记忆】：{old_memo if old_memo else "暂无。"}
【最新章节】：{chapter_content[:2000]}"""

                memo_sys = "你是长篇小说的超级记忆压缩机。\n【硬性规则-违反则压缩失败】：\n1. 必须保留备忘录中所有未死亡的关键角色的状态（包括不在当前镜头的分兵队友），即使他们本章没出场也绝对不能删除！\n2. 多线并行时，每个分兵角色的最新已知状态和所在位置都必须保留。\n3. 未解悬念和伏笔线索不可删除，只可精简措辞。\n4. 只压缩措辞冗余，不可压缩关键信息。\n5. 【基调保护】如果备忘录中包含'核心基调提醒'字段，必须原样保留。压缩时严禁引入犯罪/悬疑/惊悚类新关键词（如灭口、追杀、失踪案、变声器、纵火等），如果原文中就没有这些词汇，压缩结果中也不允许出现。"
                memo_content = self.call_llm_non_stream(memo_sys, memo_prompt, temp=0.3)
                memo_path = os.path.join(generator.DIRS["plot"], "全局备忘录.txt")
                
                # 备忘录损坏保护：内容过短则拒绝覆盖
                if len(memo_content.strip()) < 50:
                    self.result_text.insert(tk.END, " ⚠️ 压缩结果异常（过短），保留原备忘录！\n")
                else:
                    # 覆盖前自动备份
                    if os.path.exists(memo_path):
                        backup_dir = os.path.join(generator.DIRS["plot"], "备忘录备份")
                        os.makedirs(backup_dir, exist_ok=True)
                        backup_name = f"备忘录_Ch{chap_num}_{datetime.now().strftime('%H%M%S')}.txt"
                        shutil.copy2(memo_path, os.path.join(backup_dir, backup_name))
                    with open(memo_path, "w", encoding="utf-8") as f:
                        f.write(memo_content)
                    self.result_text.insert(tk.END, " ✅ 记忆已更新！\n")
            except Exception as e:
                self.result_text.insert(tk.END, f" ❌ 记忆更新报错(跳过): {str(e)}\n")
            self.root.update_idletasks()
            
            # 章节间冷却（防止API限流）
            if not self.batch_stop:
                time.sleep(3)
        
        # 完成
        self.refresh_status()
        self.result_text.insert(tk.END, f"\n\n{'='*40}\n🎉 批量生成完毕！\n")
        self.btn_new.config(state=tk.NORMAL)
        self.btn_continue.config(state=tk.NORMAL)
        self.btn_batch.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self.is_generating = False
        self.update_word_count()

    def stop_batch(self):
        self.batch_stop = True
        self.btn_stop.config(state=tk.DISABLED)
        self.result_text.insert(tk.END, "\n\n⏸ 正在停止，当前章节写完后会安全停下...\n")

    # ============================================================
    # 保存功能
    # ============================================================
    def save_new_chapter(self):
        content = self.result_text.get(1.0, tk.END).strip()
        if not content: return
        with open(self.filepath, "w", encoding="utf-8") as f:
            f.write(content)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        history_path = os.path.join(generator.DIRS["hist"], f"Ch{self.next_chap}_{timestamp}.txt")
        with open(history_path, "w", encoding="utf-8") as f:
            f.write(self.current_req + "\n\n" + "-"*40 + "\n\n" + content)
        messagebox.showinfo("成功", f"第 {self.next_chap} 章保存成功！\n请记得提炼全局记忆备忘录！")
        self.btn_save_new.config(state=tk.DISABLED)
        self.refresh_status()

    def save_append_chapter(self):
        content = self.result_text.get(1.0, tk.END).strip()
        if not content: return
        with open(self.latest_filepath, "a", encoding="utf-8") as f:
            f.write("\n\n" + content)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        history_path = os.path.join(generator.DIRS["hist"], f"Ch{self.latest_chap}_续写_{timestamp}.txt")
        with open(history_path, "w", encoding="utf-8") as f:
            f.write(self.current_req + "\n\n" + "-"*40 + "\n\n" + content)
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
                    original_text = self.info_lbl.cget("text")
                    self.info_lbl.config(text=f"[{datetime.now().strftime('%H:%M')}] ✅ 已自动保存草稿")
                    self.root.after(3000, lambda: self.info_lbl.config(text=original_text))
                except:
                    pass
        self.root.after(300000, self.auto_save_loop)

    # ============================================================
    # 工具箱：记忆压缩
    # ============================================================
    def compress_memory(self):
        if not self.latest_filepath or not os.path.exists(self.latest_filepath):
            messagebox.showwarning("提示", "尚无已写章节！")
            return
        text = generator.read_text_safe(self.latest_filepath)
        old_memo = generator.read_text_safe(os.path.join(generator.DIRS["plot"], "全局备忘录.txt"))
        
        sys_prompt = "你是长篇小说的\"超级记忆压缩机与切片系统\"。\n【硬性规则-违反则压缩失败】：\n1. 必须保留备忘录中所有未死亡的关键角色的状态（包括不在当前镜头的分兵队友），即使他们本章没出场也绝对不能删除！\n2. 多线并行时，每个分兵角色的最新已知状态和所在位置都必须保留。\n3. 未解悬念和伏笔线索不可删除，只可精简措辞。\n4. 只压缩措辞冗余，不可压缩关键信息。\n5. 【基调保护】如果备忘录中包含'核心基调提醒'字段，必须原样保留。压缩时严禁引入犯罪/悬疑/惊悚类新关键词（如灭口、追杀、失踪案、变声器、纵火等），如果原文中就没有这些词汇，压缩结果中也不允许出现。"
        user_prompt = f"""请执行绝对结构化的"短期记忆切片"。用不超过600字总结【旧记忆】+【最新章节】。
必须严格保留且更新以下结构，不可擅自删除未死亡的关键队友：

【核心坐标状态】
- 时间 / 地点 / 主角即时状态 / 关键队友多线状态

【近期事件纪要·已完成】
- 过去几章已确认发生并完结的事件（标记为✓，下一章不可重复写这些事件）

【近期事件纪要·进行中】
- 正在发生但尚未结束的事件

【主线线索与遗留伏笔】
- 之前未解决的悬念和伏笔

【下一步主线导向】
- 首要短期目标与三个未解悬念

【旧记忆参考】：
{old_memo if old_memo else "暂无。"}

【最新章节内容】：
{text}"""

        def do_compress():
            self.disable_buttons()
            self.result_text.delete(1.0, tk.END)
            self.result_text.insert(tk.END, "🧠 正在提炼记忆...\n")
            try:
                memo_content = self.call_llm_non_stream(sys_prompt, user_prompt, temp=0.3)
                path = os.path.join(generator.DIRS["plot"], "全局备忘录.txt")
                
                # 备忘录损坏保护：内容过短则拒绝覆盖
                if len(memo_content.strip()) < 50:
                    self.result_text.delete(1.0, tk.END)
                    self.result_text.insert(tk.END, "⚠️ 压缩结果异常（过短），已保留原备忘录！")
                    messagebox.showwarning("警告", "压缩结果异常（内容过短），已保留原备忘录不变。")
                    return
                
                # 覆盖前自动备份
                if os.path.exists(path):
                    backup_dir = os.path.join(generator.DIRS["plot"], "备忘录备份")
                    os.makedirs(backup_dir, exist_ok=True)
                    backup_name = f"备忘录_手动_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                    import shutil
                    shutil.copy2(path, os.path.join(backup_dir, backup_name))
                with open(path, "w", encoding="utf-8") as f:
                    f.write(memo_content)
                self.result_text.delete(1.0, tk.END)
                self.result_text.insert(tk.END, f"✅ 记忆备忘录已更新！\n\n{memo_content}")
                messagebox.showinfo("成功", "记忆备忘录已更新！")
            except Exception as e:
                messagebox.showerror("错误", f"提炼记忆失败: {str(e)}")
            finally:
                self.btn_new.config(state=tk.NORMAL)
                self.btn_continue.config(state=tk.NORMAL)
        
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

【旧伏笔追踪表】：
{old_table if old_table else "暂无。"}

【最新章节内容】：
{text}"""

        def do_track():
            self.disable_buttons()
            self.result_text.delete(1.0, tk.END)
            self.result_text.insert(tk.END, "🔮 正在追踪伏笔...\n")
            try:
                table = self.call_llm_non_stream(sys_prompt, user_prompt, temp=0.2)
                path = os.path.join(generator.DIRS["plot"], "伏笔与因果追踪表.txt")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(table)
                self.result_text.delete(1.0, tk.END)
                self.result_text.insert(tk.END, f"✅ 伏笔追踪表已更新！\n\n{table}")
                messagebox.showinfo("成功", "伏笔追踪表已更新！")
            except Exception as e:
                messagebox.showerror("错误", f"伏笔追踪失败: {str(e)}")
            finally:
                self.btn_new.config(state=tk.NORMAL)
                self.btn_continue.config(state=tk.NORMAL)
        
        threading.Thread(target=do_track, daemon=True).start()

    # ============================================================
    # 工具箱：进度编年史 (Chronicle Tracker)
    # ============================================================
    def update_chronicle(self):
        if not self.latest_filepath or not os.path.exists(self.latest_filepath):
            messagebox.showwarning("提示", "尚无已写章节！")
            return
            
        text = generator.read_text_safe(self.latest_filepath)
        chronicle_path = os.path.join(generator.DIRS["plot"], "世界编年史.txt")
        old_chronicle = generator.read_text_safe(chronicle_path)
        
        sys_prompt = "你是这本小说的'时间轴与编年史管理员'。"
        user_prompt = f"""请分析【最新章节内容】，提取其中流逝的时间（如“过了三天”、“半个月后”、“次日清晨”）以及发生的重大里程碑事件，追加更新到【小说世界编年史】中。

【要求】：
1. 必须推算出当前大概的“相对宇宙时间”。如果最新章没有明确的时间流逝，也请标记出这一章发生的时间节点（如：接上章当日、当晚）。
2. 在编年史末尾追加最新的一条记录（或者更新最后一条记录），不需要输出整个编年史，只输出新增的编年史条目。
3. 格式：[相对时间戳] 第X章发生的里程碑简述。

【已有编年史参考】：
{old_chronicle if old_chronicle else "（暂无，请从本章开始建立元年）"}

【第 {self.latest_chap} 章最新内容】：
{text}
"""

        def do_update():
            self.disable_buttons()
            self.result_text.delete(1.0, tk.END)
            self.result_text.insert(tk.END, "📜 正在推演时间轴与提取编年史...\n")
            try:
                new_entry = self.call_llm_non_stream(sys_prompt, user_prompt, temp=0.1)
                with open(chronicle_path, "a", encoding="utf-8") as f:
                    if not old_chronicle:
                        f.write("=== 小世界核心编年史 ===\n\n")
                    f.write(f"{new_entry}\n")
                    
                self.result_text.delete(1.0, tk.END)
                self.result_text.insert(tk.END, f"✅ 编年史已更新并追加：\n\n{new_entry}\n\n======================\n【完整编年史】\n\n{generator.read_text_safe(chronicle_path)}")
                messagebox.showinfo("成功", "世界编年史已更新！")
            except Exception as e:
                messagebox.showerror("错误", f"编年史更新失败: {str(e)}")
            finally:
                self.btn_new.config(state=tk.NORMAL)
                self.btn_continue.config(state=tk.NORMAL)
                
        threading.Thread(target=do_update, daemon=True).start()

    # ============================================================
    # 工具箱：润色正文
    # ============================================================
    def polish_text(self):
        text = self.result_text.get(1.0, tk.END).strip()
        if not text:
            messagebox.showwarning("提示", "正文区域为空！")
            return
        
        sys_prompt = "你是一名专业的网文润色编辑。请在保持原文情节和人物不变的前提下，优化以下文本的文笔、节奏感和五感描写。直接输出润色后的完整正文，不要加任何注释或说明。"
        
        def do_polish():
            self.disable_buttons()
            self.result_text.delete(1.0, tk.END)
            self.result_text.insert(tk.END, "✨ 正在润色中...\n")
            try:
                polished = self.call_llm_non_stream(sys_prompt, text, temp=0.5)
                self.result_text.delete(1.0, tk.END)
                self.result_text.insert(tk.END, polished)
                self.update_word_count()
                messagebox.showinfo("成功", "润色完成！请检查后保存。")
            except Exception as e:
                messagebox.showerror("错误", f"润色失败: {str(e)}")
            finally:
                self.btn_new.config(state=tk.NORMAL)
                self.btn_continue.config(state=tk.NORMAL)
                self.btn_save_new.config(state=tk.NORMAL)
        
        threading.Thread(target=do_polish, daemon=True).start()

    # ============================================================
    # 工具箱：逻辑/OOC 检查
    # ============================================================
    def logic_check(self):
        text = self.result_text.get(1.0, tk.END).strip()
        if not text:
            messagebox.showwarning("提示", "正文区域为空！")
            return
        
        check_prompt = f"""请以"严苛的编辑审稿人"身份，对以下正文进行逻辑和OOC（人设崩塌）检查。

检查维度：
1. 【逻辑硬伤】：时间线矛盾、空间位移错误、已死角色复活等。
2. 【人设崩塌】：角色的言行是否符合其设定？（参考 system prompt 中的角色卡）
3. 【设定违背】：是否有违背世界观规则的描写？
4. 【节奏问题】：是否有水文、重复、或节奏断裂？

输出格式：
- 如果没有问题：输出"✅ 本章逻辑通过，未发现明显问题。"
- 如果有问题：逐条列出问题和修改建议。

【待检查正文】：
{text}"""

        def do_check():
            self.disable_buttons()
            self.result_text.delete(1.0, tk.END)
            self.result_text.insert(tk.END, "🔍 正在检查逻辑与人设...\n")
            try:
                # 给一个较大的 prompt 提供丰富上下文
                sys_prompt = self.build_system_prompt_gui(current_prompt=text)
                report = self.call_llm_non_stream(sys_prompt, check_prompt, temp=0.2)
                self.result_text.delete(1.0, tk.END)
                self.result_text.insert(tk.END, f"🔍 逻辑检查报告：\n\n{report}")
            except Exception as e:
                messagebox.showerror("错误", f"逻辑检查失败: {str(e)}")
            finally:
                self.btn_new.config(state=tk.NORMAL)
                self.btn_continue.config(state=tk.NORMAL)
        
        threading.Thread(target=do_check, daemon=True).start()

    # ============================================================
    # 工具箱：打开历史章节
    # ============================================================
    def open_history_chapter(self):
        out_dir = generator.DIRS["out"]
        files = sorted([f for f in os.listdir(out_dir) if f.endswith(".txt") and f.startswith("第")], 
                       key=lambda x: x)
        if not files:
            messagebox.showinfo("提示", "当前没有已保存的章节文件。")
            return
        
        win = tk.Toplevel(self.root)
        win.title("📂 历史章节")
        win.geometry("400x500")
        win.grab_set()
        
        listbox = tk.Listbox(win, font=("微软雅黑", 10))
        listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        for f in files:
            listbox.insert(tk.END, f)
        
        def load_selected():
            sel = listbox.curselection()
            if not sel: return
            fname = files[sel[0]]
            fpath = os.path.join(out_dir, fname)
            content = generator.read_text_safe(fpath)
            self.result_text.delete(1.0, tk.END)
            self.result_text.insert(tk.END, content)
            self.update_word_count()
            win.destroy()
            
        ttk.Button(win, text="📖 加载到编辑区", command=load_selected).pack(pady=10)

    # ============================================================
    # 工具箱：一键打包导出全文
    # ============================================================
    def export_book(self):
        out_dir = generator.DIRS.get("out", "output")
        if not os.path.exists(out_dir):
            messagebox.showwarning("提示", "当前没有输出文件夹，无法导出！")
            return
            
        chapter_files = []
        for root, dirs, files in os.walk(out_dir):
            for f in sorted(files):
                if f.endswith(".txt") and f.startswith("第") and "章" in f:
                    chapter_files.append(os.path.join(root, f))
                    
        if not chapter_files:
            messagebox.showinfo("提示", "当前没有写好的章节，无内容可导出。")
            return
            
        def _extract_chap_num(fpath):
            """从文件路径中提取章节号用于数字排序"""
            basename = os.path.basename(fpath)
            m = re.search(r'第(\d+)章', basename)
            return int(m.group(1)) if m else 0
        chapter_files = sorted(chapter_files, key=_extract_chap_num)
        
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
                        
                        # 第一行通常是包含"第X章"的标题
                        if not title_written and clean_line.startswith("第") and "章" in clean_line:
                            # 章节标题前后空行，顶格不缩进
                            formatted_lines.append(f"\n\n\n{clean_line}\n")
                            title_written = True
                        elif clean_line.startswith("---") or clean_line.startswith("==="):
                            # 作者有话说或者分割线
                            formatted_lines.append(f"\n{clean_line}\n")
                        else:
                            # 正文：强制中文全角两字符的严格首行缩进
                            formatted_lines.append(f"　　{clean_line}")
                    
                    out_f.write("\n".join(formatted_lines))
                    out_f.write("\n")
                    
            messagebox.showinfo("成功", f"全书已一键排版导出至：\n{export_path}\n\n（所有章节自动合并、空行已清理替换为标准的中文首行缩进，可直接复制上架各大小说网！）")
        except Exception as e:
            messagebox.showerror("导出失败", f"导出过程中遇到错误: {str(e)}")

# ============================================================
# 启动
# ============================================================
if __name__ == "__main__":
    root = tk.Tk()
    app = NovelGeneratorGUI(root)
    root.mainloop()
