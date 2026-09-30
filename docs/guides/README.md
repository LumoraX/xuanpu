# 使用手册

> 这里是「怎么用」；「怎么设计的」见 [../architecture/](../architecture/README.md)，「为什么这么定」见 [../decisions/](../decisions/README.md)。
> 手册描述的是当前版本的行为，随实现一起更新。

按你的角色挑着看：

| 你是谁 | 要做什么 | 看哪篇 |
| --- | --- | --- |
| 第一次接触 | 在自己机器上把平台跑起来，走通一次带审批的对话 | [快速上手](quickstart.md) |
| 业务方 | 把一个业务场景写成业务包：skill、外部接口、脚本、审批强度、评测 | [编写业务包](writing-packages.md) |
| 应用开发者 | 在自己的系统里调用平台：建会话、收事件流、处理审批、断线补齐 | [应用接入](integrating.md) |
| 平台管理员 / 运维 | 部署、配置、多实例、密钥轮换、备份、升级 | [部署与运维](operations.md) |
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
