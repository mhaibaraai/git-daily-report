# git-daily-report

按 EOM 日程表与 GitLab 提交，用 AI 生成每周的工单进展、完成百分比和工时，在本机网页核对后，通过接口一键提交到 EOM。

以 **EOM 日程表为准**：某天排了哪些工单，就给每个工单出一份进展；节假日与调休休息日在日程表上没有工单，自然不会出现。GitLab 提交只是素材——提交推迟、某天缺失都很常见，AI 会把相邻几天的提交拆开、按阶段延展，保证每个工单每天都有产出。

```text
具体任务：完成订单状态回写；补充异常记录标记
关键数据：覆盖 12 个门店
问题解决过程：无
工单进展：65%（估算，可调整）
当日工时：4.5h（估算，可调整）
```

> 个人自用工具，写死了两个前提：工时填报在公司内部的 **EOM**（企微内登录），代码在**自建 GitLab** 上。

## 安装

```bash
uv sync
cp .env.example .env
```

`.env` 需要三组配置：

| 变量 | 怎么拿 |
| --- | --- |
| `EOM_BASE_URL` / `EOM_TOKEN` | 见下方「获取 EOM token」 |
| `GITLAB_BASE_URL` / `GITLAB_TOKEN` | `<GitLab 实例>/-/user_settings/personal_access_tokens`，勾 `read_api` |
| `AI_BASE_URL` / `AI_API_KEY` / `AI_MODEL` | 任意 OpenAI 兼容接口；由 Codex 或 Claude Code 生成时可不填 |

凭证只留在服务端，不下发浏览器，不写入报告，不打印到日志。

### 获取 EOM token

EOM 只能在企微内登录，token 要用企微自带的网页检查器取。Safari 的「开发」菜单连不上企微的内置网页，会显示「无可检查的应用程序」。

1. 企业微信 Mac 版按 `Cmd+Shift+Ctrl+D`，提示「Debug 模式开启」，菜单栏多出「调试」菜单
2. 菜单栏「调试」→「浏览器、webView 相关」→「开启 webView 元素审查」（开启后这一项变成「关闭 webView 元素审查」）
3. 在企微工作台打开 EOM，进入日程表页面，在页面上右键 →「检查元素」
4. 检查器切到「网络」，在日程表上切一下月份，点开任意一个 `core/busi/exec` 请求：
   - 请求地址去掉末尾的 `/core/busi/exec`，就是 `EOM_BASE_URL`
   - 请求头里 `token` 的值，就是 `EOM_TOKEN`

token 失效后页面会提示，按同样步骤重新取一次即可。用完可以在同一菜单里关闭元素审查，再按一次 `Cmd+Shift+Ctrl+D` 退出调试模式。

## 用法

```bash
uv run dr                        # 默认本周
uv run dr --port 8080 --no-open  # 指定端口、不开浏览器
```

页面上：

- 顶栏选「本周」「上周」，或直接改两个日期框
- 点「拉取并生成」：拉 EOM 排期 → 拉 GitLab 提交 → AI 生成，进度实时显示
- 按天、按工单一张卡片，「今日进展」「存在问题」「明日计划」各带一个复制按钮，对应 EOM 弹窗里的同名输入框
- 「今日进展」粘进 EOM 后点「自动编排」即可拆成三项
- 每张卡片展示累计完成百分比、当日工时；生成值标为估算，可以修改后保存
- 每天默认按工作量分满 7.5h，扣除 EOM 已填工时；可手动调低，所有工单合计不得超过 7.5h
- 点「一键提交当前区间」通过接口按天提交，已在 EOM 填写的日报跳过；生成与提交互相独立
- 底栏「复制全文」「下载 .md」导出整周

查看本地报告不需要 AI 或 GitLab 配置，提交只需要 EOM 配置。缺少百分比、工时或存在校验问题的日期不能提交，其他日期可以继续。

### 在 Codex 或 Claude Code 里使用

可以直接在 Codex 使用。仓库的 [.agents/skills/eom-weekly/SKILL.md](.agents/skills/eom-weekly/SKILL.md) 是 Codex 技能入口，由当前助手自己写进展，不需要单独的 AI 接口。结果写入同一份 `output/{周}.json`，可在网页查看、调整和提交。

