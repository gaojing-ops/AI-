# 千章小说创作系统 📖

> 一个开箱即用的 AI 自动小说创作工具。支持 DeepSeek / MiniMax 等大模型，Tkinter 桌面 GUI，零门槛上手。

## ✨ 核心功能

| 功能 | 说明 |
|------|------|
| 📝 写新章 / 续写 | 根据细纲 + 角色设定 + 世界观自动生成章节 |
| 🚀 批量挂机写 | 全自动多章连续生成，含记忆压缩、内容防漂移、标题去重 |
| 🧠 RAG 智能检索 | 自动从知识库中匹配最相关的设定投喂 AI（TF-IDF） |
| ✨ 划线精修 | 选中文本右键 → AI 局部重写/扩写 |
| 🔮 伏笔追踪 | AI 自动追踪未解伏笔 |
| 🔍 逻辑/OOC 检查 | AI 审查逻辑硬伤和人设崩塌 |
| 📜 进度编年史 | 自动推演时间轴并记录里程碑 |
| 📑 一键导出排版 | 合并全部章节，中文首行缩进，直接上架各平台 |

## 🚀 快速开始

### 方法一：直接运行 exe（无需 Python 环境）
1. 从 [Releases](../../releases) 下载 `千章小说创作系统_桌面版.exe`
2. 双击运行，首次会自动创建项目文件夹
3. 点击左上角「⚙ 设置」→ 填入你的 [DeepSeek API Key](https://platform.deepseek.com/)
4. 在 `characters/` 放角色设定 → `world_building/` 放世界观 → `plot/` 放大纲
5. 输入写作要求 → 点击「📝 写新章」→ 开始创作！

### 方法二：从源码运行
```bash
pip install openai jieba
python gui_app.py
```

## 📁 项目结构

```
千章小说创作系统/
├── gui_app.py          # 主程序（Tkinter GUI）
├── generator.py        # 生成引擎核心
├── rag_engine.py       # TF-IDF 智能检索引擎
├── config.json         # 配置文件（API Key 等）
├── prompts.json        # AI 提示词模板
├── 使用说明.txt        # 详细使用文档
├── characters/         # 角色设定（每个角色一个 .txt）
├── world_building/     # 世界观设定
├── plot/               # 大纲 / 备忘录 / 基调铁律
├── output/             # 生成的章节
└── history/            # 历史记录
```

## 🎯 基调铁律

在 `plot/基调铁律.txt` 中写入你的小说风格规则，系统会自动注入到每次生成的 Prompt 中。换书时只需修改此文件，无需改代码。

## ⚙️ 配置

在「设置」面板或 `config.json` 中配置：
- `api_key` — DeepSeek API Key（必填）
- `minimax_api_key` — MiniMax API Key（可选，切换模型时使用）
- `temperature` — 创意度（0.0-1.5，默认 0.8）

## 📜 License

MIT License
