# -*- coding: utf-8 -*-
"""
多流派小说设定拆解工具 (Setting Extractor)
==========================================
基于 MiniMax 2.5 API 的自动化小说设定提取系统。

功能：
  1. 读取本地 .txt 小说文件
  2. 自动鉴定流派（FPS/MMO/修仙/同人/无限流/通用）
  3. 按滑动窗口分块，逐块调用 API 提取结构化设定
  4. 增量合并写入 .md 设定档案

使用方法：
  1. 在 extract_config.json 中配置 MiniMax API Key
  2. 运行：python setting_extractor.py
"""

import os
import sys
import json
import time
import re
import threading
from datetime import datetime
import glob

try:
    from openai import OpenAI, APIError, RateLimitError, APIConnectionError
except ImportError:
    print("[致命错误] 未安装 openai 库。请运行：pip install openai")
    sys.exit(1)

# ============================================================
# 系统常量
# ============================================================
# 兼容 PyInstaller 打包：打包后 __file__ 指向临时目录，改用 exe 所在目录
if getattr(sys, 'frozen', False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "extract_config.json")
PROMPTS_FILE = os.path.join(SCRIPT_DIR, "prompts.json")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "extracted_settings")

# 已知流派标签
KNOWN_GENRES = {"FPS", "MMO", "Xianxia", "Fanfic", "Infinite", "General", "KuiQian"}

# 流派 → 提取器 Prompt 名称映射
GENRE_TO_PROMPT = {
    "FPS":      "Extract_FPS",
    "MMO":      "Extract_MMO",
    "Xianxia":  "Extract_Xianxia",
    "Fanfic":   "Extract_Fanfic",
    "Infinite": "Extract_Infinite",
    "General":  "Extract_General",
    "KuiQian":  "Extract_KuiQian",
}


# ============================================================
# 模块 1：配置加载
# ============================================================
def load_config() -> dict:
    """加载 extract_config.json"""
    default = {
        "api_key": "YOUR_MINIMAX_API_KEY",
        "base_url": "https://api.minimaxi.com/v1",
        "model": "MiniMax-M2.5",
        "temperature": 0.3,
        "max_tokens": 4096,
        "chunk_size": 5000,
        "chunk_overlap": 500,
        "router_sample_size": 5000,
        "max_retries": 3,
        "retry_delay": 2,
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=4, ensure_ascii=False)
        print(f"[提示] 已生成默认配置文件：{CONFIG_FILE}，请填入 API Key 后重新运行。")
        sys.exit(0)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # 用默认值填充缺失字段
        for k, v in default.items():
            cfg.setdefault(k, v)
        return cfg
    except Exception as e:
        print(f"[错误] 加载配置失败：{e}")
        sys.exit(1)


# ============================================================
# 模块 2：提示词解析
# ============================================================
def load_prompts(json_path: str = PROMPTS_FILE) -> dict:
    """解析 prompts.json，返回 {prompt_name: content_string} 字典"""
    if not os.path.exists(json_path):
        print(f"[错误] 提示词库不存在：{json_path}")
        sys.exit(1)
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        prompts = {}
        for key, val in raw.items():
            if isinstance(val, dict) and "content" in val:
                prompts[key] = val["content"]
            elif isinstance(val, str):
                prompts[key] = val
        return prompts
    except Exception as e:
        print(f"[错误] 解析提示词库失败：{e}")
        sys.exit(1)


def get_prompt(prompts: dict, name: str) -> str:
    """按名称检索 prompt 内容"""
    if name not in prompts:
        print(f"[警告] 提示词 '{name}' 不存在，可用项：{list(prompts.keys())}")
        return ""
    return prompts[name]