一次性安装，让任意目录都能用：

```bash
uv tool install --editable .                                    # dr 进入 PATH，.env 与 output/ 仍在本仓库
ln -s "$PWD/.agents/skills/eom-weekly" ~/.agents/skills/eom-weekly   # Codex；已有同名技能时先检查，勿覆盖
ln -s "$PWD/.claude/skills/eom-weekly" ~/.claude/skills/eom-weekly
```

之后说「写本周 EOM 周报」即可。普通写周报只生成；明确要求「提交到 EOM」时才提交。Claude Code 入口见 [.claude/skills/eom-weekly/SKILL.md](.claude/skills/eom-weekly/SKILL.md)。底层命令：

```bash
dr material [--since D --until D]          # 拉排期与提交，存排期，输出规则、素材与待写块
dr apply items.json [--since D --until D]  # 校验并填入，输出不合格与缺失的块
dr amend [--since D --until D]             # 按各工单 ID 独立重排已填百分比，保存待核对
dr submit [--since D --until D] --dry-run # 读取最新 EOM 排期并预检，不写入
dr submit [--since D --until D]           # 按天提交本地报告
```

`apply` 输入的每条记录增加 `rate` 与 `hours` 数值，例如 `"rate": 65, "hours": 4.5`。同工单完成百分比按日期不得倒退；拉取排期时读取工单开始月至今的已填进度作为上下界，包含所选区间之外的记录，这些上下文日期不会被提交。旧报告仍可查看，缺少这两个字段时需先补齐。

工单 ID 是百分比的唯一分组依据。不同 ID 即使标题、项目、任务或人员相同，也各自按自身排期从 0–100 计算，不继承其他 ID 的百分比；只有同一 ID 的跨日记录需要衔接且不得倒退。新 ID 的首日可按已完成工作估算为大于 0，无需强制填 0。

### 修正已填百分比

已填百分比需要重算时，用 `dr amend` 按每个工单 ID 自身的全部排期日独立线性分配到 100%，不把同标题的其他 ID 算入分母。只改百分比，工时与文字保持 EOM 原样；起始日期须覆盖当前工单 ID 的排期首日，无需覆盖其他 ID。

```bash
dr amend --since 2026-10-08 --until 2026-10-20          # 保存为待修正，页面标「待修正，原 X%」
dr submit --since 2026-10-08 --until 2026-10-20 --dry-run
dr submit --since 2026-10-08 --until 2026-10-20         # 按日报 ID 覆盖（op=8），回读核实
```

网页切到同一区间可逐条调整百分比，工时只读；改回原值即撤销该条修正。提交前若 EOM 里的百分比已被改动，该日阻止提交。修正写入后，同一 ID 的后续日期重新拉取生成即可按新进度衔接，其他 ID 不受影响。

提交会返回逐日结果；存在失败、冲突、阻止或待核实日期时命令退出码为 1。遇到超时、断线会回读 EOM 核实，仍无法确认则保存待核实记录并暂停后续日期，不自动重发。人工核对后，可在网页选择主动重试，或运行：

```bash
dr submit [--since D --until D] --retry-unknown
```

主动重试仍先回读，只重试确认未出现日报的待核实批次；存在内容冲突或部分写入时保留核实状态。凭据只从配置读取，不把抓包中的 token 写入代码或报告。

## 生成规则

规则写在 [src/git_daily_report/generate_rules.md](src/git_daily_report/generate_rules.md)，就是给 AI 的系统提示：

- 日程表里每个未填的（日期, 工单）都要输出，具体任务不能为空
- 提交按 EOM 的项目名称、任务标题、工单标题与 GitLab 项目名、提交内容的相关性归到工单上
- 可以拆分、延展相邻日期的提交，不能编造提交里没有的功能
- 只用业务语言，禁止文件名、函数名、英文标识符、路径、分支名、提交前缀
- 三项中没有内容的写「无」；关键数据只写能从提交里数出来的量
- 自动估算累计完成百分比和各工单工时，默认每天分满 7.5h；已填工时必须计入当日上限

