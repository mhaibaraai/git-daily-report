# 个人配置

远程流程通过 `--config <文件.json>` 或进程环境中的 `GDR_CONFIG` 指定配置；显式参数优先。`--profile` 默认选择 `default`。同一参数可以放在命令前或子命令后。

将 [配置模板](../assets/config.example.json) 复制到自己的配置目录，再填写实际设置。不要把个人实例配置提交到公开仓库；技能示例不包含真实地址、项目或凭据。`settings.local.json` 等 `*.local.json` 文件已被 Git 忽略。

## 直接填写令牌

在个人配置的 `gitlab` 和 `eom` 对象中，将 `token_env` 字段名改为 `token`，值填写对应的实际令牌，其他字段保留。例如 GitLab 的凭证字段为：

```json
"token": "REPLACE_WITH_GITLAB_TOKEN"
```

EOM 同样使用 `token`，填入 EOM 令牌。这种方式无需导出环境变量；不要同时保留该服务的 `token_env`。令牌必须为不含空白的非空 ASCII 字符串；配置对象的字符串表示、命令输出、报告和提交状态不会附带配置令牌。

也可以保留 `token_env`，其值是环境变量的名字，不是令牌，不带 `$`。两项都省略时沿用该服务的默认环境变量名。脚本不会把填错在 `token_env` 中的令牌自动当作明文令牌使用。

## 配置结构

顶层仅包含 `profiles`，其值是以名称为键的配置对象。每个配置支持：

| 字段 | 含义 |
| --- | --- |
| `workspace` | 必填，报告和提交状态所在目录；不能位于技能安装目录内，也不能是其上级目录 |
| `archive_dir` | 可选，Markdown 核查稿固定归档目录；默认配置文件旁的 `reports/`，按年份和实例/身份分目录，保留不同正文版本 |
| `timezone` | 必填，系统支持的 IANA 时区；`UTC` 不需要系统时区数据库 |
| `daily_hours` | EOM 生成及提交必填，0–24 之间的正数；同时作为每日估算目标与上限，无默认公司标准 |
| `commit_slack_days` | EOM 提交取材窗口向前后扩展的天数，默认 3，允许 0–31；不扩展工单填报日期 |
| `gitlab` | GitLab 设置；使用远程提交素材时需要 |
| `eom` | EOM 适配器设置；EOM 模式需要 |
| `mappings` | 工单与 GitLab 项目、分支的可选对应关系，默认空数组 |

相对 `workspace`、`archive_dir` 路径以配置文件目录为基准，支持 `~`。当前项目的默认归档目录为 `reports/`，已加入 `.gitignore`；自定义到其他仓库内目录时，应自行补充对应忽略规则。状态按实例和填报身份隔离；同一身份的多个配置应使用同一工作目录，让重叠区间共享提交保护。改变时区后，原时区报告不会被自动解释或覆盖；应使用独立工作目录重新生成。

## GitLab 设置

- `base_url`：必填，实例地址，支持路径前缀；禁止地址内携带用户名、密码、查询参数或片段。
- `token`：可选，直接填写 GitLab 令牌，需要具备读取相关 API 的权限；与 `token_env` 互斥。
- `token_env`：可选，令牌环境变量的名称，未设置 `token` 时默认 `GITLAB_TOKEN`。此处只能写变量名。
- `projects`：可选数组，每项为 `{"id": "group/project", "ref": "feature"}`，`id` 也可以是项目数字 ID，`ref` 可省略。
- `authors`：可选的作者姓名或邮箱数组，使用忽略大小写的精确匹配；省略时从令牌当前账号读取候选身份。无法识别时要求补充，不自动纳入所有作者。
- `all_authors`：明确读取团队记录时设置为 `true`，不能同时提供 `authors`；报告不得表述为本人全部完成。
- `max_pages`：每次列表读取的页数上限，默认 20，每页 100 条；达到上限返回警告。
- `merge_limit`：单次合并展开的提交上限，默认 50；达到上限返回警告。
- `allow_http`：默认 `false`。只有显式设为 `true` 才允许 HTTP；不提供关闭 TLS 证书验证的选项。

## EOM 设置

- `base_url`：必填，EOM 实例地址，验证规则同 GitLab。
- `worker_id`：必填，EOM 中填报人的标识。脚本核对排期中的 `ewlWorker`，身份不符时停止，不代替其他人填报。
- `token`：可选，直接填写 EOM 令牌；与 `token_env` 互斥。
- `token_env`：可选，未设置 `token` 时默认 `EOM_TOKEN`，仅指定环境变量名。代理不读取浏览器会话或自动抓取凭据。
- `allow_http`：与 GitLab 相同。
- `protocol`：仅当部署协议不同且已核实时覆盖；支持 `path`、`application`、`tenant`、`dir`、`menuId`、`modelId`、`datasetId`。这些是适配器协议字段，不包含公司实例信息；不支持在协议对象中设置任意请求头或令牌。

协议默认值：路径 `/core/busi/exec`，`application=platform`、`tenant=default`，业务字段 `dir=manHourCenter`、`menuId=ScheduleP`、`modelId=workOrders`、`datasetId=2`。

## 工单映射

推荐按 EOM 的“项目名称”列配置 `project_name`，将同一项目下不同标题、不同 ID 的工单关联到 `gitlab_project`。每项包含 `gitlab_project` 和可选 `ref`，并且只使用一个选择字段：`project_name`、`order_title` 或 `order_id`。

```json
[
  {"project_name": "示例数字孪生项目", "gitlab_project": "group/project", "ref": "feature"}
]
```

`project_name` 使用项目完整名称精确匹配，保留名称中的括号、大小写和空格；它对应“项目名称”，不是“任务标题”或“工单标题”。

特殊工单可单独使用 `order_id` 或 `order_title` 覆盖项目映射。优先级为工单 ID、工单标题、项目名称；使用最高优先级匹配到的全部条目，允许一个工单对应多个代码仓库。匹配到的项目独立拉取，不要求存在推送事件。

配置拒绝未知字段、重复 JSON 键、非有限数及同时设置 `token` 与 `token_env`。脚本不加载 `.env`，也不提供模型调用配置。旧版应用报告和待核实记录不会自动迁移、覆盖或删除。
