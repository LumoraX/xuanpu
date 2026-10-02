# 决策记录（ADR）

只增不改；要推翻旧决策，写新 ADR 并标 `Supersedes #NNNN`。旧 ADR 的正文不动，被取代、修订的部分在下表的状态一栏注明。

| 编号 | 标题 | 状态 |
| --- | --- | --- |
| [0001](0001-positioning-and-layering.md) | 定位与分层 —— 业务方只管业务，平台是 taifeng 之上的发行版 | Accepted |
| [0002](0002-out-of-process-extensions-only.md) | 业务扩展只走进程外 —— 平台进程内不加载业务方代码 | Accepted（决策 2、3 由 0008 修订；决策 2 的「经 MCP 网关接入」由 0013 修订） |
| [0003](0003-adopt-mature-components.md) | 成熟组件采用不自研 | Accepted |
| [0004](0004-planes-and-effect-gates.md) | 平台按「四平面 + 效应闸门」组织，不照搬「CPU + 外设」 | Accepted |
| [0005](0005-first-release-scope.md) | 第一版的取舍 —— 连接器先内置、密钥先自管、会话靠租约单写 | Accepted（决策 5 被 0011 决策 1 取代；决策 1 由 0013 补充，后果第 1 条被 0013 取代） |
| [0006](0006-mcp-gateway-evaluation.md) | MCP 网关的评估 —— 远端连接器经 IBM ContextForge 接入，平台自己的闸门保留 | Proposed（暂缓，见 0007 拍板结果；远端 MCP 先直连，见 0013） |
| [0007](0007-workbench-enterprise-apps-and-personal-space.md) | 企业版产品形态 —— 员工工作台、企业应用、个人空间（参照 open-dots） | Accepted（决策 3–6 与拆分顺序由 0009 修订；拍板结果第 5 项由 0011 决策 9 收窄，「对内核的依赖」一节以 0011 决策 1 为准；决策 1 的「工作台」、决策 3 的员工视角、决策 7 的页面骨架被 0012 取代） |
| [0008](0008-declarative-http-connectors.md) | 声明式 HTTP 连接器 —— 用 OpenAPI 文档把外部接口接成工具（修订 0002） | Accepted |
| [0009](0009-small-core-connect-everything.md) | 平台保持小核 —— 企业数据一律接入、自有文件走对象存储、垂直业务各自独立（修订 0007） | Accepted |
| [0010](0010-user-identity-feishu-login.md) | 用户身份 —— 飞书登录、服务端登录会话、离职事件走回调 | Accepted |
| [0011](0011-system-design-decisions.md) | 整体设计定下的九件事 —— 底座先无状态、员工内容管理员默认不可见、新增触发与个人 skill | Accepted |
| [0012](0012-personal-workstation.md) | 员工端是个人工作站 —— 一人一个助理、事项、四种待办、部门助理透明 | Accepted |
| [0013](0013-connector-sandbox-and-remote-mcp.md) | 连接器进沙盒、远端 MCP 直连 —— stdio 连接器在单独的连接器沙盒里运行，`transport: remote` 不等网关 | Accepted |
