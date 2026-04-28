# 千章小说创作系统 — AI 长篇小说写作工具

基于 DeepSeek API 的模块化上下文长篇小说创作系统，支持 GUI 一键托管批量生成和 CLI 命令行两种模式。内置一致性检查、记忆压缩、伏笔追踪、跨章扫描等多层防护，防止超长篇写作中的设定遗忘和剧情崩盘。

## 功能概览

- **GUI 桌面版**（`gui_app.py`）：一键托管批量生成、续写、记忆压缩、伏笔追踪、健康检查、实体追踪面板
- **CLI 命令行版**（`generator.py`）：菜单式交互，支持单章生成、续写、AI 工具箱
- **AI 工具箱**：世界观生成、角色生成、大纲生成、章节润色、卡文破局、记忆压缩、伏笔提取
- **质量防护**：一致性检查、质检验收、设定总校、真相分级管控、跨章一致性扫描
- **可插拔技能引擎**（`skill_engine.py`）：11 个 AI 技能 JSON，支持链式执行
- **本地 RAG 引擎**（`rag_engine.py`）：TF-IDF + 余弦相似度，无需外部数据库
- **AI 味清除**（`de_ai_flavor.py`）：批量扫描和清理模板化套话、霸总油腻词汇
- **封面生成**（`make_cover.py`）：CLI 驱动的图文叠加封面工具

## 快速开始

### 1. 环境要求

