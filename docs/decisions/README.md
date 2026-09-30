# 决策记录（ADR）

只增不改；要推翻旧决策，写新 ADR 并标 `Supersedes #NNNN`。

| 编号 | 标题 | 状态 |
| --- | --- | --- |
| [0001](0001-positioning-and-layering.md) | 定位与分层 —— 业务方只管业务，平台是 taifeng 之上的发行版 | Accepted |
| [0002](0002-out-of-process-extensions-only.md) | 业务扩展只走进程外 —— 平台进程内不加载业务方代码 | Accepted |
| [0003](0003-adopt-mature-components.md) | 成熟组件采用不自研 | Accepted |
| [0004](0004-planes-and-effect-gates.md) | 平台按「四平面 + 效应闸门」组织，不照搬「CPU + 外设」 | Accepted |
| [0005](0005-first-release-scope.md) | 第一版的取舍 —— 连接器先内置、密钥先自管、会话靠租约单写 | Accepted |
| [0006](0006-mcp-gateway-evaluation.md) | MCP 网关的评估 —— 远端连接器经 IBM ContextForge 接入，平台自己的闸门保留 | Proposed |
| [0007](0007-workbench-enterprise-apps-and-personal-space.md) | 企业版产品形态 —— 员工工作台、企业应用、个人空间（参照 open-dots） | Proposed |
| [0008](0008-declarative-http-connectors.md) | 声明式 HTTP 连接器 —— 用 OpenAPI 文档把外部接口接成工具（修订 0002） | Accepted |