AI 的输出先过 [validate.py](src/git_daily_report/validate.py) 校验：必填项、代码化描述、百分比、跨日进度及整日工时。不合格的块带着原因让 AI 重写一次；局部重写也会重算整天。仍不合格的保留原文并标红，该日期不能提交。

已经在 EOM 里填过的工单块原样展示（标「EOM 已填」），不会重新生成。

## 架构

```text
EOM 日程表（每天 × 工单）────┐
                            ├─► AI 生成整周 ─► 规则校验 ─► output/{周}.json ─► 网页调整 / 导出
GitLab 提交（区间前后 3 天）─┘       ▲
                                    └── 或：dr material ─► Codex / Claude Code 写 ─► dr apply
                                                                  │
                                  dr submit / 网页提交 ◄────────────┘
                                    └── 最新排期预检 ─► commitReport ─► 回读核实
```

```text
src/git_daily_report/
├── config.py / period.py / pipeline.py
├── cli.py                 # dr 起网页；material / apply / submit 给技能用
├── models.py              # Commit / Order / Progress / ScheduleDay
├── store.py               # 周报文件读写，重新生成直接覆盖
├── text.py                # 提交文本清洗
├── generate.py            # 组素材、调 AI、解析、重试
├── generate_rules.md      # 给 AI 的规则
├── validate.py            # 三项齐全、禁代码化描述
├── submit.py / locking.py # 按天提交、待核实记录与报告锁
├── amend.py               # 按各工单 ID 独立重排已填百分比
├── sources/{eom,gitlab}.py
├── render/memo.py         # 三段式拼接与整周导出
└── web/{server,session}.py + page.html
```

### EOM 接口

取自 EOM 前端「日程表」页面，所有业务请求都走 `POST {EOM_BASE_URL}/core/busi/exec`，请求头带 `token`、`application: platform`、`tenant: default`，靠 `condition.op` 区分操作。`schedule` 按年月读取排期，`commitReport` 提交日报。

提交按日期组织 `rows`，`rate` 映射为 `ewdRate`，`hours` 映射为 `ewdWorkRealTimes`；新填 `op=4`，修正已填日报 `op=8` 并带日报 `id`。其他元数据从最新排期读取，不交给 AI 生成。写请求只发送一次，成功响应后还需回读逐项核对。`output/.eom-submit.json` 共享待核实记录，目录级提交锁防止周报与自定义区间重复提交，报告锁保护编辑与生成；均不含凭据。

### 合并提交要展开，不能丢

本人名下的提交里九成以上是把开发分支合进 `dev` 的 Merge，标题本身没有信息量，但它带进来的那批提交才是当天真正集成的内容。因此对每个合并提交用 `parent_ids` 调 `/repository/compare` 取出第一父到第二父之间那一段，归到合并当天。带进来的提交常常是同事写的，不再按作者过滤。展不开时退化成「合并分支 xxx」。单次合并最多取 50 条。

## 容易踩的坑

- **EOM 日历会带相邻月份的补位日**：跨月区间要请求两个月，只认各自当月的日期，否则会重复
- **EOM 不让填未来的日子**：今天之后的排期不生成
- **GitLab 两个接口的时间语义不同**：`/events` 的 `after`/`before` 是开区间日期，请求时两端各外扩一天再本地收窄；`/repository/commits` 的 `since`/`until` 是闭区间 ISO8601 时间戳
- **不遍历全部仓库**：先用推送事件圈定区间内推过哪些项目，再只对这些项目拉逐条提交
- **SSE 要在发头之前就决定关连接**：没有 `Content-Length`，HTTP/1.1 keep-alive 下客户端会一直等下一个响应
- **EOM 提交时当天工时合计不得超过 7.5 小时**：前端硬规则，填工时时注意

## 边界

- 只有显式提交操作会写 EOM；只有 `dr amend` 或网页标为「待修正」的条目会覆盖已有日报，且只改百分比
- 服务只绑 `127.0.0.1`，不做部署、不对外暴露
- AI 只拆分、细化、改写提交里已有的工作，不编造功能与数字

## 开发

```bash
uv run pytest              # 带覆盖率
uv run pytest -q --no-cov  # 快速跑
```