- Python 3.10+
- DeepSeek API Key（[获取地址](https://platform.deepseek.com)）

### 2. 安装

```bash
git clone https://github.com/gaojing-ops/AI-.git
cd AI-
pip install -r requirements.txt
```

### 3. 配置

编辑 `config.json`，填入你的 API Key：

```json
{
  "api_key": "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "base_url": "https://api.deepseek.com",
  "model": "deepseek-chat",
  "temperature": 0.85,
  "max_tokens": 8192,
  "author_name": "你的笔名",
  "tone_rules": "你的写作风格要求，如：轻松幽默、章均2000字。"
}
```

### 4. 准备设定文件

在以下目录创建你的小说设定（详见各目录下的 README.md）：

```
characters/         # 角色档案（每个角色一个 .txt 文件）
world_building/     # 世界观设定（修炼体系、势力分布等）
plot/               # 大纲与细纲（全书大纲、卷大纲、逐章细纲等）
```

**最小启动配置**：至少需要 `plot/全书大纲.txt` 和 `characters/` 下至少一个角色文件。

### 5. 启动

**GUI 模式（推荐）**：
```bash
python gui_app.py
```

**CLI 模式**：
```bash
python generator.py
```

## 使用教程

### 5.1 GUI 模式 — 单章生成

1. 启动后，系统自动加载上次项目或创建默认项目 `我的小说/`
2. 点击左上角 **⚙ 设置** 检查 API Key 和模型配置
3. 在右侧 **「写作要求」** 框中输入本章情节要求，例如：
   ```
   张三在拍卖会上发现了一件疑似师父遗物的古剑，
   通过灵识感应确认是师父的青云剑碎片之一。
   竞拍过程中遇到血魔教的人阻挠，最终在大师兄的帮助下拿下。
   ```
4. 点击 **「写新章」** 按钮
5. 等待生成完成，内容显示在中央编辑区
6. 点击 **「保存新章」** 存档

### 5.2 GUI 模式 — 一键托管批量生成

批量生成前，请确保：
- `plot/` 下有对应的 **逐章细纲文件**（如 `第一卷逐章细纲.txt`）
- 下一章的细纲已被覆盖（启动健康检查可验证）

操作步骤：
1. 点击 **「健康检查」** 确认所有项 PASS
2. 点击 **「批量写」**，输入要生成的章节数量（1-100）
3. 系统自动执行完整管线：
   ```
   生成 → 禁词快检 → 质检验收 → 设定总校 → 真相护栏 → 跨章扫描 → 保存 → 记忆维护
   ```
4. 如果某章质检未通过，会自动重试（最多3次），最终强制保存以保持连贯
5. 可随时点击 **「停止」** 中断

### 5.3 CLI 模式

```bash
python generator.py
```

主菜单：
```
1. 开始写 [新的一章]
2. 继续写 [当前最新章] (追加续写)
3. 手动新增 人物/世界观/大纲
4. 进入 [AI 灵感与工具箱]
0. 退出系统
```

AI 工具箱（选项4）包含：
- 一键生成世界观设定
- 一键生成角色组档案
- 一键生成全书大纲
- 章节润色去 AI 味
- 防崩盘逻辑/OOC 质检
- 卡文破局方案生成
- 全局记忆备忘录提炼
- 伏笔追踪表更新
- 拆解大师（从参考书提取设定）

### 5.4 逐章细纲格式

逐章细纲是批量生成的关键。格式示例（`第一卷逐章细纲.txt`）：

```
【第一卷逐章细纲】开篇（第1-50章）

第1章-第5章：开篇破局
  · 第1章：主角出场，描写环境，触发核心事件。
  · 第2章：遇到第一个冲突，展示金手指初现。
  · 第3章：第一次使用能力解决问题。
  ...

第6章-第10章：第一次小高潮
  · 第6章：...
```

关键规则：
- 每章需要有 `第X章` 标记
- 支持范围标记 `第X章-第Y章` 描述共性能
- 系统会精确匹配当前章节号对应的细纲段落

### 5.5 自定义配置

#### 禁词列表

编辑 `generator.py` 或 `gui_app.py` 中的配置区：

```python
# generator.py
FORBIDDEN_KEYWORDS = {
    "旧角色名": "原因说明",
}
WATCH_KEYWORDS = [
    "需要监控的关键词",
]
```

#### 跨章扫描器

编辑 `cross_chapter_scanner.py` 中的配置：

```python
EVENT_PATTERNS = [...]
FORBIDDEN_TERMS = [...]
CHAPTER_GATED_TERMS = [...]
OLD_NAME_PATTERNS = {...}
```

#### RAG 流派模板

编辑 `prompts.json`，根据你的小说题材自定义流派判定规则和提取维度。

### 5.6 工具脚本

```bash
# AI 味扫描
python scan.py              # 扫描全部章节
python scan.py 1 50         # 扫描指定范围

# AI 味清除
python de_ai_flavor.py clean --start 1 --end 50              # 清理1-50章
python de_ai_flavor.py clean --start 1 --end 50 --dry-run    # 预览模式

# 跨章一致性扫描
python cross_chapter_scanner.py 1 50                         # 扫描1-50章
python cross_chapter_scanner.py --check-events 1 50          # 仅事件去重
python cross_chapter_scanner.py --check-forbidden 1 50       # 仅禁词检查

# 封面生成
python make_cover.py --base cover_base.png --title "我的小说|第一卷" --author "笔名"
```

## 项目结构

```
├── gui_app.py                  # GUI 桌面应用（一键托管版）
├── generator.py                # CLI 命令行引擎
├── skill_engine.py             # 可插拔技能引擎
├── rag_engine.py               # 本地 TF-IDF RAG 引擎
├── cross_chapter_scanner.py    # 跨章一致性扫描器
├── de_ai_flavor.py             # AI 味清除工具
├── scan.py                     # AI 味扫描器
├── make_cover.py               # 封面生成器
├── prompts.json                # RAG 流派提取模板
├── config.json                 # API Key / 模型配置
├── skills/                     # 11个 AI 技能 JSON
├── characters/                 # 角色档案（你的设定）
├── world_building/             # 世界观设定（你的设定）
├── plot/                       # 大纲与细纲（你的设定）
├── output/                     # 生成章节输出
└── history/                    # 生成历史
```

## 常见问题

**Q: 批量生成时卡住不动？**
A: 检查健康检查报告中的 FAIL 项。最常见的原因是下一章的细纲未覆盖，请补充 `plot/` 下的逐章细纲文件。

**Q: 生成内容出现旧角色名或其他禁词？**
A: 将禁词添加到 `generator.py` 的 `FORBIDDEN_KEYWORDS` 或 `gui_app.py` 的 `BANNED_KEYWORDS` 中。

**Q: 上下文超限错误？**
A: 系统内置了 token 预算控制，会自动裁剪上下文。如果仍超限，请精简 `characters/` 和 `world_building/` 中不必要的大文件。

**Q: 如何切换模型？**
A: 在 `config.json` 中修改 `model` 字段。支持的模型取决于你的 API 提供商。

## License

MIT
