# 业务包规范（活文档）

> 规范版本 `xuanpu/v1`。权威定义在 `packages/xuanpu-spec/src/xuanpu_spec/manifest.py`；
> `xuanpu schema` 可以导出 JSON Schema。

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
        contains: ["您好"]
        maxTokens: 2000
```

## 规则

- **多余的字段一律拒绝**：拼错的字段名在校验时报错。
- **只声明需求，不写实现细节**：模型只选档位；审批只选强度；凭据只写密钥名。skill 的 `model` 字段只在取值是档位名时生效。
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
| 不支持 | cookie 参数（忽略）、非 JSON 请求体、外部 `$ref`、`Authorization` 等由平台设置的头参数 |

接口很多时不会撑爆上下文：连接器声明多少 operation 都可以，模型只看得到当前 skill 在 `tool_names` 里点名的那几个。

限流、熔断、重试、双向 TLS 不在平台里做，交给企业已有的 API 网关。

## 归档

业务包以 zip 归档发布，清单在归档根下。

- 打包是确定性的：同样的内容总是得到逐字节相同的归档，归档的 SHA-256 是版本的指纹。
- 打包跳过 `.git`、`__pycache__`、`.venv`、`node_modules`、`.env`、`.DS_Store`；包里不允许符号链接。
- 解包把归档当作不可信输入：路径逃逸、符号链接、重复条目、体积超限、内容损坏都会被拒绝。
- 上限：归档 64 MiB，解包后 256 MiB，单文件 32 MiB，5000 个文件。

## 校验

`xuanpu validate <目录>` 做两层检查：清单本身是否合法；清单与包内容是否对得上（入口存在、工具引用有效）。发布时平台还会让内核实际加载一遍 skill。