# ============================================================
# 模块 3：文件读取与分块
# ============================================================
def read_novel(filepath: str) -> str:
    """读取 .txt 小说文件，自动处理编码"""
    encodings = ["utf-8", "gbk", "gb2312", "utf-16", "latin-1"]
    for enc in encodings:
        try:
            with open(filepath, "r", encoding=enc) as f:
                text = f.read()
            return text.strip()
        except (UnicodeDecodeError, UnicodeError):
            continue
    print(f"[错误] 无法以任何已知编码读取文件：{filepath}")
    return ""


def chunk_text(text: str, chunk_size: int = 5000, overlap: int = 500) -> list:
    """
    按固定字数分块，块之间保留重叠区以避免截断。
    返回 chunk 列表。
    """
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end]
        chunks.append(chunk)
        # 下一块的起始位置 = 当前结束 - 重叠区
        start = end - overlap
        if start >= len(text):
            break
    return chunks


# ============================================================
# 模块 4：API 调度（含重试）
# ============================================================
def create_client(config: dict) -> OpenAI:
    """创建 OpenAI 兼容客户端"""
    return OpenAI(api_key=config["api_key"], base_url=config["base_url"])


def call_minimax(client: OpenAI, config: dict,
                 system_prompt: str, user_content: str) -> str:
    """
    调用 MiniMax API，内置指数退避重试。
    返回模型生成的文本。
    """
    max_retries = config.get("max_retries", 3)
    retry_delay = config.get("retry_delay", 2)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    for attempt in range(1, max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=config["model"],
                messages=messages,
                temperature=config["temperature"],
                max_tokens=config["max_tokens"],
            )
            content = response.choices[0].message.content
            return content.strip() if content else ""

        except RateLimitError as e:
            wait = retry_delay * (2 ** (attempt - 1))
            print(f"  [限流] 第 {attempt}/{max_retries} 次重试，等待 {wait}s ... ({e})")
            time.sleep(wait)

        except (APIError, APIConnectionError) as e:
            wait = retry_delay * (2 ** (attempt - 1))
            print(f"  [API错误] 第 {attempt}/{max_retries} 次重试，等待 {wait}s ... ({e})")
            time.sleep(wait)

        except Exception as e:
            print(f"  [未知错误] {e}")
            if attempt < max_retries:
                time.sleep(retry_delay)
            else:
                raise

    print("[错误] 已达最大重试次数，跳过本次调用。")
    return ""


# ============================================================
# 模块 5：流派路由
# ============================================================
def route_genre(client: OpenAI, config: dict,
                prompts: dict, sample_text: str) -> str:
    """
    用 Router_Scan 提示词分析小说前 N 字，返回流派标签。
    若返回不在已知列表中则回退为 General。
    """
    router_prompt = get_prompt(prompts, "Router_Scan")
    if not router_prompt:
        print("[警告] 路由提示词缺失，默认使用 General。")
        return "General"

    # 替换占位符
    filled_prompt = router_prompt.replace("{current_chunk}", sample_text)

    print("🔍 正在鉴定小说流派...")
    result = call_minimax(client, config, filled_prompt, "请分析并输出标签。")

    # 从返回中提取标签（模型可能附加了解释文字）
    result_clean = result.strip()
    for genre in KNOWN_GENRES:
        if genre.lower() in result_clean.lower():
            print(f"✅ 流派鉴定结果：{genre}")
            return genre

    print(f"⚠️ 无法识别流派（API返回: '{result_clean}'），默认使用 General。")
    return "General"


