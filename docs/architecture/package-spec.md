# 业务包规范（活文档）

> 规范版本 `xuanpu/v1`。权威定义在 `packages/xuanpu-spec/src/xuanpu_spec/manifest.py`；
> `xuanpu schema` 可以导出 JSON Schema。
>
> 本文是字段与规则的参考。怎么一步步写出一个业务包见[编写业务包](../guides/writing-packages.md)，
> 完整的样例在 [`examples/support-desk`](../../examples/support-desk/)。

业务包是一个目录，根下有清单 `xuanpu.yaml`：

```
support-desk/
├── xuanpu.yaml
└── skills/
    ├── triage/
    │   ├── SKILL.md
    │   └── scripts/stamp.sh
    └── faq/
        └── SKILL.md
```

## 清单

```yaml
apiVersion: xuanpu/v1
kind: Package
metadata:
  name: support-desk          # 小写字母、数字、连字符
  version: 1.2.0              # 语义化版本
  description: 客服工单助手
spec:
  skills:
    dir: skills               # 默认 skills
    entries: [triage]         # 可以作为会话入口的 skill，至少一个
  model:
    tier: balanced            # fast | balanced | deep，默认 balanced
  policy:
    approval: standard        # strict | standard | relaxed，默认 standard
    budget:
      maxSessionTokens: 200000
      maxIterations: 40
    sandbox:
      network: false          # 脚本是否需要出网，默认否
      shell: false            # 是否开放沙盒内的后台命令工具，默认否
  connectors:
    - name: crm                 # stdio：平台启动的 MCP server 进程
      command: ["uvx", "crm-mcp"]
      env:
        CRM_TOKEN: {secret: crm-token}   # 密钥引用，明文由平台注入
        CRM_REGION: {value: cn}          # 字面量
      effect: external        # 默认的副作用分类：read | write | external，默认 external
      tools:                  # 工具白名单；留空表示开放全部
        - {name: search_customer, effect: read}
        - {name: update_ticket}
    - name: erp                 # http：按 OpenAPI 文档直接调用的接口
      transport: http
      openapi: apis/erp.yaml    # 包内的 OpenAPI 3.x 文档（YAML 或 JSON）
      baseUrl: https://erp.internal/api   # 不给则用文档 servers 里的第一个
      auth: {type: bearer, secret: erp-token}
      timeoutSeconds: 30
      tools:                    # 按 operationId；留空表示开放文档里的全部 operation
        - {name: createOrder, effect: write}
  evals:
    - name: greet
      entry: triage
      input: 你好
      expect:
        contains: ["您好"]         # 回复必须包含的片段
        notContains: ["无法处理"]  # 回复不得包含的片段
        maxTokens: 2000           # 本轮 token 上限
```

## 字段参考

清单里的键用驼峰（`maxSessionTokens`、`baseUrl`）。没标「必填」的都可省略。

### `metadata`

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `name` | 必填。小写字母开头，只含小写字母、数字、连字符，最长 63 | 包名，租户内唯一 |
| `version` | 必填。语义化版本 `主.次.修订`，可带预发布后缀（`1.2.0-rc.1`），不支持构建元数据（`+…`） | 发布后不可变 |
| `description` | 最长 500 个字符 | |

### `spec.skills`

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `dir` | `skills` | skill 目录在包内的相对路径 |
| `entries` | 必填，至少一个，不能重复 | 可以作为会话入口的 skill。第一个是建会话时的默认入口 |

### `spec.model`

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `tier` | `balanced` | `fast`、`balanced`、`deep`。对应哪个模型由平台的部署配置决定 |

