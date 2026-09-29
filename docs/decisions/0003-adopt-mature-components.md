# ADR 0003：成熟组件采用不自研

- 状态：Accepted
- 日期：2026-09-29
- 关联：ADR 0001；taifeng ADR 0017（规则③）

## 背景

平台要的很多能力已经有成熟的开源方案。自研这些东西会稀释投入，而平台真正的价值在把它们组装成「业务方只管业务」的体验。

## 决策

下列能力直接采用现成方案，xuanpu 只写接入与配置：

| 能力 | 采用 | 接入位置 |
| --- | --- | --- |
| 模型网关（路由、各租户 key、限流、回退） | LiteLLM Proxy | taifeng 已支持 litellm |
| 轨迹存储与可视化 | OpenTelemetry + Langfuse 或 Phoenix | taifeng 已有 OTel 出口 |
| 密钥管理 | Vault 或云 KMS | 平台控制面 |
| 隔离运行时 | Docker、gVisor、Firecracker、E2B、Daytona | 经 taifeng-sandbox 适配 |
| 浏览器、搜索 | Playwright MCP、现成搜索 MCP server | 经 MCP 网关 |
| 认证与 SSO | Keycloak 或任意 OIDC 服务 | 平台控制面 |
| 部署编排 | Kubernetes | 部署层 |

MCP 网关先评估现成方案（如 Docker MCP Gateway、IBM ContextForge），确认不能满足多租户凭据注入、出网控制、副作用分类后再自研。

## 后果

- 新增任何自研组件前，必须在对应 ADR 或提案里写明现成方案为何不行。
- 采用的外部组件升级由平台负责跟进，业务方无感知。
