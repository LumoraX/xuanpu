# ADR 0006：MCP 网关的评估 —— 远端连接器经 IBM ContextForge 接入，平台自己的闸门保留

- 状态：Proposed（评估结论与建议；采用与否待拍板）
- 日期：2026-09-30
- 关联：ADR 0002（业务扩展只走进程外）、ADR 0003（成熟组件采用不自研）、ADR 0005 决策 1
- 评估对象：[Docker MCP Gateway](https://github.com/docker/mcp-gateway)、[IBM ContextForge](https://github.com/IBM/mcp-context-forge)

## 背景

ADR 0003 把 MCP 网关列为「先评估现成方案」的项：确认它们不能满足多租户凭据注入、出网控制、副作用分类之后再自研。第一版（ADR 0005）先把连接器的闸门做在平台服务里，本 ADR 记录评估结果。

四项要求逐一核对：

| 要求 | Docker MCP Gateway | IBM ContextForge |
| --- | --- | --- |
| 多租户：连接器与凭据按租户隔离 | 无租户概念；只有把 server 分组的 profiles，凭据经 Docker Desktop 管理，面向单机开发者 | 有：teams 是访问边界，资源分 public / team / private 三档可见性；JWT 里的 teams 声明决定能看到什么；角色有 platform_admin / team_admin / developer / viewer |
| 凭据注入：明文只在出口出现 | 有 secrets 命令与 OAuth 流程，但绑定 Docker Desktop | 有：凭据加密存储（`AUTH_ENCRYPTION_SECRET`），支持把调用方的头透传给上游（HTTP Header Passthrough） |
| 出网控制 | 未见 | 未见网络层控制；有资源域名黑名单插件（`ResourceFilterPlugin`） |
| 副作用分类与按分类审批 | 无 | 无内置分类；但插件框架有 `tool_pre_invoke` / `tool_post_invoke` 钩子，插件可以拒绝（`continue_processing=False`）或改写调用，且支持进程外插件（STDIO / HTTP / gRPC） |
| 调用审计 | 有日志与调用追踪，范围不详 | 有：结构化日志、管理界面日志查看、OpenTelemetry 追踪（工具 / 提示 / 资源 / 网关操作自动埋点） |
| 传输 | 面向 Docker Desktop 的本机形态 | HTTP、SSE、Streamable HTTP、WebSocket；stdio 用于服务端包装 |
| 部署 | Docker CLI 插件 | PyPI、容器、Helm，多副本 + Redis |
| 许可证 | MIT | Apache-2.0 |

## 结论

1. **Docker MCP Gateway 不适合**：它面向单机开发者与 Docker Desktop，没有租户模型，无法作为多租户平台的组件。
2. **ContextForge 满足多租户与审计两项，但「副作用分类」与「按租户审批强度放行」不是它的内置能力**——这两样是 xuanpu 的概念（业务包清单里的 `effect`、租户策略里的 `approval`），任何网关都要靠平台侧的逻辑来实现。ContextForge 的 `tool_pre_invoke` 钩子可以承载这层逻辑，但需要审批时挂起本轮对话、等人裁决后续跑，这个流程由内核的挂起机制驱动，网关插件做不到。
3. **出网控制两者都没有**。这一项本来就该由沙盒（容器网络策略）承担，不应期待网关解决。

## 建议

- **平台自己的连接器闸门保留**：白名单、按副作用分类审批、参数摘要审计留在 `xuanpu-server`（现状）。它是策略层，不是网关。
- **远端 MCP server 的接入采用 ContextForge**：业务方把自己部署的 MCP server 注册到 ContextForge，平台以 HTTP 传输连接 ContextForge 的虚拟 server；租户凭据以 ContextForge 团队为边界管理，平台按租户转发凭据头。这样不用自研网关。
- **不再单独立 `xuanpu-mcp-gateway` 组件**；对应的工作变为「ContextForge 的接入与配置」。

## 前置条件

- taifeng 的 `McpHttpClient` 目前在实验层（`taifeng.experimental`），平台按红线 3 不能使用；需要它晋升到稳定层（已登记到 `kernel-seam-backlog.md` 第 12 项）。
- 由平台代为运行的本机 stdio 连接器仍然存在（第一版形态），它进沙盒的问题不变（缺口第 7 项）。

## 后果

- 采用后，业务包清单的连接器声明需要增加 `transport: http` 与地址字段；副作用分类与白名单的写法不变。
- 平台多了一个外部依赖的升级跟进（ContextForge 版本），由平台负责，业务方无感知。
