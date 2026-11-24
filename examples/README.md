# 样例与部署配置模板

> **说明**：正式官方通用技能库与标准业务匠人（`common-skills`、`travel-planner`、`driver-dispatch`、`expense-helper`、`order-desk`）已迁出至同级独立生态目录 `../lumorax-catalog/`（按 `skills/`、`plugins/`、`artisans/` 组织），经 `marketplace.catalog_dirs` 挂载到控制台「能力市场」安装使用。
> 本目录只保留平台自包含的最小教学样例、离线 mock 替身、契约测试夹具与配置样板。

| 内容 | 是什么 | 在哪篇文档里用到 |
| --- | --- | --- |
| [`support-desk/`](support-desk/) | 最小教学样例业务包：客服工单助手（1 个入口 skill、1 个子 skill、1 个 HTTP 连接器、1 个沙盒脚本、2 个评测用例） | [快速上手](../docs/guides/quickstart.md)、[编写业务包](../docs/guides/writing-packages.md) |
| [`office-review/`](office-review/) | R4-E 可复制能力样板：业务包 `package/`、外部交付描述 `delivery.json`、评测套件与确定性构建脚本 | [能力登记与安装预检](../docs/guides/capability-delivery.md) |
| [`ticket-api/server.py`](ticket-api/server.py) | 本地工单接口替身：给 `support-desk` 的 HTTP 连接器当被调用方 | [快速上手](../docs/guides/quickstart.md) |
| [`demo-office-api/server.py`](demo-office-api/server.py) | 本地办公系统替身：给订单、报销匠人提供离线演示接口 | [管家架构](../docs/architecture/butler.md) |
| [`demo-map-api/server.py`](demo-map-api/server.py) | 本地高德地图兼容替身：给 `common-skills` 的 `amap` 连接器提供离线 IP 定位、POI、路线与天气响应 | [业务包规范](../docs/architecture/package-spec.md) |
| [`xuanpu.trial.yaml`](xuanpu.trial.yaml) | 单机内存试用配置：不依赖 Postgres 与 Docker，进程退出即丢失数据 | [快速上手](../docs/guides/quickstart.md) |
| [`xuanpu.local.yaml`](xuanpu.local.yaml) | 单机持久化开发配置：控制面落 Postgres、对象存储落本地盘并挂载官方生态目录，重启不丢数据与密钥 | [部署与运维](../docs/guides/operations.md) |
| [`xuanpu.yaml`](xuanpu.yaml) | 正式部署配置示例（Postgres、S3 兼容对象存储、Docker 沙盒），列出了全部配置项 | [部署与运维](../docs/guides/operations.md) |

样例业务包与配置都有测试看着（`tests/cli/test_examples_and_docs.py`）：改了规范或配置而没同步它们，测试会失败。
