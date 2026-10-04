# xuanpu（玄圃）

基于 [taifeng](https://github.com/LumoraX/taifeng) 的企业级 agent 平台：业务方只设计自己的业务场景，调度、恢复、隔离、存储都交给平台。

> 玄圃：昆仑山上众神居住的园子。泰逢是山海经里的神，玄圃是承载它们的地方。

> **状态**：第一版 + P0 底座升级 + P1 用户身份（本地 `feat/p1-identity`，依赖 taifeng 2026.10.1.10）。业务包规范、平台服务、命令行、Python 客户端、控制台可用。P0 支持 Postgres 会话主存、对象存储归档、停止生成、连接器沙盒与远端 MCP、指标与配额修改；P1 增加飞书登录、用户管理、会话归属与隔离、代员工调用、按员工审计用量、离职事件与每日对账。真实 CLI 与模拟飞书已验证，真实飞书应用尚未验收；本轮未推送、未发布、未合入 main。MCP 网关暂缓（ADR 0006）。已知限制见[架构总览](docs/architecture/README.md#已知限制)。

## 定位：像用数据库一样用 agent

| | 数据库 | xuanpu |
| --- | --- | --- |
| **业务方写什么** | 表结构、SQL | 业务包：skills、连接器声明（MCP server、远端 MCP 地址或一份 OpenAPI 文档）、策略、评测用例 |
| **平台管什么** | 执行计划、事务、崩溃恢复、复制 | 会话调度、上下文压缩与缓存、挂起 / 恢复 / 崩溃接管、沙盒、存储、模型路由、计量审计 |
| **内核** | 存储引擎 | taifeng |
| **诊断入口** | EXPLAIN、慢查询日志 | 轨迹、回放、评测报告 |

## 分层

```
业务包（各租户提交，只含声明与进程外扩展）
        │
xuanpu（本仓）：运行时 · 控制面 · 命令行
        │
taifeng-sandbox · taifeng-stores（内核适配包）
        │
taifeng（内核，零租户概念）
```

平台按四个平面组织——计算、效应、状态、治理——一切副作用都要过闸门并留下审计，见 [ADR 0004](docs/decisions/0004-planes-and-effect-gates.md)。

## 试用

跟着[快速上手](docs/guides/quickstart.md)走一遍：在一台机器上启动平台、发布样例业务包 [`examples/support-desk`](examples/support-desk/)、走通一次带审批的对话，不需要 Postgres 和 Docker。

```bash
uv sync --all-packages --all-extras

uv run xuanpu keygen root                                   # 根凭据与摘要
uv run xuanpu keygen master                                 # 主密钥
uv run xuanpu serve --config examples/xuanpu.trial.yaml     # 控制台在 http://127.0.0.1:8420/console/

uv run xuanpu validate examples/support-desk
uv run xuanpu pack examples/support-desk -o support-desk.zip
```

## 组件

| 组件 | 包 | 状态 |
| --- | --- | --- |
| 业务包规范 | `xuanpu-spec` | 可用 |
| 平台服务（运行时 + 控制面） | `xuanpu-server` | 可用（第一版 + P0 + P1 用户身份） |
| 命令行 | `xuanpu-cli` | 可用 |
| Python 客户端 | `xuanpu-sdk-py` | 可用 |
| 控制台 | `xuanpu-console` | 可用（服务启动后访问 `/console/`） |
| MCP 网关 | — | 暂缓（ADR 0006）；部署在别处的 MCP server 经 `transport: remote` 直连（ADR 0013） |
| JavaScript 客户端 | `xuanpu-sdk-js` | 未开始 |

## 文档

| 你想 | 看 |
| --- | --- |
| 先跑起来 | [快速上手](docs/guides/quickstart.md) |
| 把业务场景写成业务包 | [编写业务包](docs/guides/writing-packages.md) |
| 在自己的系统里调用平台 | [应用接入](docs/guides/integrating.md) |
| 配置员工登录、离职事件与首位管理员 | [接入飞书登录](docs/guides/feishu-login.md) |
| 部署、配置、多实例、密钥轮换、备份、安全须知 | [部署与运维](docs/guides/operations.md) |
| 了解审计里有什么、怎么查、用量怎么算 | [审计与用量](docs/guides/audit-and-usage.md) |
| 查字段与接口 | [业务包规范](docs/architecture/package-spec.md) · [HTTP 接口](docs/architecture/http-api.md) · [命令行](packages/xuanpu-cli/README.md) · [Python 客户端](packages/xuanpu-sdk-py/README.md) |
| 了解设计 | [架构总览](docs/architecture/README.md)：四个平面、一轮对话的路径、租户隔离、已知限制 |
| 了解为什么这么定 | [决策记录](docs/decisions/README.md) |
| 给 taifeng 内核提需求 | [内核对接口缺口清单](docs/architecture/kernel-seam-backlog.md) |

## 开发

```bash
uv sync --all-packages --all-extras
uv run pytest
uv run ruff check --select F,S108,I,TC packages
uv run mypy packages/*/src
```

内核 `taifeng`（≥ 2026.10.1.10）从 PyPI 安装。`taifeng-sandbox`、`taifeng-stores` 尚未发布到 PyPI，开发期从同级目录取源码：这两个仓库与 `xuanpu` 要检出在同一个父目录下。

测试的前提：

- Postgres 相关用例对真实数据库执行。设置 `XUANPU_TEST_PG_DSN`（须装有 pgvector 扩展）使用已有服务，否则用 Docker 临时拉起 `pgvector/pgvector:pg16`；两者都没有时跳过。
- 沙盒相关用例真的起隔离进程：macOS 用系统自带的 `sandbox-exec`；Linux 需要装 `bubblewrap` 并允许创建用户命名空间。容器后端的用例（含连接器容器）在本机有 Docker 时才跑。
- 对象存储的 S3 实现用 moto 在本机起一个 S3 服务来测，不连真实的 OSS。

## 许可

Apache-2.0
