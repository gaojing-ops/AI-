# -*- coding: utf-8 -*-
"""
小说生成器 (Pro 1000章特供版) - 模块化上下文长篇小说系统
=============================================================
设计目标：解决1000章超长篇小说的“设定记忆灾难”和“Token 消耗过大”问题。
使用方法：
    1. 在 config.json 中配置 API Key
    2. 运行脚本：python generator.py
"""

import os
import sys
import json
import time
from datetime import datetime
from openai import OpenAI
import glob

# ============================================================
# 系统基础配置
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")

# 目录结构映射
DIRS = {
    "world": os.path.join(SCRIPT_DIR, "world_building"),
    "chars": os.path.join(SCRIPT_DIR, "characters"),
    "plot":  os.path.join(SCRIPT_DIR, "plot"),
    "out":   os.path.join(SCRIPT_DIR, "output"),
    "hist":  os.path.join(SCRIPT_DIR, "output", "history"),
}

# 确保目录和配置文件存在
for d in DIRS.values():
    os.makedirs(d, exist_ok=True)

def load_config():
    """加载配置，如果没有则创建一个默认的"""
    default_config = {
        "api_key": "YOUR_DEEPSEEK_API_KEY_HERE",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "temperature": 0.8,
        "max_tokens": 4096,
        "current_volume": 1,
        "author_name": "匿名作者"
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(default_config, f, indent=4, ensure_ascii=False)
        return default_config
        
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[错误] 无法加载配置 config.json: {e}")
        return default_config

config = load_config()

# 覆盖环境变量的 API KEY (若存在)
env_key = os.environ.get("DEEPSEEK_API_KEY")
if env_key:
    config["api_key"] = env_key


# ============================================================
# 核心功能模块
# ============================================================

def read_text_safe(filepath: str) -> str:
    if not os.path.exists(filepath):
        return ""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""

def list_files_in_dir(directory: str) -> dict:
    """返回目录下所有 txt 文件的 {文件名(不含扩展名): 完整路径} 映射"""
    files_map = {}
    pattern = os.path.join(directory, "*.txt")
    for file in glob.glob(pattern):
        name = os.path.splitext(os.path.basename(file))[0]
        files_map[name] = file
    return files_map

def select_dynamic_context(category: str, files_map: dict) -> list:
    """让用户多选本章需要载入的设定文件（防止全量加载导致Token爆炸）"""
    if not files_map:
        return []
        
    names = list(files_map.keys())
    print(f"\n[{category}] 知识库:")
    for i, name in enumerate(names, 1):
        print(f"  {i}. {name}")
    
    print(f"请输入本章需要出场/涉及的 {category} 编号（用空格分隔多个，直接回车跳过）:")
    choices = input("> ").strip().split()
    
    selected_contents = []
    for c in choices:
        if c.isdigit() and 1 <= int(c) <= len(names):
            selected_name = names[int(c)-1]
            content = read_text_safe(files_map[selected_name])
            if content:
                selected_contents.append(f"【{selected_name}】\n{content}")
                
    return selected_contents

def get_latest_chapter_info() -> tuple:
    """自动计算当前系统的卷号和最新章号"""
    vol = config.get("current_volume", 1)
    vol_dir = os.path.join(DIRS["out"], f"第{vol:02d}卷")
    os.makedirs(vol_dir, exist_ok=True)
    
    pattern = os.path.join(vol_dir, "第*.txt")
    files = glob.glob(pattern)
    
    latest_chap = 0
    for file in files:
        basename = os.path.basename(file)
        try:
            # 解析 "第0015章.txt" 这种名字
            num_str = basename.replace("第", "").replace("章.txt", "")
            if num_str.isdigit():
                latest_chap = max(latest_chap, int(num_str))
        except:
            pass
            
    next_chap = latest_chap + 1
    filename = f"第{next_chap:04d}章.txt"
    filepath = os.path.join(vol_dir, filename)
    
    # 获取“当前最新章”路径用于续写
    latest_filename = f"第{latest_chap:04d}章.txt"
    latest_filepath = os.path.join(vol_dir, latest_filename)
    if latest_chap == 0:
        latest_filepath = None
    
    return vol, next_chap, filepath, latest_chap, latest_filepath

def build_system_prompt() -> str:
    """
    智能构建系统 Prompt。
    包含：固定设定 (剧情大纲) + 动态设定 (用户选定的人物、世界观) + 保护指令
    改为 XML 标签结构，帮助模型更好地隔离上下文，避免记忆混乱。
    """
    prompt_parts = []
    
    # 1. 基础写作指令
    prompt_parts.append(
        "你是一名顶尖的网络小说首发网站白金作家，正在连载一部千章量级的长篇巨著。\n"
        "请严格遵守提供的数据库设定，你的目标是输出极具吸引力、行文流畅的【小说正文】。\n"
        "【绝对规则】：\n"
        "1. 只输出小说正文！严禁用任何形式与读者互动、不准加注释、不准写摘要、不准写标题。\n"
        "2. 不要使用过于翻译腔或播音腔的词汇，需符合网文阅读爽感与节奏。\n"
        "3. 如设定出现冲突，以当前的 <本章写作要求> 为最高优先级。\n"
        "4. 以下是你的全部记忆库，请严格遵循相应的设定标签。\n"
    )

    # 2. 读取各维度记忆层级（防崩盘多级记忆塔）
    # Layer 2: 长期历史记忆 (卷宗总结)
    history_archive = read_text_safe(os.path.join(DIRS["plot"], "历史卷宗概要.txt"))
    if history_archive:
        prompt_parts.append(f"<history_archive>\n{history_archive}\n</history_archive>\n")
    
    # 当前卷核心线
    master_plot = read_text_safe(os.path.join(DIRS["plot"], "当前卷大纲.txt"))
    if master_plot:
        prompt_parts.append(f"<current_volume_plot>\n{master_plot}\n</current_volume_plot>\n")

    # Layer 3: 极短期切片记忆 (上一章/备忘录状态)
    memo = read_text_safe(os.path.join(DIRS["plot"], "全局备忘录.txt"))
    if memo:
        prompt_parts.append(f"<recent_memo_state>\n{memo}\n</recent_memo_state>\n")
        
    foreshadowing = read_text_safe(os.path.join(DIRS["plot"], "伏笔与因果追踪表.txt"))
    if foreshadowing:
        prompt_parts.append(f"<foreshadowing_grid>\n{foreshadowing}\n</foreshadowing_grid>\n")

    # 3. 询问并动态加载本章出场人物
    chars_map = list_files_in_dir(DIRS["chars"])
    selected_chars = select_dynamic_context("角色档案", chars_map)
    if selected_chars:
        prompt_parts.append("<character_profiles>\n" + "\n\n".join(selected_chars) + "\n</character_profiles>\n")

    # 4. 询问并动态加载涉及的世界观/阵营/功法
    world_map = list_files_in_dir(DIRS["world"])
    selected_world = select_dynamic_context("世界观百科", world_map)
    if selected_world:
        prompt_parts.append("<world_building_rules>\n" + "\n\n".join(selected_world) + "\n</world_building_rules>\n")

    return "\n".join(prompt_parts)

def call_llm(system_prompt: str, user_prompt: str, existing_content: str = "") -> str:
    """调用大模型"""
    if config["api_key"] in ["YOUR_DEEPSEEK_API_KEY_HERE", ""]:
        print("\n[致命错误] 未配置 API Key！请打开 config.json 或设置环境变量 DEEPSEEK_API_KEY。")
        sys.exit(1)

    client = OpenAI(api_key=config["api_key"], base_url=config["base_url"])
    
    final_user_prompt = ""
    if existing_content:
        final_user_prompt += f"【本章已写内容（请紧接着以下内容继续往下写）】\n{existing_content}\n\n"
        final_user_prompt += "请根据上文的语境和情绪，无缝续写接下来的内容。\n\n"
    
    final_user_prompt += f"【后面的具体写作要求】\n{user_prompt}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": final_user_prompt}
    ]
    
    print("\n" + "=".center(60, "="))
    print(" 🚀 正在燃烧算力生成小说正文... ".center(58))
    print("=".center(60, "=") + "\n")

    full_content = ""
    try:
        stream = client.chat.completions.create(
            model=config["model"],
            messages=messages,
            temperature=config["temperature"],
            max_tokens=config["max_tokens"],
            stream=True,
        )

        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                text = chunk.choices[0].delta.content
                print(text, end="", flush=True)
                full_content += text

        print("\n")
        return full_content
    except KeyboardInterrupt:
        print("\n\n[中断] 用户手动停止生成。保留已生成部分。")
        return full_content
    except Exception as e:
        print(f"\n[API 错误]: {e}")
        return ""

