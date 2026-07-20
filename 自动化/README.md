# 自动化工作流

本目录实现每日统计和每周周测。脚本默认只读取已经提交到 Git 的内容，只生成报告，不修改题图、Markdown 或 Git 历史。

## 已确定的规则

- 时区：北京时间（`Asia/Shanghai`）。
- 每日统计：每天 22:30，生成 `报告/每日/日报-YYYY-MM-DD.md`。
- 每周周测：周日 08:00，分别生成数学和 408 两套题，以及各自的答案核验文件。
- 周测范围：运行时刻向前 7 天；周日早晨生成的是上一周的复习材料。
- 每科题量：约 10 题，目标为约 70% 原题/原题改编、30% 变式题。
- 不读取未提交内容，不自动提交生成物。
- 题型、方法和测试题通过 `S001` 等来源编号链接到对应题图；报告末尾同时提供来源索引。

## Git 提交 message 约定

每日错题整理完成后使用：

```text
daily: 2026-07-21
```

日期必须是北京时间当天日期，且每天尽量只使用一个 `daily:` 提交。其他允许格式为：

```text
weekly: 2026-W30
docs: 修改说明
chore: 初始化或维护说明
fix: 修复说明
```

每日脚本只把严格符合 `daily: YYYY-MM-DD` 的提交作为错题资料来源；不符合格式的提交会在日报中提示。

## 配置 AI

脚本使用 OpenAI 兼容的 Chat Completions API，并将题图作为 `image_url` Base64 图片输入。当前约定的模型是 `gpt-5.6-sol`，配置文件中的模型只是默认值，可使用环境变量覆盖。

推荐配置方式：

1. 将 `.env.example` 复制为 `.env`。
2. 在 `.env` 中填写 `OPENAI_API_KEY`。
3. 设置 `OPENAI_BASE_URL`，脚本会拼接 `/v1/chat/completions`；也可以直接设置 `OPENAI_CHAT_COMPLETIONS_URL`。
4. 设置 `OPENAI_MODEL`；未设置时使用 `gpt-5.6-sol`。
5. 不要把 `.env` 提交到 Git。

也可以直接设置 Windows 用户环境变量。脚本不会在日志或报告中输出密钥。

## 手动运行

在仓库根目录执行：

```powershell
python .\自动化\main.py check --date 2026-07-21
python .\自动化\main.py daily --date 2026-07-21
python .\自动化\main.py weekly --at 2026-07-27T08:00:00+08:00
```

任务计划程序使用 `--scheduled`，脚本会按照配置中的北京时间边界计算统计日期和周测结束时间；即使 Windows 因睡眠或延迟唤醒，也不会把实际唤醒时刻误当成统计边界：

```powershell
python .\自动化\main.py daily --scheduled
python .\自动化\main.py weekly --scheduled
```

首次验证可加 `--no-ai`，只检查 Git、Markdown、题图和报告结构，不向外部 AI 发送图片：

```powershell
python .\自动化\main.py daily --date 2026-07-21 --no-ai
python .\自动化\main.py weekly --at 2026-07-27T08:00:00+08:00 --no-ai
```

只预览而不写入报告文件：

```powershell
python .\自动化\main.py daily --date 2026-07-21 --no-ai --dry-run
python .\自动化\main.py weekly --at 2026-07-27T08:00:00+08:00 --no-ai --dry-run
```

脚本会额外检查工作区中的未提交路径。若某个已提交错题笔记或题图仍有未提交修改，相关来源会被跳过并在报告中标记，避免把未提交内容发送给外部 AI；其他未提交文件也不会成为统计来源。

## 安装和移除 Windows 定时任务

确认手动运行无误、API 密钥已配置后，在 PowerShell 中执行：

```powershell
PowerShell -ExecutionPolicy Bypass -File .\自动化\安装定时任务.ps1
```

安装脚本会读取 `config.json` 中的时间和星期，并要求 Windows 时区为 `China Standard Time`；它只注册任务，不会提交报告或笔记。

任务名称为：

- `每日错题-每日统计`：每天 22:30。
- `每日错题-周测`：每周日 08:00。

移除任务：

```powershell
PowerShell -ExecutionPolicy Bypass -File .\自动化\卸载定时任务.ps1
```

## 失败处理

- API 密钥缺失、网络失败或 AI 输出格式不正确时，脚本仍会写出报告，并在报告中保留错误原因。
- 任何断链、孤立题图、未知来源编号或提交 message 问题都会在报告中标记，脚本不会自动修复。
- 报告是待审阅生成物；确认无误后，可手动使用 `weekly: YYYY-Www` 或 `docs: ...` 提交。
