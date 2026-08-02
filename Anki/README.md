# Obsidian 错题导入 Anki

## 直接使用

在 Anki 中选择“文件 → 导入”，导入 `每日错题-408与数学.apkg`。导入后会出现：

- `每日错题::408`
- `每日错题::数学`

题目面包含原题图，答案面包含 Obsidian 笔记中已有的答案和解析，并保留 Question ID、来源文件与行号。

导入包的复习预设由 `自动化/config.json` 的每个 subject 的 `anki` 配置生成：

- 408：按背单词的方式高频复习，FSRS 目标保持率 95%，每天 20 张新卡。
- 数学：按回看错题的方式低频复习，FSRS 目标保持率 90%，每天 5 张新卡。

如果 Anki 已启用 FSRS，会使用两套预设各自的目标保持率；没有启用 FSRS 时，会使用包内对应的旧调度器参数。建议使用 Anki 23.10 或更新版本，并在导入时保留卡组预设。

## 重新生成

在仓库根目录运行：

```powershell
python -m pip install -r requirements-core.txt
python -m pip install -r Anki/requirements.txt
python Anki/build_anki.py
```

CI 或不希望生成 `.apkg` 时，可运行仓库内安全 fixture：

```powershell
python Anki/check_anki.py
```

该命令只创建临时仓库内图片，验证 Anki 媒体解析和外部/绝对/逃逸路径拒绝；失败返回非零，不代表完整卡组构建成功。

构建前应先运行 `python 自动化/main.py index` 并提交持久 Question ID 索引；未登记题图不会生成临时 ID。构建过程只读取配置 subjects 列表中的笔记、题图和索引，不修改、移动或删除原笔记和原图。每张卡同时在独立的 `题目ID` 字段和 `qid::<Question ID>` 标签保存身份。所有媒体必须通过仓库内安全路径检查，输出包括：

- `每日错题-408与数学.apkg`：可直接导入的卡组包。
- `卡片清单.csv`：所有候选题目及是否导入。
- `待补清单.md`：因答案为空、`todo`、缺图等原因未导入的条目。
- `构建报告.md`：卡片数量与复习参数摘要。

`.apkg` 默认被 `.gitignore` 排除且不应继续追踪历史二进制；Anki 负责主要间隔重复调度，仓库 Question ID 负责内容身份、错因与诊断，二者不维护互相独立的主调度器。
