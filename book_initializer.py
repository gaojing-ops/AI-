# -*- coding: utf-8 -*-
"""Prompt and parsing helpers for automatic book initialization."""

import json
import re


SECTION_MARKERS = {
    "world": "WORLD",
    "characters": "CHARACTERS",
    "outline": "MASTER_OUTLINE",
    "tone": "TONE_RULES",
    "canon": "CANON",
    "reveal_rules": "REVEAL_RULES",
}


def build_initialization_prompt(title, genre, total_chapters, volume_ranges, existing_outline=""):
    volume_lines = []
    for item in volume_ranges or []:
        try:
            start, end, name = item[0], item[1], item[2]
            volume_lines.append(f"- {name}: 第{start}-{end}章")
        except Exception:
            continue
    volume_text = "\n".join(volume_lines) if volume_lines else "未配置"

    system_prompt = (
        "你是长篇网文开书总架构师。请根据书名、题材和目标章数，生成可直接给托管写作使用的"
        "初始世界观、核心角色、全书大纲、基调铁律、唯一真相设定和真相揭示规则。要求具体、可执行、少空话。"
    )
    user_prompt = f"""请为新书生成初始化资料。

【书名】《{title}》
【题材】{genre or "通用"}
【目标总章数】{total_chapters}
【卷范围】
{volume_text}

【用户已有全书大纲/方向】
{existing_outline or "暂无，允许你基于题材补出完整方向。"}

【输出硬格式】
必须严格使用以下分隔符，不要更改英文标记：

<<<WORLD>>>
写世界观设定。必须包含：基础规则、核心冲突、主要地点/组织、力量/资源/制度、限制与代价、禁止临时新增的设定。

<<<CHARACTERS>>>
写核心人物组档案。至少包含：主角、2-3位关键同伴/对手、1位阶段反派、1位长期反派或压力源。
每个角色必须包含：姓名、身份、表层目标、内在缺口、关系、当前状态、行为习惯、说话辨识度、人设底线、禁止写崩点。主要角色必须能仅凭动作或对白被读者区分，禁止所有人共用同一种冷脸、沉默和狠话。

<<<MASTER_OUTLINE>>>
写全书大纲。按卷输出，每卷包含：卷主线、阶段目标、关键冲突、卷末落点。
再给前10章逐章方向。每章都必须使用以下十四字段格式，不能写成“1.”编号：
第1章 标题：
章节功能：从冲突推进/调查发现/兑现回收/关系转折/代价余波/铺垫蓄力/高潮决断中选一项
核心事件：...
出场人物：...
承接锚点：第1章写开局起点，后续章节写必须承接的上一章结果
人物选择与代价：写清谁在什么两难中做了什么选择，并立即或延迟支付什么代价
反方行动：对手、制度、环境或利益方本章主动做了什么；没有人物反派时也不能写“无”
冲突/爽点：...
兑现类型：从能力/知识/关系/身份/资源/情绪/谜底/失败代价中选一项
伏笔/禁出内容：...
状态落点：写清主角境界/能力、地点、伤势、关键物品去向
不可逆变化：本章结束后不能回档的事件或关系变化
结尾类型：从新问题/代价落地/关系变化/阶段兑现/信息反差/危机逼近/情绪余震中选一项
章末钩子：...
下一章承接关键词：用2-4个具体人名、地点、物品或行动词

<<<TONE_RULES>>>
写基调铁律。包含文风、节奏、爽点、禁用套路、平台风险规避。

<<<CANON>>>
写唯一真相设定表。区分：客观真相、角色误判、禁止提前揭示项、最早揭示章节建议。

<<<REVEAL_RULES>>>
输出合法 JSON，不要 Markdown，不要解释。格式如下：
{{"strict_mode":true,"topics":[{{"id":"topic_id","label":"主题名","earliest_hint":1,"earliest_soft":30,"earliest_hard":80,"max_level_before_hint":1,"max_level_before_soft":2,"max_level_before_hard":3,"hard_patterns":["简单中文关键词"],"sanitize_terms":{{"提前坐实词":"阶段性模糊说法"}},"allowed_examples":["允许写法"],"forbidden_examples":["禁止提前写法"]}}]}}
至少生成2个主题，用于限制系统来源、幕后黑手、世界真相、主线谜底等不能提前坐实的内容。strict_mode 必须为 true。

要求：
1. 所有内容都要服务于后续自动托管，不能只写概念。
2. 不要引用已有热门作品名称。
3. 角色名和设定名要具体，但不要堆太多专有名词。
4. 如果是悬疑/无限流/系统/修仙等题材，必须写清规则边界和代价。
"""
    return system_prompt, user_prompt


