---
name: eom-weekly
description: 按 EOM 日程表与 GitLab 提交写每周的工单进展（EOM 周报）。用户说「写周报」「写本周 / 上周 EOM 周报」「生成工单进展」「补某几天的日程表进展」时使用。
---

# EOM 周报

由你来写工单进展。`dr` 只负责拉素材、做规则校验、落盘，结果在本机网页上逐块复制到 EOM。

`dr` 不在 PATH 时，在 git-daily-report 仓库里用 `uv run dr` 代替。

## 步骤

1. **定区间**：用户没说就是本周，不传参数；「上周」或具体日期换算成 `--since YYYY-MM-DD --until YYYY-MM-DD`，后续每条命令都带同样的参数。
2. **拉素材**：运行 `dr material [--since D --until D]`。stdout 是 JSON：
   - `rules`：写作规则与输出格式，逐条严格遵守
   - `material`：排期与提交，唯一的素材来源，不得编造
   - `pending`：必须输出的（日期, 工单），一条不能少也不能多

   `pending` 为空就告诉用户没有要写的块，结束。命令失败时把 stderr 原样转告用户（常见是 `EOM_TOKEN` 过期，按 README 重新取）。
3. **写进展**：按 `rules` 写出 `{"items": [...]}`，存成临时目录下的 JSON 文件（不要放进仓库）。
4. **校验落盘**：运行 `dr apply <文件> [--since D --until D]`，stdout 是 JSON：
   - `failures`：不合格的块与原因
   - `missing`：还没写的块
5. **只重写问题块**：有 `failures` 或 `missing` 时，按原因只重写这些块，写成新文件再 `apply`，已通过的块会保留。最多重试 2 轮，仍不过的告诉用户，网页上会标红。
6. **收尾**：报告生成了几块、几块需复核，提示用户运行 `dr` 打开网页逐块复制，粘回 EOM「提交日程表」。

## 边界

- 不调用、不写回 EOM，只读排期
- 不要把 `material` 里的提交原文、文件名、英文标识符直接搬进进展
- 不要读取或输出 `.env` 里的凭证
