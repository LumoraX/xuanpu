# 编写业务包

业务包是你交给平台的全部东西：一个目录，里面是 skill（给模型的说明书）、连接器声明（模型能调哪些外部能力）、策略（哪些操作要人批准、预算多少）和评测用例。会话调度、恢复、隔离、存储、审计都由平台负责，你不用写，也不能写——业务包里没有在平台进程内运行的代码。

本文用仓库里的样例 [`examples/support-desk`](../../examples/support-desk/) 讲解。字段的完整定义见[业务包规范](../architecture/package-spec.md)。

## 目录结构

```
support-desk/
├── xuanpu.yaml                  清单：包的身份、入口、连接器、策略、评测
├── apis/
│   └── tickets.yaml             OpenAPI 文档：要接入的外部接口
└── skills/
    ├── triage/
    │   ├── SKILL.md             入口 skill
    │   └── scripts/sla.sh       沙盒里运行的脚本
    └── refund-policy/
        └── SKILL.md             子 skill
```

## 第一步：写 skill

一个 skill 是一个目录，里面有一份 `SKILL.md`：开头两行 `---` 之间是 YAML 元数据，后面的正文就是给模型的指令。

```markdown
---
name: triage
description: 工单分诊：查工单、给出处理时限、按用户要求改派优先级
version: 1.0.0
type: composite
entry: true
child_skills: [refund-policy]
tool_names:
  - api__tickets__getTicket
  - api__tickets__updateTicket
---
# 工单分诊

你是客服团队的工单助手。……
```

| 字段 | 含义 |
| --- | --- |
| `name` | skill 的标识，包内唯一 |
| `description` | 一句话说明。父 skill 靠它决定要不要调用这个子 skill，要写清楚「什么时候该找我」 |
| `type` | `atomic`：一份知识或规则，被别的 skill 调用，不能当入口，也不能声明 `child_skills`、`tool_names`、`model`。`composite`：能调子 skill、能用工具，可以当入口；`child_skills`、`tool_names`、`scripts` 至少要有一样 |
| `entry` | `true` 表示可以作为会话入口。还要同时列进清单的 `spec.skills.entries` |
| `child_skills` | 可以调用的子 skill。模型经内置的 `call_skill` 工具调用，调用子 skill 不需要审批 |
| `tool_names` | 这个 skill 里模型能用的连接器工具。**没写进来的连接器工具模型看不到**，哪怕清单里声明了。内核自带的 `call_skill`、`read_skill` 总是可用，声明了 `scripts` 就自动可用 `run_script`，这三个不用写 |
| `scripts` | 脚本声明，见下文 |
| `model` | 只在取值是档位名（`fast` / `balanced` / `deep`）时生效，用来给个别 skill 换档。写具体模型名会被忽略 |

skill 的加载与执行由 taifeng 内核负责，元数据的完整语义以内核文档为准。发布时平台会让内核实际加载一遍，加载不了的包发布不出去。

写指令时有两条来自实践的建议：

- 把需要审批的动作告诉模型，并说明被拒绝后怎么办。否则模型可能换个说法反复重试。
- 工具能查到的事实让它去查，别写进指令；指令里只写判断规则和口径。

## 第二步：接外部能力

模型要碰外部世界，只有这几条路。它们都在平台进程之外，都要过闸门、留审计。

| 形态 | 适合 | 在哪运行 |
| --- | --- | --- |
| HTTP 连接器 | 已有 HTTP 接口和 OpenAPI 文档的内部系统。接入成本最低 | 平台直接发请求 |
| MCP 连接器（stdio） | 已经有 MCP server 程序，或者需要 HTTP 表达不了的交互 | 平台在连接器沙盒里启动的子进程 |
| 远端 MCP 连接器 | MCP server 已经部署在别处（企业自建或第三方提供） | 平台经 MCP Streamable HTTP 直接请求 |
| 沙盒脚本 | 纯计算、数据整理、调用命令行工具 | 隔离的脚本沙盒 |

### HTTP 连接器

把接口的 OpenAPI 3.x 文档放进包里，在清单里声明：

```yaml
connectors:
  - name: tickets
    transport: http
    openapi: apis/tickets.yaml
    auth: {type: bearer, secret: tickets-token}
    timeoutSeconds: 15
    tools:
      - {name: listTickets}
      - {name: getTicket}
      - {name: updateTicket, effect: write}
```

文档里的每个 operation 变成一个工具，名字是 `api__<连接器名>__<operationId>`，比如 `api__tickets__getTicket`。路径、查询、头参数平铺成工具参数，JSON 请求体放在 `body` 参数下。

要点：