### `spec.policy`

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `approval` | `standard` | `strict`、`standard`、`relaxed`。各强度下哪些动作要审批，见[编写业务包](../guides/writing-packages.md#审批强度) |
| `budget.maxSessionTokens` | 平台上限 | 一个会话累计的 token 上限，至少 1000。高于平台上限时按平台上限 |
| `budget.maxIterations` | 平台上限 | 一轮对话里模型与工具往返次数的上限，1–500。高于平台上限时按平台上限 |
| `sandbox.network` | `false` | 沙盒里的脚本与命令能否出网 |
| `sandbox.shell` | `false` | 是否提供在沙盒里执行后台命令的工具（`run_in_background`、`wait_for_task`） |

### `spec.connectors[]`

连接器名在包内不能重复。

| 字段 | 适用 | 默认 | 说明 |
| --- | --- | --- | --- |
| `name` | 全部 | 必填 | 命名规则同包名。出现在工具名里 |
| `transport` | 全部 | `stdio` | `stdio`：平台启动的 MCP server 进程；`http`：按 OpenAPI 文档调用的接口 |
| `effect` | 全部 | `external` | 这个连接器的工具默认的副作用分类：`read`、`write`、`external` |
| `tools` | 全部 | 空（开放全部） | 白名单。每项 `{name, effect}`，`effect` 省略时用默认分类；不能重名 |
| `command` | stdio | 必填 | 启动 MCP server 的命令，字符串数组。工作目录是业务包根目录 |
| `env` | stdio | 空 | 传给进程的环境变量。每项是 `{value: 字面量}` 或 `{secret: 密钥名}`，二选一 |
| `openapi` | http | 必填 | OpenAPI 3.x 文档在包内的相对路径（YAML 或 JSON，最大 8 MiB） |
| `baseUrl` | http | 文档 `servers` 的第一个绝对地址 | 接口的基地址，`http://` 或 `https://` 开头 |
| `auth` | http | 无（接口不需要凭据） | 见下文「HTTP 连接器」 |
| `timeoutSeconds` | http | `30` | 单次调用的超时，最大 600。另受平台的连接器超时约束，取小的 |

把另一种传输的字段写进来（比如 stdio 连接器写了 `openapi`）会报错。

### `spec.evals[]`

用例名在包内不能重复。

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `name` | 必填，命名规则同包名 | |
| `entry` | 必填，必须在 `skills.entries` 里 | 用哪个入口 |
| `input` | 必填，非空 | 发给模型的那句话 |
| `expect.contains` | 字符串数组 | 回复必须包含的片段 |
| `expect.notContains` | 字符串数组 | 回复不得包含的片段 |
| `expect.maxTokens` | 正整数 | 本轮消耗的 token 上限 |

不写 `expect` 的用例只检查这一轮是否正常完成。

### 密钥名

`env` 里的 `secret` 与 `auth.secret` 写的是密钥名，命名规则同包名。取值由租户管理员经 `PUT /v1/secrets/<名字>` 存进平台。

## 规则

- **多余的字段一律拒绝**：拼错的字段名在校验时报错。
- **只声明需求，不写实现细节**：模型只选档位；审批只选强度；凭据只写密钥名。skill 的 `model` 字段只在取值是档位名时生效。
- **工具可见性**：模型只看得到当前 skill 在 `tool_names` 里点名的连接器工具；内核自带的 `call_skill`、`read_skill` 总是可用，skill 声明了 `scripts` 就自动可用 `run_script`。
- **连接器工具名**：stdio 连接器是 `mcp__<连接器名>__<工具名>`，HTTP 连接器是 `api__<连接器名>__<operationId>`；skill 在 `tool_names` 里按这个名字引用。引用了清单里没有的连接器、前缀与传输方式不符、或白名单之外的工具，校验不通过。
- **入口**：清单 `entries` 里的 skill 必须在 SKILL.md 里声明 `entry: true`。
- **版本**：发布后不可变，改动必须发新版本。不兼容的规范变更升 `apiVersion`。

## HTTP 连接器

企业里大量的内部接口不必逐个包成 MCP server：把 OpenAPI 文档放进业务包，声明一个 `transport: http` 的连接器，文档里每个 operation 就是一个工具。

| 项 | 规则 |
| --- | --- |
| 工具参数 | 路径、查询、头参数与请求体平铺成一个对象，请求体在 `body` 键下；`$ref` 已展开 |
| 副作用分类 | 逐 operation 声明优先；否则 GET / HEAD 是 `read`，其余用连接器的 `effect`（默认 `external`） |
| 凭据 | `auth.type`：`bearer`、`header`（配 `header`）、`query`（配 `name`）、`basic`（密钥取值形如 `用户:口令`）；取值只写密钥名 |
| 出口范围 | 主机由 `baseUrl`（或文档 `servers`）定死；路径参数整体转义；模型只能填声明过的参数 |
| 响应 | 不跟随重定向；4xx / 5xx 连同响应体交给模型并记为出错；响应体上限 1 MiB，非文本类型只给占位 |
| 不支持 | cookie 参数（忽略）、非 JSON 请求体、外部 `$ref`、`Authorization` 等由平台设置的头参数、需要换取令牌的 OAuth2 流程 |

接口很多时不会撑爆上下文：连接器声明多少 operation 都可以，模型只看得到当前 skill 在 `tool_names` 里点名的那几个。

限流、熔断、重试、双向 TLS 不在平台里做，交给企业已有的 API 网关。

平台不限制 `baseUrl` 可以指向哪里（包括内网地址）：出网范围靠部署的网络策略约束。设计理由见 [ADR 0008](../decisions/0008-declarative-http-connectors.md)。

## 归档

业务包以 zip 归档发布，清单在归档根下。

- 打包是确定性的：同样的内容总是得到逐字节相同的归档，归档的 SHA-256 是版本的指纹。
- 打包跳过 `.git`、`__pycache__`、`.venv`、`node_modules`、`.env`、`.DS_Store`；包里不允许符号链接。
- 解包把归档当作不可信输入：路径逃逸、符号链接、重复条目、体积超限、内容损坏都会被拒绝。
- 上限：归档 64 MiB，解包后 256 MiB，单文件 32 MiB，5000 个文件。

## 校验

`xuanpu validate <目录>` 做两层检查：清单本身是否合法；清单与包内容是否对得上（入口存在、工具引用有效）。发布时平台还会让内核实际加载一遍 skill。