def build_story_bible_prompt(title, genre, total_chapters, volume_ranges, master_outline, canon_text):
    volume_lines = []
    for item in volume_ranges or []:
        try:
            volume_lines.append(f"- {item[2]}: 第{int(item[0])}-{int(item[1])}章")
        except Exception:
            continue
    system_prompt = (
        "你是长篇网文结构总监。请把已有大纲转换为机器可校验的故事圣经。"
        "只输出一个合法 JSON 对象，不要 Markdown，不要解释。"
    )
    user_prompt = f"""【书名】《{title}》
【题材】{genre or "通用"}
【总章数】{total_chapters}
【分卷范围】
{chr(10).join(volume_lines)}

【已有全书大纲】
{(master_outline or "")[:16000]}

【唯一真相设定】
{(canon_text or "")[:8000]}

输出 JSON 必须含以下字段：
{{
  "schema_version": 2,
  "book_title": "{title}",
  "premise": "一句具体的核心前提",
  "core_promise": "书名卖点如何贯穿全书",
  "system_rules": ["3-6条能力、资源或世界规则边界"],
  "volume_contracts": [
    {{"start":1,"end":50,"name":"第一卷","goal":"本卷行动目标","promise_progress":"卖点推进","power_boundary":"力量/能力边界","climax":"卷末不可逆高潮","antagonist_strategy":"反方主动策略及调整方式","relationship_turns":["由人物行动造成的关系转折"],"value_choice_contract":"反复考验人物价值观的选择与代价","environment_constraints":["实际改变行动的职业/制度/地理/资源限制"],"payoff_mix":["能力","知识","关系","失败代价"],"pacing_guard":"防止相同章节功能连续复制的规则","avoid":["禁止重复项"]}}
  ],
  "arc_contracts": [
    {{"start":1,"end":25,"goal":"阶段行动目标","promise_progress":"本阶段新兑现","power_boundary":"上限与代价","climax":"阶段落点","antagonist_strategy":"本阶段反方主动策略","relationship_turns":["本阶段关系转折"],"value_choice_contract":"本阶段人物选择及代价","environment_constraints":["本阶段环境限制"],"payoff_mix":["知识","关系","能力"],"pacing_guard":"本阶段节拍防重复规则","avoid":["禁止循环"]}}
  ],
  "hard_gates": [
    {{"before":80,"terms":["80章前禁止坐实的真相短语"]}}
  ]
}}

硬要求：
1. volume_contracts 必须严格覆盖第1章到第{total_chapters}章，边界与给定分卷范围完全一致。
2. arc_contracts 必须连续覆盖第1章到第{total_chapters}章，每段建议20-70章，不得重叠或留空。
3. goal 和 climax 必须是可发生、可验证的事件，不能写“继续成长”“面对挑战”。
4. power_boundary 必须防止无铺垫升级；avoid 必须防止重复地图、重复反派和重复高潮。
5. hard_gates 只放真正不能提前坐实的核心真相。
6. 每个分卷和阶段必须写清反方行动、关系转折、人物选择、环境限制、兑现组合和节拍防重复；这些字段不能写成空泛的“加强冲突”。
"""
    return system_prompt, user_prompt


