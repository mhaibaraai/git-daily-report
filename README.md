# git-daily-report

一个标准 Agent Skill，供 Claude Code、Codex 等支持 `SKILL.md` 的 AI 代理使用，结合 GitLab 提交与 EOM 工单排期生成日报、周报，也支持纯 GitLab 或本地 Git 报告。

内容由当前代理撰写；Python 脚本负责取材、校验、保存和按需提交。无需独立模型接口、服务端或 Python 第三方运行依赖。

## 安装

将整个技能目录放入目标代理的技能目录，名称保留为 `git-daily-report`；`SKILL.md`、`scripts/`、`references/` 与 `assets/` 一起保留。

| 代理 | 常见用户级位置 |
| --- | --- |
| Codex | `~/.agents/skills/git-daily-report/` |
| Claude Code | `~/.claude/skills/git-daily-report/` |

也可以使用代理支持的项目级技能目录。已有同名技能时先检查再决定是否替换。脚本运行需要 shell 权限与 Python 3.9+；本地取材另需支持 `--since-as-filter` 的 Git。除 UTC 外的显式时区依赖系统时区数据库。

## 配置

默认流程为 GitLab＋EOM；缺少配置时明确提示，不自动退回本地来源。将 [配置模板](assets/config.example.json) 复制到技能目录以外的个人配置位置，填入实际实例地址、填报身份、时区和每日工时。

使用 `--config <文件.json>` 或环境变量 `GDR_CONFIG` 选择配置。令牌可以直接写入个人配置的 `token` 字段，也可以通过 `token_env` 引用环境变量（默认 `GITLAB_TOKEN`、`EOM_TOKEN`）；同一个服务只选一种方式。`settings.local.json` 已被 Git 忽略，含令牌的配置不要提交或输出。脚本不读取 `.env`。详细字段见 [配置说明](references/configuration.md)。

实例配置与运行状态留在个人工作目录，技能中不包含真实公司、项目或专属分支配置。EOM 使用独立协议适配器，并非企微官方通用工单接口。

## 使用

```text
结合 GitLab 和 EOM 排期，生成本周工单进展。
生成今天的工单日报，校验通过后提交到 EOM。
预检这段时间的日报，先不要提交。
把指定工单的已填百分比修正为我提供的数值，只保存提案。
只用 GitLab 生成上周工作总结。
只根据当前本地仓库生成今天的日报，不连接远程系统。
```

- 工单模式按 EOM 的日期与 ID 组织；刷新保留草稿，已有日报默认跳过。
- 工时、百分比允许标注估算，每日预算由用户配置；进度按工单 ID 独立衔接。
- 没有事实的工单保持待补充，推断的后续计划需要确认。
- 普通“写报告”不提交；明确要求提交时才写入 EOM。
- 每次提交前用 `review` 生成可核查 Markdown；默认保存在配置文件旁的 `reports/年份/实例标识/`（项目内已忽略），不同正文保留不同版本用于年度总结。实际提交前必须成功归档，可通过 `archive_dir` 自定义固定目录。
- 已填正文使用 `revise` 保存修正提案，审阅后按需提交；保持百分比和工时，原值变化时阻止覆盖。
- 生成说明留在本地审核信息中，缺失的关键数据、问题解决过程和存在问题填“无”。
- 写入后回读核实，结果未知时持久化状态并暂停，不自动重复发送。
- 本地 Git 模式保留原有多仓库、作者筛选、分支及日期范围参数，不需要远程配置或凭证。

## 脚本接口

```bash
python3 scripts/report.py material --config /path/to/settings.local.json --period this-week
python3 scripts/report.py apply /path/to/items.json --config /path/to/settings.local.json --period this-week
python3 scripts/report.py revise /path/to/revision.json --config /path/to/settings.local.json --period this-week
python3 scripts/report.py review --config /path/to/settings.local.json --period this-week
python3 scripts/report.py submit --dry-run --config /path/to/settings.local.json --period this-week
python3 scripts/report.py --help
python3 scripts/collect_git_activity.py --help
```

在代理中从技能绝对路径调用脚本；以上相对路径仅用于在本仓库检查接口。各命令保持相同的配置、模式、日期和时区。查看、导出已有报告不需要服务令牌。

[技能入口](SKILL.md) 负责流程选择；[GitLab 取材](references/gitlab.md)、[EOM 流程与输入格式](references/eom.md)、[本地 Git 参数](references/collection.md) 和 [写作规则](references/report-writing.md) 按需加载。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
python3 scripts/report.py --help
python3 scripts/collect_git_activity.py --help
```

测试只使用临时 Git 仓库、测试凭证和本机随机端口上的模拟 HTTP 服务，不连接真实实例。脚本和报告素材不是通用脱敏工具，对外报告仍应按用户要求匿名化业务信息。

项目采用 [MIT License](LICENSE)。
