# 架构总览（活文档）

> 本目录只描述**当前生效的设计**；为什么这么定见 [../decisions/](../decisions/README.md)。
>
> 相关：[内核对接口缺口清单](kernel-seam-backlog.md) · [HTTP 接口](http-api.md) · [业务包规范](package-spec.md)
>
> 怎么用平台（而不是它怎么设计的）见[使用手册](../guides/README.md)。

## 现状

第一版：业务包规范、平台服务（控制面 + 运行时 + HTTP 接口 + 评测运行器）、命令行、Python 客户端、控制台五个包可用。MCP 网关已评估，建议采用现成方案。

## 分层与依赖方向

```
业务包（声明 + 进程外扩展）
        │ 发布
xuanpu
  ├─ xuanpu-spec          业务包规范（对外契约，不依赖其他组件，也不依赖 taifeng）
  ├─ xuanpu-cli           校验 / 打包 / 生成凭据 / 迁移 / 启动服务 / 运行评测
  ├─ xuanpu-sdk-py        Python 客户端（只依赖 HTTP 库）
  ├─ xuanpu-console       控制台：纯静态页面，调用 HTTP 接口；服务装了本包就挂在 /console/
  └─ xuanpu-server        控制面 + 运行时 + HTTP 接口 + 评测运行器
        │ 只依赖稳定层
taifeng-sandbox · taifeng-stores
        │
taifeng
```

依赖只能自上而下；下层永远不 import 上层，也不出现上层概念。

## 四个平面

按 [ADR 0004](../decisions/0004-planes-and-effect-gates.md)，平台分四个平面。下表是各平面在代码里的落点：

| 平面 | 职责 | 模块 |
| --- | --- | --- |
| 计算面 | 组装并承载内核引擎、会话租约、事件分发 | `runtime/assembler.py`、`runtime/sessions.py`、`runtime/events.py` |
| 效应面 | 副作用的出口：沙盒、连接器（MCP 与 HTTP）、模型 | `runtime/sandbox.py`、`runtime/connectors.py`、`runtime/http_api.py`、`runtime/models.py`、`runtime/approval.py` |
| 状态面 | 会话消息、线程目录、长期记忆 | `bootstrap.py` 里的 `PostgresStateBackends`，实现来自 taifeng-stores。长期记忆的槽位在（`StateBackends.memory_store`），但配置文件尚未接入 |
| 治理面 | 租户、凭据、业务包版本、密钥、配额、审计、用量、评测 | `platform.py`、`tenancy.py`、`packages.py`、`secrets.py`、`quota.py`、`audit.py`、`evals.py`、`controlplane/` |

所有外部依赖（存储、密钥保管库、模型客户端工厂、沙盒供给方、状态面后端）都经 `Platform` 的构造参数注入；平台代码不读环境变量，环境变量只在命令行入口读取。

## 一轮对话的路径

```
POST /v1/sessions/{id}/messages
  │ 认证 → Principal（带租户）
  ▼
SessionManager.send_message
  ├─ 配额：租户可用？当天用量未超？
  ├─ 取会话（按租户过滤；别的租户的会话按不存在处理）
  ├─ 取得租约（别的实例持有且未到期 → 409，带上持有者）
  ├─ RuntimeAssembler.get —— 首次使用时组装：
  │     连接器：stdio —— 取密钥 → 启动 MCP server → 列工具 → 按白名单筛
  │             http  —— 读包内 OpenAPI 文档 → 核对密钥已配置 → 按白名单筛 operation
  │     审批：按业务包的审批强度与工具的副作用分类生成权限策略
  │     沙盒：受限读 + 只写工作区 + 按声明决定是否出网
  │     模型：按档位路由的客户端
  │     → taifeng.EnginePool.create(...)
  ├─ pool.get_or_create（有线程标识则从存储续接）
  ├─ EngineEventHub.start —— 建立全量事件订阅
  └─ engine.submit → 同步登记本轮的事件流
  ▼
响应：Server-Sent Events，直到本轮结束（完成 / 失败 / 挂起）
```

事件枢纽同时做三件事：把事件分给各轮对话的事件流、成批写审计、在一轮对话结束时把用量入账。

## 资源回收

引擎、连接器进程、沙盒都占内存与进程，不能随会话数无限增长。平台实例每隔租约的三分之一做一次维护：

| 对象 | 条件 | 动作 |
| --- | --- | --- |
| 会话的引擎 | 没有进行中的对话，且空闲超过 `session_idle_seconds`（默认 300 秒） | 释放引擎、放弃租约；会话保持活动，下次对话时从存储续接（等审批的会话同样如此） |
| 运行时 | 引擎池里没有活着的会话，且空闲超过 `runtime_idle_seconds`（默认 900 秒） | 关闭引擎池、连接器进程、沙盒；下次使用时重新组装 |

内核因还有后台子任务而拒绝释放引擎时，什么都不动，下一轮再试。

## 配额与停机

| 配额 | 检查点 | 超出时 |
| --- | --- | --- |
| `max_active_sessions` | 建会话 | 429 |
| `max_daily_tokens` | 每轮对话开始前（用量在一轮结束时入账，拦的是下一轮） | 429 |
| `max_concurrent_turns` | 每轮对话开始前（本实例内计数） | 429 |

平台关闭（`Platform.close`）分两步：先拒绝新的对话（503 `runtime_unavailable`），等在途对话跑完，最多等 `shutdown_drain_seconds`；到期仍未结束的被取消。之后释放本实例持有的全部会话，会话保持活动，可由别的实例接手。