def build_semantic_invariants_prompt(
    title, genre, world_text, characters_text, canon_text
):
    """Compile prose lore into deterministic, fail-closed regex contracts."""
    system_prompt = (
        "你是长篇小说事实合同工程师。把人物与唯一真相编译成机器可执行的语义一致性 JSON。"
        "只输出一个合法 JSON 对象，不要 Markdown，不要解释。正则必须是 Python re 可编译的简单表达式。"
    )
    user_prompt = f"""【书名】《{title}》
【题材】{genre or '通用'}

【世界规则】
{(world_text or '')[:7000]}

【人物档案】
{(characters_text or '')[:7000]}

【唯一真相】
{(canon_text or '')[:10000]}

输出 JSON 必须且只能以这四个顶层数组为核心：
{{
  "identity_bindings": [
    {{"id":"稳定英文id","entity":"人物或代号","claim_patterns":["能捕获身份断言且必须含(?P<value>...)的正则"],"allowed_values":["唯一允许值"],"severity":"critical","message":"冲突说明"}}
  ],
  "exclusive_fact_groups": [
    {{"id":"事实键","facts":[{{"id":"事实值id","label":"事实值","patterns":["正文坐实该值的正则"]}}],"max_active":1,"severity":"critical","message":"互斥事实说明"}}
  ],
  "forbidden_patterns": [
    {{"id":"禁用项id","patterns":["明确错误事实或编辑层元话语正则"],"severity":"error","message":"为什么禁止"}}
  ],
  "numeric_rules": [
    {{"id":"数值id","patterns":["必须含(?P<value>\\d+(?:\\.\\d+)?)的正则"],"min":0,"max":100,"unit":"单位","severity":"error","message":"数值边界"}}
  ]
}}

硬要求：
1. 身份谜底、死因、关键物品归属、能力本质等只能有一个客观答案的内容，必须进入 identity_bindings 或 exclusive_fact_groups。
   会在后文复盘的关键证据口径也必须覆盖：时间、时长、数量、证据介质，以及删除/保全状态；不得只登记宏观谜底而漏掉可核验细节。
2. claim_patterns 的每条正则都必须含命名捕获组 (?P<value>...)；numeric_rules 同理。
3. 只把正文明确坐实的表达设为事实命中，不要让“怀疑、排除、不是、尚未确认”误触正确事实。
4. forbidden_patterns 至少覆盖：把章纲/审稿/读者/作者意图写进正文、资料中明确禁止的伪科学或错误设定、提前坐实的谜底。
5. numeric_rules 只为资料中确有硬边界的数值建立规则；没有可靠边界就留空数组，不得编造医学参数。
6. 所有 id 唯一；severity 只能是 info/warning/error/critical；禁止使用回溯灾难型正则。
7. 即使某一类没有规则也必须保留空数组，四个顶层字段一个都不能少。
"""
    return system_prompt, user_prompt


def build_opening_outline_prompt(title, genre, book_brief, world_text, characters_text, master_outline):
    """Build a focused prompt for the commercial opening when the broad initializer truncates."""
    system_prompt = (
        "你是商业长篇网文的开篇主编。只设计前10章逐章合同，不写正文，不复述全书设定。"
        "前三章必须兑现书名与简介承诺，后七章必须把开篇冲突扩成第一个可验证的因果闭环。"
    )
    user_prompt = f"""【书名】《{title}》
【题材】{genre}

【核心方向】
{(book_brief or '')[:6000]}

【世界规则】
{(world_text or '')[:6000]}

【核心人物】
{(characters_text or '')[:6000]}

【卷级方向】
{(master_outline or '')[:6000]}

严格输出第1章到第10章，每章必须逐行包含以下十四个字段：
第N章 标题：
章节功能：
核心事件：
出场人物：
承接锚点：
人物选择与代价：
反方行动：
冲突/爽点：
兑现类型：
伏笔/禁出内容：
状态落点：
不可逆变化：
结尾类型：
章末钩子：
下一章承接关键词：

硬要求：
1. 第1章前800字内必须出现项目简介承诺的核心异常、危机或人物困境；用具体行动展示，不用背景说明代替剧情。
2. 第2章必须让主角通过行动验证至少一条关键规则、资源边界或现实阻力，禁止站桩讲设定。
3. 第3章必须第一次兑现书名/简介卖点，并让现实、关系、身份或目标发生不可逆变化，同时抛出更大的具体问题。
4. 第4-10章围绕同一条因果链继续推进，不能立刻换地图、换反派或重开任务；第10章形成第一次阶段胜利并支付已经约定的代价。
5. 每章至少推进一个外部行动和一个关系变化；钩子必须是具体人物、物件、倒计时或事件，不得用空泛感叹。
6. 不准新增项目资料未授权的系统、能力、组织、资源、万能解释者或临时幕后黑手。
7. 状态落点必须写清地点、身体/心理状态、持有物、已验证规则和仍属猜测的内容，供自动托管承接。
8. 遵守番茄发布规范：不写低俗色情、未成年人负面导向、极端血腥、攻击引战、站外导流；不得用重复段落、无关信息或改名换皮恶意水文。
9. 连续三章不得使用相同章节功能、兑现类型或结尾类型；过渡章可以用代价余波、关系变化或情绪余震收章，不强行制造突发危机。
"""
    return system_prompt, user_prompt


