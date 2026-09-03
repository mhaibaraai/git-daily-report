# git-daily-report

从 Gitee 企业工作项与企业 GitLab 提交汇总每日工作点，在本机网页里审核、改写、导出。

主干是 git 活动，不依赖任何工单——工单往往滞后于实际开发，等它建好才能填日报是本末倒置。产出是「日期 → 工作点 → 明细」三层结构，另提供一份三段式视图供直接粘贴回工时填报系统（我这边是 EOM）。

> 这是个人自用工具，按我的环境写死了两个前提：任务在 **Gitee 企业版**的工作项里，代码在**自建 GitLab** 上。换环境需要改 `sources/` 下的两个客户端；三段式导出的段名与 7.5 小时上限对应我司 EOM 页面的规则，用别的系统要改 `render/memo.py` 与 `validation.py`。归属判定、存储与网页层与数据源无关，可以直接复用。

```markdown
## 2026-08-01
### 应急平台态势图层重构
- 完成图层切换逻辑
- 修复图例渲染错位
### 组件库维护
- 修复表单嵌套字段校验失效
```

## 安装

```bash
uv sync
cp .env.example .env
```

`.env` 需要三处凭证：

| 变量 | 怎么拿 |
| --- | --- |
| `GITEE_ACCESS_TOKEN` | `gitee.com/profile/personal_access_tokens`，勾 `enterprises` |
| `GITLAB_TOKEN` | `<GitLab 实例>/-/user_settings/personal_access_tokens`，勾 `read_api` |
| `AI_API_KEY` | 任意 OpenAI 兼容接口的 key，配 `AI_BASE_URL` 与 `AI_MODEL` |

另需 `GITEE_ASSIGNEE`（Gitee 登录名）。GitLab 是内网实例，脱离内网时该源自动跳过，Gitee 侧照常出结果。AI 没配也不影响采集与导出，只是页面上不能对话。

凭证只留在服务端，不下发浏览器，不写入报告，不打印到日志。

## 用法

```bash
uv run dr
```

就这一条。它起本机服务并打开浏览器，剩下的都在页面上：

- 顶栏选区间——`今天` / `本周` / `本月` / `上月`，或直接改两个日期框
- 点「拉取数据」，进度实时回显各数据源拉到多少
- 右侧预览三层结构，归属靠推断的工作点会标出来
- 「复制全文」「下载 .md」，可切「三段式」视图粘回 EOM
- 左侧对话交给 AI：「把 8/1 那两条合并」「这条改得专业点」「未关联的挪到态势图层」

命令入口装在 `.venv/bin/dr`。想直接敲 `dr`，先 `source .venv/bin/activate`，或 `uv tool install --editable .` 装到全局。

### 脚本化兜底

日常不需要，写脚本时才用：

```bash
uv run dr sync --month 2026-08                     # 拉数写入 output/2026-08.json
uv run dr sync --since 2026-08-04 --until 2026-08-10
uv run dr sync --month 2026-08 --sources gitee     # 不在内网时只跑 Gitee
uv run dr sync --month 2026-08 --force             # 丢弃改过的内容重新生成
uv run dr export --month 2026-08                   # 导出 output/2026-08.md
uv run dr export --month 2026-08 --format memo     # 三段式
uv run dr web --port 8080 --no-open                # 指定端口、不开浏览器
```

## 架构

```text
Gitee 企业工作项 ──► 任务容器（跨天活跃区间）─┐
                                              ├─► 按天归并 ──► output/{区间}.json
GitLab 逐条 commit ─► 明细（按天）────────────┘                      │
                                                                    ├─► Markdown
                                        本机网页：预览 / 对话 / 导出 ┘─► 三段式
```

```text
src/git_daily_report/
├── config.py / period.py / pipeline.py / cli.py
├── models.py            # WorkItem / Commit / WorkPoint / DayReport
├── report.py            # 按天归并成工作点
├── store.py             # v2 读写与合并
├── text.py              # 提交文本清洗
├── validation.py        # 当日工时上限提醒
├── sources/{gitee,gitlab}.py
├── linking/
│   ├── matcher.py       # 项目名归一化 + 人工覆盖表
│   └── attribution.py   # commit 归属判定与消歧
├── render/{markdown,memo}.py
└── web/{server,tools,llm}.py + page.html
```

`output/{区间}.json` 是唯一事实来源（`schemaVersion: 2`），Markdown 与三段式都由它单向导出。重跑采集不会覆盖改过的标题与明细（比对 `title`/`entries` 与 `generatedTitle`/`generatedEntries`），除非 `--force`。

