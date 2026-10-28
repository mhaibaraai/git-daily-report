# git-daily-report

按 EOM 日程表与 GitLab 提交，用 AI 生成每周的工单进展，在本机网页里逐块复制，粘回 EOM「提交日程表」。

以 **EOM 日程表为准**：某天排了哪些工单，就给每个工单出一份进展；节假日与调休休息日在日程表上没有工单，自然不会出现。GitLab 提交只是素材——提交推迟、某天缺失都很常见，AI 会把相邻几天的提交拆开、按阶段延展，保证每个工单每天都有产出。

```text
具体任务：完成订单状态回写；补充异常记录标记
关键数据：覆盖 12 个门店
问题解决过程：无
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
| `AI_BASE_URL` / `AI_API_KEY` / `AI_MODEL` | 任意 OpenAI 兼容接口 |

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
- 底栏「复制全文」「下载 .md」导出整周

工单进展百分比与工时不生成，在 EOM 里填。

## 生成规则

规则写在 [src/git_daily_report/generate_rules.md](src/git_daily_report/generate_rules.md)，就是给 AI 的系统提示：

- 日程表里每个未填的（日期, 工单）都要输出，具体任务不能为空
- 提交按 EOM 的项目名称、任务标题、工单标题与 GitLab 项目名、提交内容的相关性归到工单上
- 可以拆分、延展相邻日期的提交，不能编造提交里没有的功能
- 只用业务语言，禁止文件名、函数名、英文标识符、路径、分支名、提交前缀
- 三项中没有内容的写「无」；关键数据只写能从提交里数出来的量

AI 的输出不直接信任，先过 [validate.py](src/git_daily_report/validate.py) 的规则校验：缺块、具体任务为空、明日计划为空、含代码化描述。不合格的块带着原因让 AI 重写一次，仍不合格的保留原文、卡片标红写明原因，不阻塞其余的块。

已经在 EOM 里填过的工单块原样展示（标「EOM 已填」），不会重新生成。

## 架构

```text
EOM 日程表（每天 × 工单）────┐
                            ├─► AI 生成整周 ─► 规则校验 ─► output/{周}.json ─► 网页逐块复制
GitLab 提交（区间前后 3 天）─┘
```

```text
src/git_daily_report/
├── config.py / period.py / pipeline.py / cli.py
├── models.py              # Commit / Order / Progress / ScheduleDay
├── store.py               # 周报文件读写，重新生成直接覆盖
├── text.py                # 提交文本清洗
├── generate.py            # 组素材、调 AI、解析、重试
├── generate_rules.md      # 给 AI 的规则
├── validate.py            # 三项齐全、禁代码化描述
├── sources/{eom,gitlab}.py
├── render/memo.py         # 三段式拼接与整周导出
└── web/{server,session}.py + page.html
```

### EOM 接口

取自 EOM 前端「日程表」页面，所有业务请求都走 `POST {EOM_BASE_URL}/core/busi/exec`，请求头带 `token`、`application: platform`、`tenant: default`，靠 `condition.op` 区分操作。这里只用 `op: "schedule"`（按年月取每天的工单与已填进展）。

**只读**：工具不调用 `commitReport`，没有任何写回 EOM 的通道。

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

- 不写 EOM。页面是给人复制粘贴的，工具本身没有任何写回通道
- 服务只绑 `127.0.0.1`，不做部署、不对外暴露
- AI 只拆分、细化、改写提交里已有的工作，不编造功能与数字

## 开发

```bash
uv run pytest              # 带覆盖率
uv run pytest -q --no-cov  # 快速跑
```
