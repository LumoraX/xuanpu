# 文档总入口

| 目录 | 是什么 | 给谁看 |
| --- | --- | --- |
| [guides/](guides/README.md) | 使用手册：快速上手、编写业务包、应用接入、[飞书登录](guides/feishu-login.md)、部署与运维、审计与用量、[本机开箱](guides/local-trial.md)、[桌面客户端](guides/desktop-client.md)、[Docker 部署](guides/docker-deployment.md) | 使用平台的人 |
| [architecture/](architecture/README.md) | 当前生效的设计与参考资料（活文档，永远代表现状）：架构总览、业务包规范、HTTP 接口、内核缺口清单、[个人工作站](architecture/personal-workspace.md)、[单机 Docker 部署](architecture/deployment.md)、[桌面客户端](architecture/desktop-client.md) | 使用平台的人查字段与接口；开发平台的人 |
| [decisions/](decisions/README.md) | ADR 决策记录（为什么这么定，只增不改） | 开发平台的人 |
| [superpowers/specs/](superpowers/specs/2026-09-30-system-design.md) | 实现前的设计：平台整体设计、个人工作站产品设计、各子项目详细设计、示意稿 | 开发平台的人 |
| [superpowers/specs/](superpowers/specs/) | 子项目实现前的详细设计；实现落地后，现状以 architecture/ 为准 | 开发平台的人 |
| [superpowers/plans/](superpowers/plans/) | 实现计划、交接与进度快照；最近一份全局快照见[2026-10-07 实现现状](superpowers/plans/2026-10-07-implementation-status.md) | 开发平台的人 |

各个包自己的说明在 `packages/<包名>/README.md`；可以直接运行的样例在 [`examples/`](../examples/)。

当前办公任务使用与验收见[办公交办](guides/work-tasks.md)、[任务架构](architecture/work-tasks.md)和[R1验收](guides/releases/2026-10-21-r1-acceptance.md)。

财务业务闭环的待审阅设计与 Skill／API 契约见[财务能力设计](superpowers/specs/2026-10-21-finance-capability-design.md)，首个独立子项目为[F0 发票基座](superpowers/specs/2026-10-21-finance-f0-design.md)；这些材料不表示财务服务已实现或上线。
