# 样例

| 内容 | 是什么 | 在哪篇文档里用到 |
| --- | --- | --- |
| [`support-desk/`](support-desk/) | 样例业务包：客服工单助手。有一个入口 skill、一个子 skill、一个 HTTP 连接器、一个沙盒脚本、两个评测用例 | [快速上手](../docs/guides/quickstart.md)、[编写业务包](../docs/guides/writing-packages.md) |
| [`ticket-api/server.py`](ticket-api/server.py) | 样例工单接口：给 `support-desk` 的 HTTP 连接器当被调用方。只用标准库，数据在内存里 | [快速上手](../docs/guides/quickstart.md) |
| [`xuanpu.trial.yaml`](xuanpu.trial.yaml) | 单机试用配置：不依赖 Postgres 与 Docker，进程退出即丢失数据 | [快速上手](../docs/guides/quickstart.md) |
| [`xuanpu.yaml`](xuanpu.yaml) | 正式部署的配置示例（Postgres、S3 兼容的对象存储、docker 沙盒，可以多实例），列出了全部配置项 | [部署与运维](../docs/guides/operations.md) |

样例业务包与两份配置都有测试看着（`packages/xuanpu-cli/tests/test_examples_and_docs.py`）：改了规范或配置而没同步它们，测试会失败。