`xuanpu serve` 下，平台关闭排在 HTTP 服务器自己的收尾之后：服务器先停止接受连接，并等已打开的响应（事件流）发完，最多等 `shutdown_drain_seconds` 加 5 秒（uvicorn 的 `timeout_graceful_shutdown`），到期断开还没发完的响应，那几轮对话转到后台；之后才轮到上面的两步。所以 `shutdown_drain_seconds` 在第二段约束的是调用方已断开、仍在后台跑的对话，整个停机最长约 `2 × shutdown_drain_seconds + 5` 秒。

## 效应闸门

| 出口 | 放行条件 | 审计 |
| --- | --- | --- |
| 沙盒内脚本、命令 | 内核工具层按权限策略检查；需要审批时本轮挂起 | 内核事件（工具调用、脚本执行） |
| 连接器工具（MCP 进程或 OpenAPI 描述的 HTTP 接口，共用一个闸门） | 在清单白名单内；按副作用分类与审批强度放行或挂起 | 每次调用一条 `connector_call`：连接器、传输、工具、副作用、决定、耗时、参数摘要 |
| 模型调用 | 租户可用、当天用量未超、会话预算未超 | 内核事件；开启留痕时另记完整请求 |

审批强度与副作用的对应关系见[编写业务包](../guides/writing-packages.md#审批强度)（实现在 `runtime/approval.py`）。没有被规则覆盖的副作用一律走审批。

## 租户隔离

taifeng 不知道租户。隔离体现在平台组装运行时注入的每一样资源上：

| 资源 | 隔离方式 |
| --- | --- |
| 控制面记录 | 每次读写都带 `tenant_id`，存储实现按它过滤 |
| 业务包文件 | `<数据目录>/packages/<租户>/<包名>/<版本>/` |
| 会话存储 | `<数据目录>/threads/<租户-摘要>/<包名>/<版本>/` |
| 沙盒工作区 | `<数据目录>/workspaces/<租户-摘要>/<包名>/<版本>/`；沙盒里读不到别处 |
| 状态面（Postgres） | 每个租户一组带专属前缀的表 |
| 密钥 | 密文以「租户 + 密钥名」作附加认证数据，挪到别处解不开 |
| 连接器进程 | 环境只有清单声明的变量加最小集合，不继承平台进程的环境 |
| 模型凭据 | 可配置为各租户用自己在保管库里的网关凭据 |

访问别的租户的资源一律按「不存在」处理，不透露它是否存在。

## 审计

审计记录只追加，来自三处：

| 来源 | 内容 | 去重 |
| --- | --- | --- |
| `engine` | 内核事件。流式增量不记，改在一轮结束时记一条 `turn_text` 带回复全文；含完整提示词的事件默认不记 | 按 `(事件流, 序号)`；引擎每次被创建是一条新的事件流 |
| `gate` | 连接器调用的过闸记录 | 不去重 |
| `control` | 控制面操作：租户、凭据、密钥、业务包、会话、用户发出的消息（`message_sent`） | 不去重 |

内核因队列满丢事件时，事件枢纽会记一条 `audit_gap`，审计不完整不会悄悄发生。

## 组件状态

| 组件 | 状态 |
| --- | --- |
| `xuanpu-spec` | 可用 |
| `xuanpu-server` | 可用（第一版，取舍见 [ADR 0005](../decisions/0005-first-release-scope.md)） |
| `xuanpu-cli` | 可用 |
| MCP 网关 | 已评估，建议采用 ContextForge，不自研（ADR 0006，待拍板）；接入等内核 `McpHttpClient` 晋升稳定层 |
| `xuanpu-console` | 可用（第一版：会话对话与审批、业务包发布与评测、审计、用量） |
| `xuanpu-sdk-py` | 可用 |
| `xuanpu-sdk-js` | 未开始 |
| 评测运行器 | 可用：每个用例一个会话，按 contains / notContains / maxTokens 检查；走正常的闸门、计量、审计 |

## 已知限制

- 连接器进程不在沙盒里；平台目前只适合承载受信任租户的业务包。
- HTTP 连接器的目标地址由租户管理员在清单里声明，平台不限制可声明的范围（含内网地址），出网范围靠部署的网络策略约束；凭据方式不含 OAuth2 换取令牌的流程（ADR 0008）。
- 会话主存是节点本地文件，跨节点接手要求共用数据目录。
- 进行中的对话不能单独取消，只能关闭会话。
- 调用方断开后只能经回放接口补齐已审计的事件，拿不到流式增量（回复全文在 `turn_text` 里）。
- 配额拦的是「下一轮」，当天实际用量可能超出上限一轮对话的量；并发对话数只在本实例内计数。
- 同一运行时下的会话共用沙盒工作区。
- 前台 shell 与文件类工具未开放（内核的这些工具工厂不在稳定层）。
- 长期记忆没有接进配置文件：taifeng-stores 提供了向量记忆后端，但平台还没有嵌入服务的配置项。以库的方式装配平台时可以自己注入（见 `packages/xuanpu-server/README.md`）。
- 没有删除租户的接口。
- 主密钥轮换后没有批量重新加密的工具，要逐条重写密钥。
- 审计表没有内置的清理与归档。
- 人工拒绝的连接器调用走不到闸门，过闸记录（`decision=rejected`）由平台在送回审批结果时按审计里的待办补记：挂起事件还没写进审计时会缺这一条，同一个拒绝被两次送回同时提交、或内核因同一次送回里别的待办答复不合格而整次拒绝时可能多一条（见 `runtime/gate_audit.py`）。
- 内核事件里的工具调用参数是一段 JSON 文本，按键名脱敏对它不起作用（见[审计与用量](../guides/audit-and-usage.md#审计里有哪些敏感内容)）。
