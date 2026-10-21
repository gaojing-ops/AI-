# 小说写作工具

用于新书策划、逐章生成、连续性检查、有限修复、记忆维护和断点续写。

## 开始使用

1. 使用 Python 3.14，双击 `启动小说工具.pyw`。
2. 点击“新建一本”，在 `projects/` 下建立独立项目并填写设定。
3. 配置模型与凭据，开始生成；需要停止时点击“暂停”。

本地 `.tools/` 保存配套依赖。新电脑可按 `requirements.txt` 安装依赖，再从示例配置开始设置。凭据使用本地配置、加密存储或环境变量，不提交 GitHub。

当前目录只保留工具、通用空白模板、测试和说明；`projects/` 初始为空。已清理的小说资料可从本机回收站恢复。

## 有界执行

同章调用次数、修复次数和时间预算持久化；恢复或切换模型不会自动归零。耗尽时先查看停止原因，只有显式续开预算后才继续。具体限制由项目配置决定。

```powershell
py -3.14 -X utf8 -B novel_cli.py status --project "projects/项目名"
py -3.14 -X utf8 -B novel_cli.py doctor --project "projects/项目名"
```

正式批次须明确终点并显式授权执行：

```powershell
py -3.14 -X utf8 -B novel_cli.py run --project "projects/项目名" --expected-latest 0 --to 3 --execute
```

## 文档和测试

- [使用说明](docs/使用说明.md)
- [目录结构](docs/目录结构.md)
- [工具状态](docs/开发状态.md)
- [维护交接](docs/HANDOVER.md)
- [项目进度](项目进度.md)

离线测试：

```powershell
py -3.14 -X utf8 -B -m pytest -q -p no:cacheprovider
```

程序测试衡量工具行为；稿件是否适合发布仍需阅读判断。