- **白名单**：`tools` 里列了谁，谁才会变成工具；留空表示文档里的 operation 全部开放。接口文档通常很大，建议总是写白名单。
- **副作用分类**（`effect`）：`read`、`write`、`external` 三种，决定要不要审批。不写时 GET / HEAD 算 `read`，其余算连接器的默认值（默认 `external`，最保守）。`PATCH`、`POST` 这类修改数据的操作，如果只影响你们自己的系统，标成 `write`。
- **凭据**：`auth.secret` 写的是密钥的名字。取值由租户管理员存进平台（`PUT /v1/secrets/<名字>`），发请求时才取出来；模型看不到，审计里也没有。支持 `bearer`、`basic`、自定义头、查询参数四种放法。
- **目标地址**：由清单的 `baseUrl` 或文档的 `servers` 定死。模型只能填 operation 声明过的参数，改不了主机，也不能借参数改写路径。
- 响应体超过 1 MiB 会被截断；4xx / 5xx 会连同响应体交给模型，让它自己判断下一步。

不支持的写法（cookie 参数、非 JSON 请求体、外部 `$ref`、OAuth2 换取令牌）见[业务包规范](../architecture/package-spec.md#http-连接器)。

### MCP 连接器（stdio）

```yaml
connectors:
  - name: crm
    command: ["python3", "servers/crm_mcp.py"]   # 随包提交的 MCP server
    env:
      CRM_TOKEN: {secret: crm-token}     # 密钥引用
      CRM_REGION: {value: cn}            # 字面量
    effect: external
    tools:
      - {name: search_customer, effect: read}
      - {name: update_ticket, effect: write}
```

平台在组装运行时的时候启动 `command`，工作目录是业务包的根目录。工具名是 `mcp__<连接器名>__<工具名>`。

进程跑在这个运行时的**连接器沙盒**里（与脚本的沙盒分开），看到的环境：

| 项 | 情况 |
| --- | --- |
| 能读 | 业务包目录和系统运行时目录（`/usr` 等）；本机后端另加部署方放开的目录（`sandbox.connector_readable_roots`）。读不到平台的数据目录、别的租户的文件 |
| 能写 | 只有 `$HOME`、`$TMPDIR`、`$WORKSPACE`，三个都指向这个运行时的连接器工作目录。它是同一个业务包版本的所有会话共用的，在平台节点本地，不跨实例、不备份、不保证还在 |
| 网络 | 可以出网，不受 `policy.sandbox.network` 影响（那一项只管脚本） |
| 环境变量 | 只有 `PATH`、`LANG`、`HOME`、`TMPDIR`、`WORKSPACE` 和 `env` 里声明的那些，不继承平台进程的环境。`env` 不能设置 `LD_*`、`DYLD_*` 这类动态链接器变量，校验时报错 |
| 解释器与依赖 | `PATH` 只有系统目录。本机后端下用系统目录里的解释器（如 `/usr/bin/python3`），装在别处的请部署方把它与依赖所在的目录加进 `connector_readable_roots`（Python 虚拟环境要虚拟环境目录与背后的解释器目录两个）；容器后端下用部署方的连接器镜像（`sandbox.connector_image`），里面要装好你需要的运行时（如 `node`） |
| 启动时限 | 起进程、握手、列出工具要在部署配置的 `limits.connector_startup_seconds`（默认 30 秒）内完成。`uvx`、`npx` 这类第一次运行时现场下载依赖的写法容易超时：依赖要预装进节点或镜像，或者随包提交 |

要点：

- 白名单里声明了、但 server 实际没有提供的工具，会让会话起不来（`503 runtime_unavailable`），而不是悄悄少一个工具。
- **同一个业务包里的 stdio 连接器共用一个连接器沙盒与工作目录**，彼此读得到对方写的文件；容器后端与 macOS 本机后端下还能看到彼此的环境变量（含注入的密钥）。不信任彼此的连接器，放进不同的业务包。
- 密钥只注入进程环境。连接器拿到之后发给谁、写到哪里，平台管不了；起不来时平台日志里有按值脱敏后的 stderr 末尾，可以请平台管理员帮忙看。

### 远端 MCP 连接器

MCP server 已经部署在别处、经 MCP Streamable HTTP 访问时，不必让平台起进程：

```yaml
connectors:
  - name: docs
    transport: remote
    url: https://mcp.example.com/mcp
    auth: {type: bearer, secret: docs-token}
    timeoutSeconds: 30
    tools:
      - {name: search, effect: read}
```

工具名与 stdio 连接器一样是 `mcp__<连接器名>__<工具名>`。字段的完整规则见[业务包规范](../architecture/package-spec.md#远端-mcp-连接器)。要点：

- **地址**：用 `https://`。`http://` 只宜试用，凭据会明文经过网络（校验会提醒）。地址的路径与查询串里不要放凭据（带查询串校验会提醒），凭据写进 `auth`。
- **凭据**：只支持放在请求头里的 `bearer` 与 `header` 两种，只写密钥名。平台在**每次请求发出时**从保管库现取，所以轮换即时生效；凭据只发往与 `url` 同源（协议、主机、端口都相同）的请求，不跟随重定向。不支持 OAuth 换取令牌。
- **出口**：请求从平台节点发出，不进沙盒。平台不读环境里的代理与证书配置，用私有 CA 签发证书的 server 暂时接不了。平台也不限制 `url` 指向哪里（包括内网与云元数据地址），回包会交给模型——能访问到哪里由部署的网络策略决定。
- **回包**：远端回的文本（成功的结果、错误）交给模型与审计之前按值脱敏，凭据的明文原样或按常见转义出现时会被换掉。
- **工具在组装运行时时定下**：之后 server 增减工具（`tools/list_changed`）不会生效，要等运行时空闲回收、重新组装。server 发起的 elicitation、sampling、roots 请求不支持（回 `-32601`）。
- **副作用分类自己标**：平台不采信 server 自报的只读标注，不标就是连接器的默认分类（默认 `external`）。
- **超时**：单次请求按 `timeoutSeconds` 与平台的连接器超时取小的，握手与列工具也按它算，整个连上的过程还受 `limits.connector_startup_seconds` 约束。超时的调用平台会通知远端取消，但远端可能已经执行了——有副作用的工具要能承受重复或不确定的结果。
- 远端重启后原来的 MCP 会话失效（404），平台不会重新握手，调用一直失败，直到运行时空闲回收后重新组装。
- 需要 `xuanpu-spec` 2026.10.2.7 或更新的版本才认得 `transport: remote`。

### 沙盒脚本

在 `SKILL.md` 里声明：

```yaml
scripts:
  - name: sla
    path: scripts/sla.sh
    language: shell              # shell 或 python
    description: 按优先级给出处理时限（小时）
    timeout_seconds: 10
    args_schema:
      type: object
      properties:
        priority: {type: string, enum: [low, normal, high]}
      required: [priority]
```

声明了脚本，模型就能经内置的 `run_script` 工具调用它。脚本看到的环境：

| 项 | 情况 |
| --- | --- |
| 参数 | 按 `args_schema.properties` 的声明顺序作为位置参数传入（`$1`、`$2`… 或 `sys.argv[1:]`） |
| 工作目录 | 脚本所在的目录 |
| 能读 | 业务包目录和系统运行时目录（`/usr` 等）。读不到平台的数据目录、别的租户的文件、宿主机的用户目录 |
| 能写 | 只有 `$WORKSPACE`（`$HOME`、`$TMPDIR` 也指向它） |
| 网络 | 默认不通。清单里 `policy.sandbox.network: true` 才放开 |
| 环境变量 | 只有 `PATH`、`LANG`、`HOME`、`TMPDIR`、`WORKSPACE`。**拿不到任何密钥** |
| 超时 | 到 `timeout_seconds` 即被杀掉 |
| 解释器 | shell 脚本用 `/bin/sh`，Python 脚本用沙盒里的 `python3`（容器后端取决于部署方选的镜像） |

两点要留意：

- **工作区是同一个业务包版本下所有会话共用的**，不是每个会话一份。不要把某个客户的数据留在工作区里，也不要假设上一次写的文件还在或不在。
- 脚本需要凭据才能做的事（调内部接口），请改用连接器。凭据不会进沙盒。

清单里 `policy.sandbox.shell: true` 会额外提供两个工具 `run_in_background` 与 `wait_for_task`（同样要写进 `tool_names` 才可见），让模型在沙盒里自己写命令执行。这比预先写好的脚本自由得多，审批也更严（见下表），没有明确需要不要开。

## 第三步：定策略

```yaml
policy:
  approval: standard
  budget:
    maxSessionTokens: 200000
    maxIterations: 20
  sandbox:
    network: false
    shell: false
```

### 审批强度

你只选强度，规则由平台生成：

| 动作 | `strict` | `standard` | `relaxed` |
| --- | --- | --- | --- |
| 调用子 skill | 放行 | 放行 | 放行 |
| 沙盒里跑脚本 | 审批 | 放行 | 放行 |
| 沙盒里执行命令（`shell: true` 时） | 审批 | 审批 | 放行 |
| 连接器工具：`read` | 审批 | 放行 | 放行 |
| 连接器工具：`write` | 审批 | 审批 | 放行 |
| 连接器工具：`external` | 审批 | 审批 | 审批 |
| 其余一切 | 审批 | 审批 | 审批 |

「审批」的意思是：这一轮对话停下来，把待办交给调用方；调用方送回批准或拒绝后再继续。等待期间不占用平台资源，等多久都可以。

所以副作用分类要认真标：标得太松，不该自动执行的操作会直接执行；标得太紧，用户会被审批淹没。拿不准就不标，默认的 `external` 在任何强度下都要审批。

### 预算

| 字段 | 含义 | 不写时 |
| --- | --- | --- |
| `maxSessionTokens` | 一个会话累计能用多少 token | 用平台上限 |
| `maxIterations` | 一轮对话里模型与工具最多往返几次 | 用平台上限 |

只能比平台上限低，写得更高也按平台上限算。

### 模型档位

```yaml
model:
  tier: balanced      # fast | balanced | deep
```

业务包不写模型名。每个档位对应哪个模型，由平台管理员在部署配置里定。

## 第四步：写评测

```yaml
evals:
  - name: refund-window
    entry: triage
    input: 退款多久到账？
    expect:
      contains: ["7 个工作日"]
      notContains: ["无法回答"]
      maxTokens: 4000
```

每个用例开一个新会话，把 `input` 发给 `entry`，等这一轮结束后检查：

- 回复里要有 `contains` 的每个片段，不能有 `notContains` 的任何片段；
- 本轮消耗的 token 不超过 `maxTokens`；
- 本轮必须正常完成。**停在审批点算不通过**——评测没有人来批准，所以用例要避开需要审批的路径，或者单独用一个审批更松的版本来测。

评测走的是真实路径：真的调模型、真的调连接器、真的计入用量和审计。对外部系统有写操作的用例，请指向测试环境。

没有评测用例的包可以发布，但校验时会提醒。

## 第五步：校验、发布、上线

```bash
xuanpu validate ./support-desk                 # 本地校验
xuanpu pack ./support-desk -o support-desk.zip # 打包（先校验）
```

校验会查：字段是否合法（拼错的字段名会报错）、入口 skill 是否存在且声明了 `entry: true`、skill 引用的连接器工具是否在清单和白名单里、OpenAPI 文档能否解析、白名单里的 operation 是否存在。

打包会跳过 `.git`、`__pycache__`、`.venv`、`node_modules`、`.env`、`.DS_Store`；包里不允许符号链接。上限是归档 64 MiB、解包后 256 MiB、单文件 32 MiB、5000 个文件。

发布（需要租户管理员的凭据）：

```bash
# 只发布，不生效
curl -X POST localhost:8420/v1/packages -H "Authorization: Bearer $KEY" --data-binary @support-desk.zip

# 先拿这个版本跑评测
xuanpu eval --key $KEY --package support-desk --version 1.1.0

# 通过了再设为生效版本
curl -X POST localhost:8420/v1/packages/support-desk/activate \
     -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d '{"version": "1.1.0"}'
```

版本规则：

- **发布后不可变**。同名同版本不能再发，改一个字也要升版本号。
- **会话在创建时钉住版本**。切换生效版本只影响之后新建的会话，进行中的会话继续用原来的版本。
- **回滚就是把生效版本指回旧版本**，用同一个 `activate` 接口。
- 建会话时可以显式指定 `version`，用来灰度或对比。

## 密钥要在用之前配好

发布时平台不检查密钥是否已配置。包引用的密钥缺失时，第一次对话会失败：

```json
{"error": {"code": "secret_missing", "message": "密钥 'tickets-token' 尚未配置"}}
```

补上密钥后重试即可，不用重新发布。密钥轮换也一样：HTTP 连接器与远端 MCP 连接器每次请求现取，立即生效；stdio 连接器的密钥是启动进程时注入的，要等运行时空闲回收后重新组装才会用上新值。

## 常见问题

| 现象 | 原因 |
| --- | --- |
| 校验报「引用了工具 …，但清单里没有连接器 …」 | `tool_names` 里的工具名拼错了，或者连接器名对不上。HTTP 连接器的前缀是 `api__`，stdio 与远端 MCP 连接器是 `mcp__` |
| 模型说它没有某个工具 | 工具没写进当前 skill 的 `tool_names` |
| 每次调用都要审批 | 工具的副作用分类是默认的 `external`。只读操作标成 `read`，内部写操作标成 `write` |
| 对话返回 `503 runtime_unavailable` | MCP server 起不来（连接器沙盒里读不到解释器、镜像里没装运行时、第一次启动超时、想写工作目录之外的地方）、远端 MCP 连不上、没有提供白名单里的工具，或者沙盒后端不可用。细节在平台的日志里 |
| 校验报 `env` 不能设置动态链接器变量 | 连接器的 `env` 里有 `LD_*` 或 `DYLD_*`。这类变量不允许，换别的办法传配置 |
| 脚本报找不到命令 | 沙盒里的 `PATH` 只有系统目录；容器后端下取决于镜像里装了什么 |
| 评测用例结果是 `suspended` | 用例走到了需要审批的操作 |
