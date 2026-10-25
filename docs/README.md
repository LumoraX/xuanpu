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

当前办公任务使用与验收见[办公交办](guides/work-tasks.md)、[任务架构](architecture/work-tasks.md)和[R2验收](guides/releases/2026-10-21-r2-acceptance.md)；[R1验收](guides/releases/2026-10-21-r1-acceptance.md)保留历史结果。

财务业务闭环的待审阅设计与 Skill／API 契约见[财务能力设计](superpowers/specs/2026-10-21-finance-capability-design.md)，首个独立子项目为[F0 发票基座](superpowers/specs/2026-10-21-finance-f0-design.md)；这些材料不表示财务服务已实现或上线。

R2已交付持久执行、时间与事件触发、工作动态及员工入口，生产版本2026.10.22.2；实际共享宿主整机重启也已完成验收。设计和实施过程见[持久执行设计](superpowers/specs/2026-10-21-r2-durable-work-design.md)及[详细实施总计划](superpowers/plans/2026-10-21-r2-delivery.md)，能力与未验边界以[R2验收记录](guides/releases/2026-10-21-r2-acceptance.md)为准。

R3的[设计提案](superpowers/specs/2026-10-22-r3-enterprise-collaboration-design.md)与[详细实施总计划](superpowers/plans/2026-10-22-r3-delivery.md)已拆解为五段，首先推进[企业微信只读连接](superpowers/plans/2026-10-22-r3a-connections-wecom.md)，随后建设组织岗位、共享事项、交接审批及统一体验。R3-A已实现连接安装、本人同意/撤销、企微客户端身份绑定与受控只读；代码和合成协议验收见[记录](guides/releases/2026-10-22-r3a-acceptance.md)。真实企业微信应用验收仍待管理员配置，生产未启用本期功能；其余R3阶段仍按总计划推进。

R4-A已在源码实现[能力目录与安装契约](architecture/capability-delivery.md)及[原生管理、SDK和CLI入口](guides/capability-delivery.md)。本地分层结果见[R4-A验收](guides/releases/2026-10-25-r4a-acceptance.md)；分配、执行和发布尚待后续阶段，当前未生产部署。