def call_llm_tool(system_prompt: str, user_prompt: str) -> str:
    """专用于工具箱的大模型调用（非流式或流式均可，这里统一用流式以保持体验）"""
    return call_llm(system_prompt, user_prompt)

def get_multiline_input(prompt_text: str) -> str:
    print(f"\n{prompt_text}")
    print("（输入空行并回车结束当前内容输入）")
    lines = []
    while True:
        try:
            line = input("> " if not lines else "  ")
            if line.strip() == "" and lines:
                break
            if line.strip() != "":
                lines.append(line)
        except EOFError:
            break
    return "\n".join(lines)

def init_demo_files():
    """确保所有必要的子文件夹存在（不再创建演示文件）"""
    for d in DIRS.values():
        os.makedirs(d, exist_ok=True)

def toolbox_menu():
    """AI 灵感与工具箱菜单"""
    while True:
        print("\n" + "🛠️ 【AI 灵感与工具箱】".center(58))
        print("====== 📖 设定与大纲生成 (开书必备) ======")
        print("1. 🌍 一键生成【世界观设定】")
        print("2. 👤 一键生成【核心人物组档案】(需先有世界观)")
        print("3. 📜 一键生成【全书大纲】(需先有世界观和人物)")
        print("====== 🔧 写作辅助与质检 (连载必备) ======")
        print("4. ✨ 章节润色去 AI 味")
        print("5. 🔍 防崩盘逻辑/OOC质检")
        print("6. 💡 卡文破局方案生成")
        print("====== 🧠 长期记忆与伏笔管理 (防烂尾核心) ======")
        print("7. 🧊 提炼【全局记忆备忘录】(压缩上下文，防止遗忘)")
        print("8. 🕸️ 提取并登记【伏笔追踪表】(因果链穿透)")
        print("====== 📚 进阶参考 (高级) ======")
        print("9. 📖 拆解大师：从外部 txt 参考书中提取设定")
        print("0. 🔙 返回主菜单")
        print("-" * 62)
        
        choice = input("请输入工具选项(0-9): ").strip()
        
        if choice == '0':
            break
            
        elif choice == '1':
            theme = input("\n请输入你要写的小说题材和风格 (例如：赛博朋克 升级流 / 古言宅斗 细腻向): ").strip()
            if not theme: continue
            sys_prompt = "你是一个顶级网文主编和世界观架构师。"
            user_prompt = f"""请生成【{theme}】题材的长篇小说世界观，要求结构清晰、逻辑自洽。具体包含以下6点：
1. 地理设定
2. 社会阶层/体系
3. 核心规则（3-5条）
4. 力量/科技体系（请细化晋级门槛，增加一个‘代价’设定，避免主角升级太顺）
5. 核心冲突
6. 禁忌设定
每一点都要详实，不能空洞。"""
            content = call_llm_tool(sys_prompt, user_prompt)
            if content:
                path = os.path.join(DIRS["world"], "自动生成_世界观.txt")
                with open(path, "w", encoding="utf-8") as f: f.write(content)
                print(f"[✓] 已保存至: {path}")

        elif choice == '2':
            world_context = read_text_safe(os.path.join(DIRS["world"], "自动生成_世界观.txt"))
            if not world_context:
                print("未找到 `自动生成_世界观.txt`，请先运行工具 [1]。")
                continue
            sys_prompt = "你是一个专精于人物塑造的最佳畅销书作家。"
            user_prompt = f"""基于以下【世界观设定】，创作长篇小说的核心人物档案，共8人（3位核心主角+5位关键配角，含1位核心反派），要求人设鲜明、有记忆点。
【世界观设定】：
{world_context}

每个档案必须包含：姓名、年龄、外貌（含2处标志性特征）、身份（表层加隐藏）、性格（核心性格+小缺点+细节表现）、核心动机（短期+长期）、弱点、标志性特征（口头禅+习惯性动作）。
反派必须写清楚“为什么坏”，避免纯恶人设定。"""
            content = call_llm_tool(sys_prompt, user_prompt)
            if content:
                path = os.path.join(DIRS["chars"], "自动生成_角色组.txt")
                with open(path, "w", encoding="utf-8") as f: f.write(content)
                print(f"[✓] 已保存至: {path}")

        elif choice == '3':
            world_context = read_text_safe(os.path.join(DIRS["world"], "自动生成_世界观.txt"))
            chars_context = read_text_safe(os.path.join(DIRS["chars"], "自动生成_角色组.txt"))
            sys_prompt = "你是一位擅长把控长篇小说节奏的白金大作手。"
            user_prompt = f"""基于以下设定，创作长篇小说全书大纲，共3卷，每卷20章，总60章。每章要求有核心事件、爽点/情绪点、结尾钩子，伏笔连贯，节奏合理。
请在每卷中间插入一次小高潮，每卷结尾留一个大钩子。

格式要求：第一卷【卷名】-> 卷核心目标 -> 第1章：核心事件 + 爽点 + 结尾钩子 ...

【世界观】：{world_context}
【人物】：{chars_context}"""
            content = call_llm_tool(sys_prompt, user_prompt)
            if content:
                path = os.path.join(DIRS["plot"], "自动生成_全书大纲.txt")
                with open(path, "w", encoding="utf-8") as f: f.write(content)
                print(f"[✓] 已保存至: {path}")

        elif choice == '4':
            vol, _, _, latest_chap, latest_filepath = get_latest_chapter_info()
            if not latest_filepath or not os.path.exists(latest_filepath):
                print("[错误] 未找到任何已写章节！")
                continue
            text_to_polish = read_text_safe(latest_filepath)
            sys_prompt = "你是顶级文学编辑，专门去除文字中的机器生成感和翻译腔。"
            user_prompt = f"""请润色以下小说片段，核心目标是**去AI味**、增强文笔：
· 删除AI模板化套话，替换为细节描写
· 优化语言节奏
· 每300字左右新增1处五感描写或人物习惯性动作
· 保留核心情节
· 修正语句不通、逻辑不畅的地方

【原始待润色文本】：
{text_to_polish}"""
            content = call_llm_tool(sys_prompt, user_prompt)
            if content:
                path = latest_filepath.replace(".txt", "_已润色.txt")
                with open(path, "w", encoding="utf-8") as f: f.write(content)
                print(f"[✓] 润色完成！已另存为: {path}")

        elif choice == '5':
            vol, _, _, latest_chap, latest_filepath = get_latest_chapter_info()
            if not latest_filepath: continue
            text = read_text_safe(latest_filepath)
            outline = read_text_safe(os.path.join(DIRS["plot"], "当前卷大纲.txt"))
            sys_prompt = "你是极为严苛的剧情逻辑审查员。"
            user_prompt = f"""请基于【大纲】，分析【当前最新章节】的内容，重点检查：
1. 逻辑漏洞（与前文冲突）
2. 人设OOC（主角言行是否符合网文爽感和性格）
3. 节奏与冲突（是否太慢或太突兀）
给出具体问题和可直接修改的方案。

【大纲】：{outline}
【当前章节内容】：{text}"""
            call_llm_tool(sys_prompt, user_prompt)

        elif choice == '6':
            sys_prompt = "你是网文救火队长，专门帮作者突破写作瓶颈。"
            user_issue = input("\n请详细说明你目前的卡文场景（现在的剧情局势、主角状态等）：\n> ")
            if not user_issue: continue
            user_prompt = f"""当前卡文场景：{user_issue}
基于网文常见套路和反转技巧，生成 **5种合理的破局方案**。
要求：合理性、方案各不相同、必须标注合理性权重（1-5星）。"""
            content = call_llm_tool(sys_prompt, user_prompt)
            # No specific file saving for this tool, just print output
            # if content:
            #     print(content) # The call_llm_tool already prints content

        elif choice == '7':
            vol, _, _, latest_chap, latest_filepath = get_latest_chapter_info()
            if not latest_filepath:
                print("[错误] 未找到任何已写章节！")
                continue
            text = read_text_safe(latest_filepath)
            old_memo = read_text_safe(os.path.join(DIRS["plot"], "全局备忘录.txt"))
            
            sys_prompt = "你是长篇小说的“超级记忆压缩机与切片系统”。"
            user_prompt = f"""我们将延续此前的剧情。为了防止20万字以后的 AI 失忆（长文本衰减），请你执行绝对结构化的“短期记忆切片”。
要求：用不超过 600 字总结【旧记忆】+【最新一章发生的事】，并**严格格式化**输出。

必须包含且仅包含以下结构（以此作为之后生成的绝对坐标）：
【核心坐标状态】
- 时间：(例如：深夜 / 选拔前夕 / 宗门大战三年后)
- 地点：(当前确切的物理空间)
- 主角即时状态：(健康/重伤缺蓝/刚获得神器/心情暴怒等)
- 其他关键人物状态：(只有在场的人需要描写)

【近期事件纪要】
- 过去三章确认发生的事实和达成的交易/死亡/突破。

【下一步主线导向】
- 当前的首要短期目标与三个未解悬念。

【旧记忆参考】：
{old_memo if old_memo else "暂无。"}

【最新章节内容提取素材】：
{text}"""
            content = call_llm_tool(sys_prompt, user_prompt)
            if content:
                path = os.path.join(DIRS["plot"], "全局备忘录.txt")
                with open(path, "w", encoding="utf-8") as f: f.write(content)
                print(f"\n[✓] 记忆已压缩并更新！保存在：{path}")
                print(f"（系统已自动在底层的 System Prompt 中接入此备忘录，以后的每次生成 AI 都会‘记得’这些核心内容。）")

        elif choice == '8':
            vol, _, _, latest_chap, latest_filepath = get_latest_chapter_info()
            if not latest_filepath: continue
            text = read_text_safe(latest_filepath)
            old_grid = read_text_safe(os.path.join(DIRS["plot"], "伏笔与因果追踪表.txt"))
            
            sys_prompt = "你是逻辑极其严密的剧本审查师。"
            user_prompt = f"""请分析【最新一章内容】，找出所有作者有意或无意埋下的“坑”（伏笔、承诺、未解释的异象、奇怪的NPC等）。
请将它们追加到【现有的伏笔表格】中，并输出一张完整的、更新后的《伏笔登记表》。
表格需包含以下字段：
· 伏笔物项/事件
· 首次出现章节（目前是第 {latest_chap} 章）
· 关联人物/动作
· 尚未揭晓的悬念点
· 预期回收条件

【现有旧伏笔表】：
{old_grid if old_grid else "暂无。"}

【最新一章内容】：
{text}"""
            content = call_llm_tool(sys_prompt, user_prompt)
            if content:
                path = os.path.join(DIRS["plot"], "伏笔与因果追踪表.txt")
                with open(path, "w", encoding="utf-8") as f: f.write(content)
                print(f"\n[✓] 伏笔追踪表已更新！保存在：{path}")
                print(f"（系统已将此表融入底层逻辑，AI 在未来的续写中一旦触发回收条件，会自动帮你填坑！）")

        elif choice == '9':
            print("\n【拆解大师】支持输入一本他人小说的 txt 文件，逆向提取其中的世界观设定。")
            filepath = input("请输入参考小说的 txt 文件绝对路径（例如: C:\\Temp\\参考书.txt）：\n> ").strip()
            # 移除路径两端可能带有的引号（Windows拖拽文件会自动加双引号）
            filepath = filepath.strip("\"'").strip()
            
            if not filepath or not os.path.exists(filepath):
                print(f"[错误] 文件不存在或路径错误。")
                continue
            
            text = read_text_safe(filepath)
            if not text:
                print(f"[错误] 读取文件失败或文件为空。")
                continue
                
            # 截断文本，避免过长导致 Token 超限或账单爆表
            MAX_EXTRACT_CHARS = 20000
            if len(text) > MAX_EXTRACT_CHARS:
                print(f"[提示] 该小说较长（{len(text)} 字），系统将截取前 {MAX_EXTRACT_CHARS} 字提取核心设定。")
                text = text[:MAX_EXTRACT_CHARS]
            else:
                print(f"[提示] 正在拆解全文（共 {len(text)} 字）...")
                
            sys_prompt = "你是一个顶级的网文主编和设定拆解专家。只输出提取的内容，不要有任何多余的寒暄和解释步骤。"
            user_prompt = f"""请仔细阅读以下提供的小说开头/片段，从中强力反推并提取出这本小说的核心设定。
请严格按照以下几个维度输出：
1. 【力量/职业体系】：（例如：修仙境界划分、异能等级、机甲型号、经济体系等）
2. 【世界观与核心规则】：（例如：位面构成、宗门/家族势力、基本法则、地理特征）
3. 【男女主角及配角档案】：（姓名、特征、金手指属性、核心性格）
4. 【特殊设定/名词释义】：（小说中特有的专有名词、货币、特有组织）

注意：如果文本中只写了炼气、筑基，没有提到后续境界，请基于网文常识合理推演补全它的后续境界。如果某项设定正文完全没有，请标明“暂未出现”。

【小说文本开始】：
{text}
【小说文本结束】"""
            
            content = call_llm_tool(sys_prompt, user_prompt)
            if content:
                # 提取原文件名作为保存的标识
                basename = os.path.basename(filepath)
                name_without_ext = os.path.splitext(basename)[0]
                save_path = os.path.join(DIRS["world"], f"参考解析_{name_without_ext}.txt")
                
                with open(save_path, "w", encoding="utf-8") as f:
                    f.write(content)
                print(f"\n[✓] 解析完工！已将设定保存至你的世界观库中：{save_path}")
                print(f"如果你想在下一章中模仿或使用这套设定，只要在主界面选择“写新章”时，把它的编号打上钩就行了！")