# ============================================================
# 模块 6：上下文管理 (State Memory)
# ============================================================
def read_existing_md(md_path: str) -> str:
    """读取已有的 .md 设定文件作为上下文记忆"""
    if not os.path.exists(md_path):
        return ""
    try:
        with open(md_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def compress_context(context: str, max_chars: int = 3000) -> str:
    """
    当上下文过长时进行截取压缩。
    策略：保留 Markdown 标题结构 + 每个段落的前几行。
    """
    if len(context) <= max_chars:
        return context

    lines = context.split("\n")
    compressed = []
    current_len = 0
    section_line_count = 0

    for line in lines:
        # 始终保留标题行
        if line.startswith("#"):
            compressed.append(line)
            current_len += len(line) + 1
            section_line_count = 0
        elif section_line_count < 5:  # 每个段落最多保留5行内容
            compressed.append(line)
            current_len += len(line) + 1
            section_line_count += 1

        if current_len >= max_chars:
            compressed.append("\n... (上下文已压缩，更多详情见设定档案原文) ...")
            break

    return "\n".join(compressed)


# ============================================================
# 模块 7：安全文件写入
# ============================================================
def safe_write_md(filepath: str, content: str):
    """
    安全写入 Markdown 文件。
    先写入 .tmp 临时文件，成功后原子替换。
    """
    tmp_path = filepath + ".tmp"
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        # 原子替换（Windows 上 os.replace 可覆盖已有文件）
        os.replace(tmp_path, filepath)
    except Exception as e:
        print(f"[错误] 写入文件失败：{e}")
        # 清理临时文件
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


# ============================================================
# 模块 8：Markdown 模板
# ============================================================
MD_TEMPLATES = {
    "FPS": """# 设定档案：{novel_name}

## 1. 战术信息与感知系统
* 听觉收集：
* 视野与侦察：
* 报点黑话：

## 2. 枪匠改装与战术防具
* 主力武器与改装方案：
* 防具配置：

## 3. 干员状态与小队协同
* 角色状态更新：
* 战术协同记录：

## 4. 经济与撤离博弈
* 高保值物资：
* 撤离路线/点位博弈：

## 5. 设定冲突检测
""",

    "General": """# 设定档案：{novel_name}

## 1. 角色图谱
* （待提取）

## 2. 世界观与规则
* （待提取）

## 3. 关键道具与奇物
* （待提取）

## 4. 伏笔追踪
* [ ] 待解：
* [x] 已解：

## 5. 设定冲突检测
""",

    "MMO": """# 设定档案：{novel_name}

## 1. 角色与职业体系
* （待提取）

## 2. 副本与BOSS机制
* （待提取）

## 3. 装备与经济系统
* （待提取）

## 4. 公会与社交政治
* （待提取）

## 5. 设定冲突检测
""",

    "Xianxia": """# 设定档案：{novel_name}

## 1. 修炼体系
* （待提取）

## 2. 宗门与势力
* （待提取）

## 3. 法宝与丹药
* （待提取）

## 4. 天地规则
* （待提取）

## 5. 设定冲突检测
""",

    "Fanfic": """# 设定档案：{novel_name}

## 1. 原作对照
* （待提取）

## 2. 角色映射
* （待提取）

## 3. 金手指与系统
* （待提取）

## 4. 剧情偏移追踪
* （待提取）

## 5. 设定冲突检测
""",

    "Infinite": """# 设定档案：{novel_name}

## 1. 主神系统
* （待提取）

## 2. 副本世界
* （待提取）

## 3. 强化体系
* （待提取）

## 4. 团队与势力
* （待提取）

## 5. 设定冲突检测
""",

    "KuiQian": """# 设定档案：{novel_name}

## 1. 亏损系统限制与本期资金
* （待提取）

## 2. 反向拉胯商业计划
* （待提取）

## 3. 员工背刺与外界迪化
* （待提取）

## 4. 最终反向盈利结果与情绪
* （待提取）

## 5. 设定冲突检测
""",
}


def init_md_template(genre: str, novel_name: str) -> str:
    """根据流派标签生成对应的 Markdown 模板骨架"""
    template = MD_TEMPLATES.get(genre, MD_TEMPLATES["General"])
    return template.replace("{novel_name}", novel_name)


# ============================================================
# 模块 9：增量合并
# ============================================================
def merge_into_md(existing_md: str, new_extraction: str,
                  client: OpenAI, config: dict) -> str:
    """
    将新提取的设定增量合并到已有的 Markdown 设定档案中。
    使用 LLM 来智能合并以保持结构一致性。
    """
    if not existing_md.strip():
        return new_extraction

    merge_prompt = (
        "你是一个精确的文档合并工具。你的任务是将【新提取的设定数据】"
        "增量合并到【现有设定档案】中。\n\n"
        "合并规则：\n"
        "1. 保持现有档案的 Markdown 标题结构不变\n"
        "2. 新数据追加到对应分类下，不要删除旧数据\n"
        "3. 如果新旧数据有重复，保留最新版本\n"
        "4. 如果新数据指出设定冲突，追加到「设定冲突检测」段落\n"
        "5. 输出完整的合并后 Markdown 文档\n"
        "6. 不要添加任何解释性文字，只输出合并后的文档\n\n"
        f"【现有设定档案】：\n{existing_md}\n\n"
        f"【新提取的设定数据】：\n{new_extraction}"
    )

    result = call_minimax(client, config, merge_prompt, "请执行合并并输出完整文档。")
    return result if result else existing_md


# ============================================================
# 主流程
# ============================================================
def print_banner():
    """打印启动横幅"""
    print("\n" + "=" * 62)
    print(" 🔬 多流派小说设定拆解工具 v1.1 ".center(58))
    print("    基于 MiniMax 2.5 | 自动流派鉴定 | 增量设定提取  ".center(58))
    print("=" * 62)


def timed_input(prompt: str, timeout: int = 60, default: str = "") -> str:
    """
    带超时的 input。超时后自动返回 default 值。
    在 Windows 上使用 threading 实现。
    """
    result = [default]

    def _read():
        try:
            result[0] = input(prompt)
        except EOFError:
            pass

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        print(f"\n  ⏰ {timeout}秒无操作，自动继续...")
    return result[0]


def get_novel_path() -> str:
    """交互式获取小说文件路径"""
    print("\n请输入小说 .txt 文件的完整路径（支持拖拽文件到此窗口）：")
    path = input("> ").strip().strip("\"'").strip()
    if not path:
        return ""
    if not os.path.exists(path):
        print(f"[错误] 文件不存在：{path}")
        return ""
    if not path.lower().endswith(".txt"):
        print("[错误] 请提供 .txt 格式的小说文件。")
        return ""
    return path


def get_novel_name(filepath: str) -> str:
    """从文件路径提取或让用户输入作品名"""
    default_name = os.path.splitext(os.path.basename(filepath))[0]
    print(f"\n作品名称（直接回车使用文件名 '{default_name}'）：")
    name = input("> ").strip()
    return name if name else default_name


def estimate_chapters(total_chars: int, chars_per_chapter: int = 3000) -> int:
    """估算总章节数"""
    return max(1, total_chars // chars_per_chapter)


def sparse_sample(text: str, sample_size: int = 5000,
                  interval: int = 100000) -> list:
    """
    稀疏采样：每隔 interval 字取一个 sample_size 的片段。
    用于全书快速概览，以最少 API 调用覆盖全书脉络。
    """
    samples = []
    pos = 0
    while pos < len(text):
        chunk = text[pos:pos + sample_size]
        if chunk.strip():
            samples.append(chunk)
        pos += interval
    return samples


def run_chunk_extraction(chunks: list, extractor_prompt: str,
                         md_path: str, client, config: dict,
                         label: str = ""):
    """
    通用的分块提取+合并循环。返回实际处理的块数。
    """
    total = len(chunks)
    processed = 0

    for i, chunk in enumerate(chunks):
        tag = f"[{label} {i+1}/{total}]" if label else f"[{i+1}/{total}]"
        print(f"\n{tag} 处理中...")

        context_memory = read_existing_md(md_path)
        context_memory = compress_context(context_memory, max_chars=3000)

        filled = extractor_prompt.replace(
            "{context_memory}",
            context_memory if context_memory else "（暂无已提取设定）"
        )
        filled = filled.replace("{current_chunk}", chunk)

        print(f"  🤖 调用 MiniMax API...")
        new_data = call_minimax(client, config, filled,
                                "请严格按照提取规则分析当前文本片段并输出结构化设定。")

        if not new_data:
            print(f"  ⚠️ 未返回数据，跳过。")
            continue

        print(f"  📎 合并到设定档案...")
        existing_md = read_existing_md(md_path)
        merged = merge_into_md(existing_md, new_data, client, config)
        safe_write_md(md_path, merged)
        processed += 1
        print(f"  ✅ 完成。")

        if i < total - 1:
            time.sleep(1)

    return processed


def run_extraction(filepath: str, novel_name: str,
                   config: dict, prompts: dict):
    """
    三阶段智能拆解流程：
      阶段1 - 全书速览：稀疏采样覆盖全本，快速提取核心设定骨架
      阶段2 - 精读前100章：逐块详细提取
      阶段3 - 分段续扫：每次追加100章，用户决定是否继续
    """
    # 1. 读取小说
    print(f"\n📖 正在读取文件：{filepath}")
    full_text = read_novel(filepath)
    if not full_text:
        print("[错误] 文件读取失败或为空。")
        return

    total_chars = len(full_text)
    est_chapters = estimate_chapters(total_chars)
    print(f"   总字数：{total_chars:,} 字（预估 ~{est_chapters} 章）")

    # 2. 创建 API 客户端
    if config["api_key"] in ["YOUR_MINIMAX_API_KEY", ""]:
        print("\n[致命错误] 未配置 API Key！")
        return

    client = create_client(config)

    # 3. 流派路由
    sample_size = config.get("router_sample_size", 5000)
    sample = full_text[:sample_size]
    genre = route_genre(client, config, prompts, sample)

    # 3.5 让用户确认或手动选择流派
    genre_list = sorted(KNOWN_GENRES)
    print(f"\n  当前自动鉴定结果：{genre}")
    print(f"  直接回车接受，或输入编号手动选择流派：")
    for idx, g in enumerate(genre_list, 1):
        marker = " ← 当前" if g == genre else ""
        print(f"    {idx}. {g}{marker}")
    override = input("  > ").strip()
    if override.isdigit() and 1 <= int(override) <= len(genre_list):
        genre = genre_list[int(override) - 1]
        print(f"  ✅ 已手动切换流派为：{genre}")
    elif override == "":
        print(f"  ✅ 使用自动鉴定结果：{genre}")
    else:
        print(f"  ⚠️ 无效输入，继续使用自动鉴定结果：{genre}")

    # 4. 确定提取器
    extractor_name = GENRE_TO_PROMPT.get(genre, "Extract_General")
    extractor_prompt = get_prompt(prompts, extractor_name)
    if not extractor_prompt:
        print(f"[错误] 找不到提取器 '{extractor_name}'。")
        return

    # 5. 初始化输出 Markdown
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    safe_name = re.sub(r'[\\/:*?"<>|]', '_', novel_name)
    md_filename = f"设定档案_{safe_name}.md"
    md_path = os.path.join(OUTPUT_DIR, md_filename)

    if not os.path.exists(md_path):
        template = init_md_template(genre, novel_name)
        safe_write_md(md_path, template)
        print(f"📝 已初始化设定档案：{md_path}")
    else:
        print(f"📝 检测到已有档案，将进行增量更新：{md_path}")

    start_time = time.time()
    chunk_size = config.get("chunk_size", 5000)
    chunk_overlap = config.get("chunk_overlap", 500)

    # =========================================
    # 阶段1：全书速览（稀疏采样）
    # =========================================
    print(f"\n{'='*62}")
    print(f" ⚡ 阶段1：全书速览 — 稀疏采样提取核心骨架")
    print(f"{'='*62}")

    skim_samples = sparse_sample(full_text, sample_size=5000, interval=100000)
    skim_count = len(skim_samples)
    est_skim_calls = skim_count * 2
    print(f"  策略：每10万字取5000字片段，共 {skim_count} 个采样点")
    print(f"  预计 API 调用：~{est_skim_calls} 次")

    print(f"\n  开始速览？(y/n，直接回车开始，60秒无操作自动开始)")
    if timed_input("  > ", timeout=60, default="").strip().lower() in ("n", "no", "否"):
        print("  跳过速览。")
    else:
        processed = run_chunk_extraction(
            skim_samples, extractor_prompt, md_path, client, config,
            label="速览"
        )
        print(f"\n  ⚡ 速览完成！处理 {processed}/{skim_count} 个采样点。")

    # =========================================
    # 阶段2：精读 或 全读
    # =========================================
    CHARS_PER_BATCH = 300000  # 100章 ≈ 30万字
    processed_up_to = 0

    all_chunks = chunk_text(full_text, chunk_size, chunk_overlap)
    detail_end = min(total_chars, CHARS_PER_BATCH)
    detail_text = full_text[processed_up_to:detail_end]
    detail_chunks = chunk_text(detail_text, chunk_size, chunk_overlap)
    est_chaps = estimate_chapters(detail_end)

    print(f"\n{'='*62}")
    print(f" 🔍 阶段2：精读 / 全读")
    print(f"{'='*62}")
    print(f"  全书共 {total_chars:,} 字（~{estimate_chapters(total_chars)} 章）")
    print(f"  精读前 ~{est_chaps} 章 = {len(detail_chunks)} 个块，~{len(detail_chunks)*2} 次 API")
    print(f"  全读全部 = {len(all_chunks)} 个块，~{len(all_chunks)*2} 次 API")
    print(f"\n  请选择（60秒无操作自动精读）：")
    print(f"    回车/1  - 精读前 ~{est_chaps} 章")
    print(f"    2       - 全读整本书")
    print(f"    n       - 跳过")

    phase2_choice = timed_input("  > ", timeout=60, default="1").strip().lower()
    if phase2_choice in ("n", "no", "否"):
        print("  跳过阶段2。")
    elif phase2_choice == "2":
        print(f"\n  📖 全读模式：处理全部 {len(all_chunks)} 个块...")
        processed = run_chunk_extraction(
            all_chunks, extractor_prompt, md_path, client, config,
            label="全读"
        )
        processed_up_to = total_chars
        print(f"\n  📖 全读完成！已处理全书 ~{estimate_chapters(processed_up_to)} 章。")
    else:
        print(f"\n  🔍 精读模式：处理前 {len(detail_chunks)} 个块...")
        processed = run_chunk_extraction(
            detail_chunks, extractor_prompt, md_path, client, config,
            label="精读"
        )
        processed_up_to = detail_end
        print(f"\n  🔍 精读完成！已处理到第 ~{estimate_chapters(processed_up_to)} 章。")

    # =========================================
    # 阶段3：分段续扫
    # =========================================
    while processed_up_to < total_chars:
        remaining = total_chars - processed_up_to
        remaining_chaps = estimate_chapters(remaining)

        print(f"\n{'─'*62}")
        print(f"  📊 当前进度：已扫到第 ~{estimate_chapters(processed_up_to)} 章")
        print(f"      剩余未扫：~{remaining_chaps} 章（{remaining:,} 字）")
        print(f"\n  是否继续扫描下一批？（60秒无操作自动继续）")
        print(f"    回车/y  - 继续 100 章")
        print(f"    n       - 停止，保存当前成果")
        print(f"    数字    - 输入自定义章数")

        choice = timed_input("  > ", timeout=60, default="").strip().lower()
        if choice in ("n", "no", "否"):
            break
        elif choice.isdigit() and int(choice) > 0:
            next_batch = int(choice) * 3000
        elif choice in ("y", "yes", "是", ""):
            next_batch = CHARS_PER_BATCH
        else:
            break

        next_end = min(total_chars, processed_up_to + next_batch)
        next_text = full_text[processed_up_to:next_end]
        next_chunks = chunk_text(next_text, chunk_size, chunk_overlap)

        print(f"\n{'='*62}")
        print(f" 🔍 续扫：第 ~{estimate_chapters(processed_up_to)+1} 到 ~{estimate_chapters(next_end)} 章")
        print(f"{'='*62}")
        print(f"  共 {len(next_chunks)} 个块")

        run_chunk_extraction(
            next_chunks, extractor_prompt, md_path, client, config,
            label="续扫"
        )
        processed_up_to = next_end
        print(f"\n  ✅ 续扫完成！已扫到第 ~{estimate_chapters(processed_up_to)} 章。")

    # 完成汇总
    elapsed = time.time() - start_time
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)

    print(f"\n{'='*62}")
    print(f" 🎉 拆解完成！")
    print(f"    作品：{novel_name}")
    print(f"    流派：{genre}")
    print(f"    已扫范围：前 ~{estimate_chapters(processed_up_to)} 章")
    print(f"    耗时：{minutes}分{seconds}秒")
    print(f"    输出文件：{md_path}")
    print(f"{'='*62}\n")

    return True


def run_batch_extraction(folder_path: str, config: dict, prompts: dict):
    """
    批量拆解：扫描指定文件夹下所有 .txt 文件，逐一执行设定拆解。
    """
    # 收集所有 .txt 文件
    txt_files = glob.glob(os.path.join(folder_path, "*.txt"))
    if not txt_files:
        print(f"\n[错误] 文件夹中没有找到 .txt 文件：{folder_path}")
        return

    txt_files.sort()  # 按文件名排序
    total = len(txt_files)

    print(f"\n{'='*62}")
    print(f" 📚 批量拆解模式")
    print(f"    文件夹：{folder_path}")
    print(f"    发现 {total} 个 .txt 文件")
    print(f"{'='*62}")

    # 显示文件列表，标注已有档案的
    already_done = set()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for i, f in enumerate(txt_files, 1):
        basename = os.path.basename(f)
        name_no_ext = os.path.splitext(basename)[0]
        safe_name = re.sub(r'[\\/:*?"<>|]', '_', name_no_ext)
        md_path = os.path.join(OUTPUT_DIR, f"设定档案_{safe_name}.md")
        size = os.path.getsize(f)
        if os.path.exists(md_path):
            already_done.add(i - 1)  # 0-indexed
            print(f"  {i:3d}. {basename}  ({size:,} 字节)  [已有档案]")
        else:
            print(f"  {i:3d}. {basename}  ({size:,} 字节)")

    # 跳过选项
    skip_existing = False
    if already_done:
        print(f"\n  其中 {len(already_done)} 个文件已有设定档案。")
        print(f"  是否跳过已有档案的文件？(y/n，默认跳过)")
        skip_choice = input("> ").strip().lower()
        skip_existing = skip_choice not in ("n", "no", "否")
        if skip_existing:
            print(f"  将跳过 {len(already_done)} 个已处理的文件。")

    print(f"\n确认开始批量拆解？(y/n)")
    confirm = input("> ").strip().lower()
    if confirm not in ("y", "yes", "是", ""):
        print("已取消。")
        return

    # 逐一处理
    batch_start = time.time()
    results = []  # (文件名, 状态, 耗时)

    for idx, filepath in enumerate(txt_files):
        basename = os.path.basename(filepath)
        novel_name = os.path.splitext(basename)[0]

        # 跳过已处理的文件
        if skip_existing and idx in already_done:
            print(f"\n  ⏭️  [{idx+1}/{total}] {basename} — 已有档案，跳过")
            results.append((basename, "⏭️ 跳过", "-"))
            continue

        print(f"\n{'━'*62}")
        print(f" 📕 [{idx+1}/{total}] {basename}")
        print(f"{'━'*62}")

        file_start = time.time()
        try:
            success = run_extraction(filepath, novel_name, config, prompts)
            elapsed = time.time() - file_start
            if success:
                results.append((basename, "✅ 成功", f"{elapsed:.0f}s"))
            else:
                results.append((basename, "❌ 失败", f"{elapsed:.0f}s"))
        except KeyboardInterrupt:
            results.append((basename, "⏹️ 中断", "-"))
            print(f"\n[中断] 用户手动停止。已完成 {idx}/{total} 个文件。")
            break
        except Exception as e:
            elapsed = time.time() - file_start
            results.append((basename, f"❌ 错误: {e}", f"{elapsed:.0f}s"))
            print(f"  [错误] 处理失败：{e}，跳过此文件。")
            continue

    # 批量结果汇总
    batch_elapsed = time.time() - batch_start
    batch_min = int(batch_elapsed // 60)
    batch_sec = int(batch_elapsed % 60)

    print(f"\n\n{'='*62}")
    print(f" 📊 批量拆解汇总报告")
    print(f"{'='*62}")
    print(f"  总文件数：{total}")
    print(f"  总耗时：{batch_min}分{batch_sec}秒")
    print(f"  输出目录：{OUTPUT_DIR}")
    print(f"{'─'*62}")
    print(f"  {'序号':>4}  {'文件名':<30} {'状态':<12} {'耗时':>6}")
    print(f"  {'─'*4}  {'─'*30} {'─'*12} {'─'*6}")
    for i, (name, status, elapsed) in enumerate(results, 1):
        # 截断过长的文件名
        display_name = name if len(name) <= 30 else name[:27] + "..."
        print(f"  {i:4d}  {display_name:<30} {status:<12} {elapsed:>6}")

    success_count = sum(1 for _, s, _ in results if "成功" in s)
    fail_count = len(results) - success_count
    print(f"{'─'*62}")
    print(f"  成功：{success_count}  |  失败/中断：{fail_count}")
    print(f"{'='*62}\n")


def get_folder_path() -> str:
    """交互式获取文件夹路径"""
    print("\n请输入包含 .txt 小说文件的文件夹路径（支持拖拽文件夹到此窗口）：")
    path = input("> ").strip().strip("\"'").strip()
    if not path:
        return ""
    if not os.path.isdir(path):
        print(f"[错误] 不是有效的文件夹路径：{path}")
        return ""
    return path


def main():
    """主入口"""
    print_banner()

    # 加载配置和提示词
    config = load_config()
    prompts = load_prompts()
    print(f"✅ 已加载 {len(prompts)} 个提示词模板")

    while True:
        print("\n" + "-" * 40)
        print("请选择操作：")
        print("  1. 📖 拆解单本小说")
        print("  2. 📚 批量拆解（整个文件夹）")
        print("  3. 📂 查看已有设定档案")
        print("  0. 🚪 退出")
        print("-" * 40)

        choice = input("> ").strip()

        if choice == "1":
            filepath = get_novel_path()
            if not filepath:
                continue
            novel_name = get_novel_name(filepath)
            run_extraction(filepath, novel_name, config, prompts)

        elif choice == "2":
            folder = get_folder_path()
            if not folder:
                continue
            run_batch_extraction(folder, config, prompts)

        elif choice == "3":
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            files = [f for f in os.listdir(OUTPUT_DIR) if f.endswith(".md")]
            if not files:
                print("\n  （还没有任何设定档案）")
            else:
                print(f"\n📂 已有设定档案（{OUTPUT_DIR}）：")
                for i, f in enumerate(files, 1):
                    size = os.path.getsize(os.path.join(OUTPUT_DIR, f))
                    print(f"  {i}. {f}  ({size:,} 字节)")

        elif choice == "0":
            print("\n再见！祝阅读愉快。📚")
            break

        else:
            print("无效输入，请重试。")


if __name__ == "__main__":
    main()