### 工作项是跨天容器

工作项不再被压成某一天的素材，而是从创建起算的活跃区间：未完成的一直有效，完成了就到完成那天为止。某天有提交打到它，它才在那天出现。工作项完成那天额外留一条记录——关闭任务是实打实的成果；创建不留，那往往是项目经理建单或自己接手，当天未必做了事。

### 合并提交要展开，不能丢

实测本人名下的提交里九成以上是把开发分支合进 `dev` 的 Merge，标题本身没有信息量，但它带进来的那批提交才是当天真正集成的内容。因此对每个合并提交用 `parent_ids` 调 `/repository/compare` 取出第一父到第二父之间那一段，用它们的标题当明细，归到合并当天。

带进来的提交常常是同事写的，**不再按作者过滤**——集成别人的代码也是当天的工作。展不开时（compare 失败或为空）退化成「合并分支 dev-20260821」，不让这条痕迹凭空消失。单次合并最多取 50 条，防止合并长期分支时刷屏。

### 关联匹配与置信度

commit message 与分支名里不带工作项编号，归属只能靠推断。可用信号只有两个：提交所在项目的名字，以及工作项的活跃区间。因此每个工作点都带一档置信度：

| 档次 | 判定 | 页面标记 |
| --- | --- | --- |
| `exact` | 项目名归一化后唯一命中一个活跃工作项 | 无 |
| `fuzzy` | 多个候选，靠提交标题与工作项标题重合选出 | 按标题推断 |
| `ambiguous` | 多个候选无法区分，取活跃跨度最窄者并列出全部候选 | 归属存疑 |
| `sole` | 项目名对不上，但当天只有一个活跃工作项 | 当天唯一任务 |
| `unmatched` | 归不上，按所在项目单独成点 | 未关联 |

归不上的提交不丢弃。标记只进页面预览，复制出去的正文是干净的。

消歧唯一的确定性手段是 `config/project-mapping.json`（见同目录 `.example.json`）写 GitLab 项目名 → Gitee 项目名的覆盖表。误判偏多时优先补覆盖表，而不是放宽匹配规则。

提交信息按是否命中「修复/定位/排查/异常/优化」等关键词分流进三段式的「问题解决过程」或「具体任务」，合并提交丢弃，conventional commits 前缀剥掉。抽不出的段落留空，**绝不编造**。

## 容易踩的坑

- **GitLab 两个接口的时间语义不同**：`/events` 的 `after`/`before` 是开区间日期，请求时两端各外扩一天再本地收窄；`/repository/commits` 的 `since`/`until` 是闭区间 ISO8601 时间戳，不能照搬那套外扩
- **不遍历全部仓库**：先用推送事件圈定区间内推过哪些项目，再只对这些项目拉逐条提交
- **提交作者在本地过滤**：用 `/user` 拿到的邮箱与名字比对；拿不到身份时全部保留，宁可多给也不静默丢弃。这层过滤只作用于顶层提交，合并展开出来的那批不再过滤
- **Gitee 时间过滤只有 `since` 能用**：`created_at`/`finished_at`/`deadline` 的各种范围写法实测一律 422，上界只能本地过滤
- **Gitee 企业接口别走 `api.gitee.com`**：那是 OpenAPI V8，需独立企业授权；`gitee.com/api/v5/enterprises/{slug}/issues` 用个人令牌就能读
- **SSE 要在发头之前就决定关连接**：没有 `Content-Length`，HTTP/1.1 keep-alive 下客户端会一直等下一个响应
- **粘回 EOM 时当天工时合计不得超过 7.5 小时**：前端硬规则，超了整批拒收，页面会提前提醒

## 边界

- 不写 EOM。三段式视图是给人复制粘贴的，工具本身没有任何写回通道
- 服务只绑 `127.0.0.1`，不做部署、不对外暴露
- AI 只改措辞与归属，不新增事实；数字、工作项标题、项目名一律以报告原值为准
- 抽不出的内容保持为空并如实汇报，不做推测填充

## 未决

- 工作项状态是否需要过滤未定，当前 `state=all` 全拉
- Gitee 上的代码提交尚未采集，只读企业工作项；代码托管在 Gitee 的项目目前只能靠工作项本身留痕
- 同一项目下多个工作项并行时，`ambiguous` 的比例取决于项目划分粒度，需要靠覆盖表长期打磨

## 开发

```bash
uv run pytest              # 带覆盖率
uv run pytest -q --no-cov  # 快速跑

grep -rn "ScheduleOrder\|ReportRow\|wecom" src/    # 应无结果
```
