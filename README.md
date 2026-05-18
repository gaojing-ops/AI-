# 通用小说一键托管工具

这是从两个旧项目工具中合并出的通用底座。旧项目内容没有复制进来；题材差异通过配置和模板包管理。

## 启动

```powershell
pip install -r requirements.txt
python gui_app.py
```

首次使用：
1. 点击“开书向导”，填写书名、总章数和卷范围。
2. 在 `characters/`、`world_building/`、`plot/` 填入设定、大纲和逐章细纲。
3. 点击“体检大纲”，确认没有 FAIL。
4. 点击“设置”，填写 API Key。
5. 点击“一键写完整本”。

## 目录

- `output/`：工作稿、待审稿、托管检查稿。
- `publish/`：通过发布检查的干净章节。
- `logs/`：批量状态、审计日志、章节状态。
- `templates/`：可选题材配置包。

## 托管策略

默认 `trusteeship_policy` 为 `lenient_continue`：
- 普通质量问题记录为“可继续但需复核”，托管继续。
- API、空稿、章节号错误、严重重复、硬禁词、严格真相越界、当前章跨章 HIGH 会暂停。

自动润色不进托管主链路。托管只做生成、检查、状态记录和记忆维护。
