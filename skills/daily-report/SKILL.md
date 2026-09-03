---
name: daily-report
description: 整理每日工作点报告的措辞。工具从 Gitee 企业工作项与企业 GitLab 提交汇总出「日期 → 工作点 → 明细」，本技能负责在终端里润色措辞、确认归属存疑的条目，只重写表达不新增事实。当用户提到写日报、整理工作点、汇总本周做了什么、优化日报措辞、月底补日报、导出三段式粘回 EOM 时触发。
---

# 每日工作点整理

日常入口是网页（`uv run dr`），对话与预览都在页面上。本技能是终端里的等价路径，用于不想开浏览器、或想批量处理的场合。

## 规则

**动手前先读 [../../src/git_daily_report/web/rules.md](../../src/git_daily_report/web/rules.md)。** 不可越过的边界、归属存疑的处置、明细与三段式的写法、做完怎么汇报，全在那份文件里。网页对话的 system prompt 用的也是它，两条通道同一套规矩。

下面只讲终端通道自己的操作流程。

## 流程

### 1. 汇总

```bash
uv run dr sync --month 2026-09
```

用户没说范围就默认当月，也可以 `--since` / `--until` 取任意区间，或 `--date today` 只看一天。命令会打印工作点数、待确认归属数、各数据源拉到多少，以及被跳过的数据源（不在内网时 GitLab 会跳过）。

缺凭证会直接说缺哪一项、去哪里拿，照着补进 `.env` 即可。

### 2. 读取并润色

报告在 `output/{区间}.json`，结构是 `{"schemaVersion":2,"range":{...},"days":[{"day":"...","points":[...]}]}`。每个工作点里与你有关的只有：

| 字段 | 处理 |
| --- | --- |
| `title` | 工作点标题，可改写成更像任务名的说法 |
| `entries` | **主要要改的**，明细列表 |
| `hours` | 工时，用户说了才填 |
| `generatedTitle` / `generatedEntries` | 生成原值，别动，用于识别改动 |
| `confidence` / `candidates` | 只读，见下 |
| `commits` | 只读素材，供你理解上下文；合并提交已展开成它带进来的那批 |

按规则逐条改写，然后把整个文件写回原路径。

### 3. 先处理归属存疑的

`confidence` 不是 `exact` 的工作点是推断出来的，四个取值各自怎么处置见规则文件。

判错的根因通常是项目命名对不上。改 `config/project-mapping.json` 补一条 GitLab 项目名 → Gitee 项目名的覆盖表，下次 `sync` 就自动对上了——这比每次手工挪更划算。

### 4. 导出

```bash
uv run dr export --month 2026-09                 # 工作点格式
uv run dr export --month 2026-09 --format memo   # 三段式，粘回 EOM 页面
```

粘回 EOM 时提醒用户：三段都要有内容，工单进展、工时、存在问题、明日计划四项必填，且**当天工时合计不得超过 7.5 小时**。

再次 `sync` 不会覆盖改过的内容（靠 `title`/`entries` 与 `generated*` 不一致判定），除非显式加 `--force`。