def build_guardrails_prompt(title, genre, book_brief, world_text, characters_text, master_outline):
    """Build the missing tone/canon/reveal artifacts in a compact second pass."""
    system_prompt = (
        "你是长篇网文的连续性总监。只补齐写作铁律、唯一真相和分级揭示规则，"
        "确保自动写作不会把猜测提前写成答案。"
    )
    user_prompt = f"""【书名】《{title}》
【题材】{genre}
【核心方向】{(book_brief or '')[:5000]}
【世界规则】{(world_text or '')[:5000]}
【核心人物】{(characters_text or '')[:4000]}
【卷级方向】{(master_outline or '')[:5000]}

必须严格使用以下标记：

<<<TONE_RULES>>>
给出可执行的基调铁律：叙事视角、节奏、悬疑公平性、爽点兑现、人物对白区分、反AI腔、平台风险和绝对禁用套路。

<<<CANON>>>
区分并逐条写清：客观真相、开书时各角色所知、角色误判、首个阶段闭环的真实因果、全书谜底、不可更改规则、禁止提前揭示项与最早揭示区间。不得把猜测混入客观真相。

<<<REVEAL_RULES>>>
只输出合法 JSON：
{{"strict_mode":true,"topics":[{{"id":"id","label":"主题","earliest_hint":1,"earliest_soft":30,"earliest_hard":80,"max_level_before_hint":1,"max_level_before_soft":2,"max_level_before_hard":3,"hard_patterns":["简单中文关键词"],"sanitize_terms":{{"提前坐实词":"阶段性模糊说法"}},"allowed_examples":["允许写法"],"forbidden_examples":["禁止提前写法"]}}]}}
生成3-6个主题，覆盖项目资料里最重要的身份谜底、幕后机制、世界真相或结局答案。关键词必须可直接匹配，不使用复杂正则。
"""
    return system_prompt, user_prompt


def build_reveal_rules_prompt(title, book_brief, canon_text):
    """Generate compact reveal gates without repeating the full guardrail document."""
    system_prompt = (
        "你是长篇悬疑小说的真相门控工程师。只输出合法 JSON 对象，不要 Markdown，不要解释。"
    )
    user_prompt = f"""【书名】《{title}》
【开书方向】{(book_brief or '')[:4500]}
【唯一真相】{(canon_text or '')[:9000]}

输出格式：
{{"strict_mode":true,"topics":[{{"id":"id","label":"主题","earliest_hint":1,"earliest_soft":30,"earliest_hard":80,"max_level_before_hint":1,"max_level_before_soft":2,"max_level_before_hard":3,"hard_patterns":["简单中文关键词"],"sanitize_terms":{{"提前坐实词":"阶段性模糊说法"}},"allowed_examples":["允许写法"],"forbidden_examples":["禁止提前写法"]}}]}}

要求：
1. 从项目简介和唯一真相中选择3-6个最需要延迟揭示的主题，不得使用固定题材模板替代项目事实。
2. 每个主题 hard_patterns 1-3条、sanitize_terms 0-3组、allowed_examples 1条、forbidden_examples 1条；总输出控制在3000字以内。
3. 已在黄金三章明确承诺公开的钩子不能被当成禁词；真正延迟的是钩子背后的完整答案。
4. 只用简单中文关键词，不写章号前缀，不使用复杂正则转义。
5. hard 等级必须晚于 soft，soft 不得早于 hint；替换词只能降低确定性，不能编造新事实。
"""
    return system_prompt, user_prompt