def write_menu():
    """连载写作主界面的单章生成系统 Prompt 增强"""
    pass # 核心功能已经在主流程中实现，这里只是做一个结构占位

def main_menu():
    init_demo_files()
    
    while True:
        print("\n" + "★《千章小说创作系统 v2.0 - 终极版》★".center(60))
        print("====== 🚀 核心连载写作 ======")
        print("1. 📝 开始写 [新的一章]")
        print("2. ✍️  继续写 [当前最新章] (追加续写)")
        print("====== 🛠️ 档案管理与 AI 工具 ======")
        print("3. 🗂️ 手动新增 人物/世界观/大纲")
        print("4. 🤖 进入【AI 灵感与工具箱】(设定生成/润色/防崩盘)")
        print("0. ⚙️  退出系统")
        print("-" * 62)
        
        choice = input("请输入选项(0-4): ").strip()
        
        if choice == '1':
            vol, chap_num, filepath, _, _ = get_latest_chapter_info()
            print(f"\n=> 准备创作：[第{vol}卷] - 第 {chap_num:04d} 章")
            
            system_prompt = build_system_prompt()
            single_chapter_rules = (
                "\n\n<chapter_quality_rules>\n"
                "【单章质量控制指令】(极度重要)：\n"
                "细节要求：加入至少3处五感描写、1段心理描写、1句标志性台词。\n"
                "本章需有2-3次情绪波动，至少1个小爽点。\n"
                "结尾强制设置1个悬念钩子(Hook)。字数控制在 2000 字左右。\n"
                "</chapter_quality_rules>\n"
            )
            system_prompt += single_chapter_rules
            
            while True:
                user_req = get_multiline_input(f"【第{chap_num}章】具体写作要求是什么？(事件流程必填，也可输入'exit'退出)")
                if not user_req or user_req.strip().lower() == 'exit': 
                    break
                    
                content = call_llm(system_prompt, f"<本章写作要求>\n{user_req}\n</本章写作要求>")
                if content:
                    print("\n" + "="*40)
                    print("📝 章节生成完毕，请确认：")
                    print("  1. 保存此章并返回菜单")
                    print("  2. 不满意，修改要求并重新生成")
                    print("  0. 放弃并返回主菜单")
                    action = input("请输入选项(0-2): ").strip()
                    
                    if action == '1':
                        with open(filepath, "w", encoding="utf-8") as f: f.write(content)
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        history_path = os.path.join(DIRS["hist"], f"Ch{chap_num}_{timestamp}.txt")
                        with open(history_path, "w", encoding="utf-8") as f: f.write(user_req + "\n\n" + "-"*40 + "\n\n" + content)
                        print(f"[✓] 本章已完稿！保存至：{filepath}")
                        # Auto-prompt to compress memo?
                        print("提示：你可以前往工具箱柜台使用 [7] 进行记忆压缩。")
                        input("按回车返回主菜单...")
                        break
                    elif action == '2':
                        print("\n==> 进入重新生成流程 <==")
                        continue
                    else:
                        print("已放弃保存。")
                        break
                else:
                    break
                
        elif choice == '2':
            vol, _, _, latest_chap, latest_filepath = get_latest_chapter_info()
            if not latest_filepath or not os.path.exists(latest_filepath):
                print("\n[错误] 当前没有任何已写的章节，无法续写！")
                continue
                
            existing_content = read_text_safe(latest_filepath)
            system_prompt = build_system_prompt()
            
            while True:
                user_req = get_multiline_input(f"【续写第{latest_chap}章】接下来的情节该怎么发展？(输入'exit'退出)")
                if not user_req or user_req.strip().lower() == 'exit': 
                    break
                    
                content = call_llm(system_prompt, f"<本章写作要求>\n{user_req}\n</本章写作要求>", existing_content=existing_content)
                if content:
                    print("\n" + "="*40)
                    print("📝 续写生成完毕，请确认：")
                    print("  1. 保存追加的内容并返回菜单")
                    print("  2. 不满意，修改要求并重新生成")
                    print("  0. 放弃并返回主菜单")
                    action = input("请输入选项(0-2): ").strip()
                    
                    if action == '1':
                        with open(latest_filepath, "a", encoding="utf-8") as f: f.write("\n\n" + content)
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        history_path = os.path.join(DIRS["hist"], f"Ch{latest_chap}_续写_{timestamp}.txt")
                        with open(history_path, "w", encoding="utf-8") as f: f.write(user_req + "\n\n" + "-"*40 + "\n\n" + content)
                        print(f"[✓] 续写完稿！已追加保存至：{latest_filepath}")
                        input("\n按回车返回主菜单...")
                        break
                    elif action == '2':
                        print("\n==> 进入重新生成流程 <==")
                        continue
                    else:
                        print("已放弃保存。")
                        break
                else:
                    break

        elif choice == '3':
            print("\n请直接前往 /characters, /world_building, /plot 目录用编辑器创建 .txt 文件即可。")
        elif choice == '4':
            toolbox_menu()
        elif choice == '0':
            print("再见，大作家！期待你的大作！")
            break
        else:
            print("无效输入！")

if __name__ == "__main__":
    main_menu()
