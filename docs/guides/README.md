# 使用手册

> 这里是「怎么用」；「怎么设计的」见 [../architecture/](../architecture/README.md)，「为什么这么定」见 [../decisions/](../decisions/README.md)。
> 手册描述的是当前版本的行为，随实现一起更新。

按你的角色挑着看：

| 你是谁 | 要做什么 | 看哪篇 |
| --- | --- | --- |
| 第一次接触 | 在自己机器上把平台跑起来，走通一次带审批的对话 | [快速上手](quickstart.md) |
| 业务方 | 把一个业务场景写成业务包：skill、外部接口（HTTP、MCP、远端 MCP）、脚本、审批强度、评测 | [编写业务包](writing-packages.md) |
| 应用开发者 | 在自己的系统里调用平台：建会话、收事件流、处理审批、停止生成、断线补齐 | [应用接入](integrating.md) |
| 租户管理员 / 应用开发者 | 飞书自建应用、登录、离职事件、通讯录权限与首位管理员 | [接入飞书登录](feishu-login.md) |
| 平台管理员 / 运维 | 部署、配置、多实例、密钥轮换、备份、升级、监控与排障、安全须知 | [部署与运维](operations.md) |
| 平台管理员 / 试用者 | 在一台 Mac 上用真实模型、Postgres 和飞书登录持续试用（需要 Docker） | [本机开箱](local-trial.md) |
| 平台管理员 / 运维 | 构建镜像、在 Linux 服务器上用 Docker Compose 部署单实例 | [Docker 部署](docker-deployment.md) |
| 员工 / 平台管理员 | 安装 macOS 桌面客户端，或自己构建安装包 | [桌面客户端](desktop-client.md) |
| 员工 / 应用开发者 | 交办会议与表格、确认行动项、下载成果和补读进展 | [办公交办](work-tasks.md) |
| 安全 / 合规 | 审计里有什么、没有什么，怎么查、怎么导出，用量怎么算 | [审计与用量](audit-and-usage.md) |

参考资料（查字段、查接口时用）：

| 资料 | 内容 |
| --- | --- |
| [业务包规范](../architecture/package-spec.md) | 清单 `xuanpu.yaml` 的全部字段与规则 |
| [HTTP 接口](../architecture/http-api.md) | 全部接口、错误码、事件流格式 |
| [命令行](../../packages/xuanpu-cli/README.md) | `xuanpu` 各子命令与退出码 |
| [Python 客户端](../../packages/xuanpu-sdk-py/README.md) | `XuanpuClient` 的全部方法 |
| [控制台](../../packages/xuanpu-console/README.md) | 网页界面能做什么 |
| [examples/](../../examples/) | 样例业务包 `support-desk`、样例工单接口、两份配置 |

员工使用主动跟进见[主动业务助理](proactive-assistant.md)。

- [2026-10-20 主动业务助理发布验收](releases/2026-10-20-proactive-business.md)

- [2026-10-21 R1办公交付验收](releases/2026-10-21-r1-acceptance.md)