def build_opening_revision_prompt(title, book_brief, world_text, canon_text, opening_outline):
    """Ask the review model to reconcile and tighten the golden-three contract."""
    system_prompt = (
        "你是商业网文的黄金三章总编兼连续性修订师。你必须直接给出可替换的三章合同，"
        "既要提高点击与追读，也要消除规则冲突；不要写审稿说明，不要写正文。"
    )
    user_prompt = f"""【书名】《{title}》
【核心方向】{(book_brief or '')[:6000]}
【世界规则】{(world_text or '')[:6500]}
【唯一真相】{(canon_text or '')[:7000]}

【待修订的前三章合同】
{(opening_outline or '')[:18000]}

请整体重写第1-3章合同，并遵守以下口径：
1. 项目简介、世界规则和唯一真相是事实来源；待修订细纲与它们冲突时必须修正，不能自行发明第三种答案。
2. 第1章前800字内落地书名/简介承诺的核心异常、冲突或人物困境；把人物职业/身份与钩子合并，删除多余匿名人、巧合和重复道具。
3. 第2章通过行动验证关键规则、资源边界或现实阻力；不能用说明书、系统面板或万能角色直接解释。
4. 第3章第一次兑现核心卖点，完成一个小闭环，同时造成不可逆变化并抛出更大的具体威胁。
5. 前三章只公开项目简介明确承诺的钩子，不提前坐实唯一真相中的身份谜底、幕后机制和终局答案。
6. 每章只保留一条主行动线；角色解决问题必须依靠已经设定的职业、能力、关系和资源，并支付既定代价。
7. 状态落点必须能直接交给下一章：时间、地点、身体/心理状态、持有物、已验证事实与仍属猜测的信息分开写。
8. 遵守番茄发布规范；不得靠极端血腥、低俗色情、攻击引战、站外导流、重复段落或无关信息制造刺激和字数。

严格只输出第1章、第2章、第3章。每章逐行包含：
第N章 标题：
章节功能：
核心事件：
出场人物：
承接锚点：
人物选择与代价：
反方行动：
冲突/爽点：
兑现类型：
伏笔/禁出内容：
状态落点：
不可逆变化：
结尾类型：
章末钩子：
下一章承接关键词：
"""
    return system_prompt, user_prompt


def parse_json_object(text):
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            data = json.loads(raw[start:end + 1])
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def infer_protagonist(characters_text):
    text = characters_text or ""
    patterns = (
        r"(?:主角|主人公)[^\n]{0,20}(?:姓名)?[：:]\s*([\u4e00-\u9fff]{2,4})",
        r"姓名[：:]\s*([\u4e00-\u9fff]{2,4})",
        r"^#{0,3}\s*主角[：:\s]+([\u4e00-\u9fff]{2,4})",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.MULTILINE)
        if match:
            return match.group(1)
    return "主角"


def parse_initialization_output(text):
    text = text or ""
    sections = {}
    marker_pattern = re.compile(r"<<<([A-Z_]+)>>>\s*", re.MULTILINE)
    matches = list(marker_pattern.finditer(text))
    if not matches:
        return sections

    for idx, match in enumerate(matches):
        name = match.group(1)
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        for key, marker in SECTION_MARKERS.items():
            if name == marker:
                sections[key] = body
                break
    return sections


def has_meaningful_content(text):
    text = (text or "").strip()
    if not text:
        return False
    hard_placeholders = (
        "请填写",
        "待填写",
        "标题待定",
        "示例主题",
        "每个主要角色一个",
        "每个重要系统",
    )
    compact = re.sub(r"\s+", "", text)
    if len(compact) < 80:
        return False
    if any(word in text for word in hard_placeholders):
        return False
    return True
