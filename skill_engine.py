# -*- coding: utf-8 -*-
"""
Skill 技能引擎 - 可插拔 AI 智能体角色模块系统
================================================
负责加载、解析、执行 skills/ 目录下的 JSON 技能文件。
每个 Skill 定义一个专属 AI 角色人设 + 专用 Prompt，支持：
  - 单技能执行
  - 链式执行（多个 Skill 依次执行，上一个输出作为下一个输入）
  - 用户自定义 Skill
"""

import os
import sys
import json
import glob
import re

# ============================================================
# 常量
# ============================================================
REQUIRED_FIELDS = {"id", "name", "system_prompt", "input_type", "output_type"}
VALID_INPUT_TYPES = {"editor_text", "latest_chapter", "user_input", "selected_text"}
VALID_OUTPUT_TYPES = {"replace_editor", "append_editor", "save_to_file", "popup"}
VALID_CATEGORIES = {"editing", "quality", "memory", "planning", "creative", "other"}

# ============================================================
# Skill 加载
# ============================================================
def get_skills_dir(base_dir=None):
    """获取 skills 目录路径"""
    if base_dir is None:
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, "skills")


def load_skill(filepath):
    """加载单个 Skill JSON 文件，返回 skill dict 或 None"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            skill = json.load(f)
        # 注入来源路径
        skill["_filepath"] = filepath
        return skill
    except Exception as e:
        print(f"[Skill引擎] 加载失败 {filepath}: {e}")
        return None


def load_all_skills(skills_dir=None):
    """
    扫描 skills/ 目录，加载所有合法的 Skill。
    返回 {skill_id: skill_dict} 字典。
    """
    if skills_dir is None:
        skills_dir = get_skills_dir()
    
    if not os.path.exists(skills_dir):
        os.makedirs(skills_dir, exist_ok=True)
        return {}
    
    skills = {}
    pattern = os.path.join(skills_dir, "*.json")
    for filepath in sorted(glob.glob(pattern)):
        skill = load_skill(filepath)
        if skill is None:
            continue
        errors = validate_skill(skill)
        if errors:
            print(f"[Skill引擎] 校验失败 {os.path.basename(filepath)}: {errors}")
            continue
        skills[skill["id"]] = skill
    
    return skills


def load_skills_by_category(skills_dir=None):
    """按 category 分组加载所有 Skill，返回 {category: [skill1, skill2, ...]}"""
    all_skills = load_all_skills(skills_dir)
    grouped = {}
    for skill in all_skills.values():
        cat = skill.get("category", "other")
        if cat not in grouped:
            grouped[cat] = []
        grouped[cat].append(skill)
    return grouped


# ============================================================
# Skill 校验
# ============================================================
def validate_skill(skill):
    """
    校验 Skill JSON 结构是否合法。
    返回错误列表（空列表表示通过）。
    """
    errors = []
    
    if not isinstance(skill, dict):
        return ["Skill 必须是一个 JSON 对象"]
    
    # 必填字段检查
    missing = REQUIRED_FIELDS - set(skill.keys())
    if missing:
        errors.append(f"缺少必填字段: {missing}")
    
    # 枚举值检查
    if skill.get("input_type") and skill["input_type"] not in VALID_INPUT_TYPES:
        errors.append(f"input_type 无效: {skill['input_type']}，合法值: {VALID_INPUT_TYPES}")
    
    if skill.get("output_type") and skill["output_type"] not in VALID_OUTPUT_TYPES:
        errors.append(f"output_type 无效: {skill['output_type']}，合法值: {VALID_OUTPUT_TYPES}")
    
    if skill.get("category") and skill["category"] not in VALID_CATEGORIES:
        errors.append(f"category 无效: {skill['category']}，合法值: {VALID_CATEGORIES}")
    
    # temperature 范围检查
    temp = skill.get("temperature")
    if temp is not None and (not isinstance(temp, (int, float)) or temp < 0 or temp > 2.0):
        errors.append(f"temperature 超出范围 [0, 2.0]: {temp}")
    
    return errors


# ============================================================
# Skill 执行
# ============================================================
def build_skill_prompt(skill, input_text, extra_context=None):
    """
    根据 Skill 定义构建完整的 system_prompt 和 user_prompt。
    
    Args:
        skill: Skill 字典
        input_text: 输入文本（根据 input_type 获取的内容）
        extra_context: 额外上下文（如知识库内容）
    
    Returns:
        (system_prompt, user_prompt) 元组
    """
    # 构建 System Prompt：角色人设 + Skill 专用指令
    sys_parts = []
    
    role_persona = skill.get("role_persona", "")
    if role_persona:
        sys_parts.append(role_persona)
    
    sys_parts.append(skill["system_prompt"])
    
    system_prompt = "\n\n".join(sys_parts)
    
    # 构建 User Prompt
    user_parts = []
    
    # 处理模板变量替换（使用占位符避免 input 中包含 {xxx} 时破坏模板）
    user_content = skill.get("user_prompt_template", "")
    if user_content:
        # 模板模式：由模板控制上下文的插入位置
        input_placeholder = "__INPUT_TEXT_PLACEHOLDER__"
        context_placeholder = "__EXTRA_CONTEXT_PLACEHOLDER__"
        user_content = user_content.replace("{input_text}", input_placeholder)
        user_content = user_content.replace("{extra_context}", context_placeholder)
        # 替换后用实际内容填充，确保不会被 input 中可能的 { 干扰
        safe_input = input_text
        safe_context = extra_context if extra_context else "（暂无）"
        user_content = user_content.replace(input_placeholder, safe_input)
        user_content = user_content.replace(context_placeholder, safe_context)
        user_parts.append(user_content)
    else:
        # 无模板模式：自动拼接上下文 + 输入
        if extra_context:
            user_parts.append(f"【参考上下文】：\n{extra_context}")
        user_parts.append(input_text)
    
    user_prompt = "\n\n".join(user_parts)
    
    return system_prompt, user_prompt


def execute_skill(skill, input_text, llm_call_fn, extra_context=None):
    """
    执行单个 Skill。
    
    Args:
        skill: Skill 字典
        input_text: 输入文本
        llm_call_fn: LLM 调用函数，签名 fn(system_prompt, user_prompt, temperature) -> str
        extra_context: 额外上下文
    
    Returns:
        生成的文本结果
    """
    system_prompt, user_prompt = build_skill_prompt(skill, input_text, extra_context)
    temperature = skill.get("temperature", 0.5)
    
    result = llm_call_fn(system_prompt, user_prompt, temperature)
    return result


def execute_chain(skill_list, initial_input, llm_call_fn, extra_context=None, 
                  progress_callback=None):
    """
    链式执行多个 Skill：上一个输出作为下一个输入。
    
    Args:
        skill_list: Skill 列表（按执行顺序）
        initial_input: 初始输入文本
        llm_call_fn: LLM 调用函数
        extra_context: 额外上下文（共享给所有 Skill）
        progress_callback: 进度回调 fn(step_index, total, skill_name, status)
    
    Returns:
        最终输出文本
    """
    current_text = initial_input
    total = len(skill_list)
    
    for i, skill in enumerate(skill_list):
        if progress_callback:
            progress_callback(i, total, skill.get("name", "未命名"), "执行中")
        
        current_text = execute_skill(skill, current_text, llm_call_fn, extra_context)
        
        if progress_callback:
            progress_callback(i, total, skill.get("name", "未命名"), "完成")
    
    return current_text


# ============================================================
# Skill 创建与保存
# ============================================================
def create_skill_template():
    """返回一个空白的 Skill 模板字典，方便用户填充"""
    return {
        "id": "my_custom_skill",
        "name": "我的自定义技能",
        "icon": "🔧",
        "category": "other",
        "description": "在这里描述你的技能做什么",
        "role_persona": "你是一个专业的...",
        "system_prompt": "请对以下文本执行...",
        "input_type": "editor_text",
        "output_type": "replace_editor",
        "temperature": 0.5,
        "requires_context": False,
        "user_input_prompt": None,
        "tags": []
    }


def save_skill(skill, skills_dir=None):
    """
    保存 Skill 到 JSON 文件。
    文件名使用 skill["id"] + .json。
    """
    if skills_dir is None:
        skills_dir = get_skills_dir()
    os.makedirs(skills_dir, exist_ok=True)
    
    # 格式校验
    errors = validate_skill(skill)
    if errors:
        raise ValueError(f"Skill 格式错误: {errors}")
    
    # 清理内部字段
    save_data = {k: v for k, v in skill.items() if not k.startswith("_")}
    
    filename = f"{skill['id']}.json"
    filepath = os.path.join(skills_dir, filename)
    
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2)
    
    return filepath


def delete_skill(skill_id, skills_dir=None):
    """删除指定 Skill 文件"""
    if skills_dir is None:
        skills_dir = get_skills_dir()
    filepath = os.path.join(skills_dir, f"{skill_id}.json")
    if os.path.exists(filepath):
        os.remove(filepath)
        return True
    return False


# ============================================================
# 工具函数
# ============================================================
CATEGORY_LABELS = {
    "editing": "📝 润色与编辑",
    "quality": "🔍 质检与校对",
    "memory": "🧠 记忆管理",
    "planning": "📋 大纲与规划",
    "creative": "💡 创意与灵感",
    "other": "🔧 其他",
}

CATEGORY_ORDER = ["editing", "quality", "memory", "planning", "creative", "other"]


def get_category_label(category):
    """获取分类的中文标签"""
    return CATEGORY_LABELS.get(category, f"🔧 {category}")


def get_skill_display_text(skill):
    """获取 Skill 的显示文本（图标 + 名称）"""
    icon = skill.get("icon", "🔧")
    name = skill.get("name", "未命名")
    return f"{icon} {name}"


# ============================================================
# 测试入口
# ============================================================
if __name__ == "__main__":
    print("=" * 50)
    print(" Skill 引擎自检 ".center(48))
    print("=" * 50)
    
    skills_dir = get_skills_dir()
    print(f"\nSkills 目录: {skills_dir}")
    
    all_skills = load_all_skills()
    print(f"已加载 {len(all_skills)} 个技能：")
    for sid, skill in all_skills.items():
        icon = skill.get("icon", "🔧")
        name = skill.get("name", "?")
        cat = skill.get("category", "other")
        desc = skill.get("description", "")
        print(f"  {icon} {name} [{cat}] - {desc}")
    
    print("\n✅ Skill 引擎自检通过！")
