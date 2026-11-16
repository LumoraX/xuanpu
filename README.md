# xuanpu（玄圃）

基于 [taifeng](https://github.com/LumoraX/taifeng) 的企业 agent 平台：**员工只对管家说话，管家自己办，或派给匠人去办，验收后交付。** 业务方只提交业务包，调度、恢复、隔离、存储、计量审计都由平台负责。

> 玄圃：昆仑山上众神居住的园子。泰逢是山海经里的神，玄圃是承载它们的地方。

> **状态**：R0–R5 已合入主线，依赖 taifeng 2026.11.14.1。R5 带来管家—匠人编排、执行追踪与极简入口，均按租户白名单开启，默认关闭；真实模型回归与用户签收尚未完成（见 [R5 交付计划](docs/superpowers/plans/2026-11-14-r5-delivery.md)）。在此之前已有：飞书登录与员工身份、个人工作站（文件、事项、待办、日程）、提醒与通知中心、主动业务助理、持久任务、到人协作（交接、审批、共享事项）、企业连接、能力安装治理。MCP 网关暂缓（ADR 0006）。已知限制见[架构总览](docs/architecture/README.md#已知限制)。

## 定位：管家与匠人

玄圃是企业核心系统：身份、业务规则、流程编排、权限、状态与审计的长期中心在玄圃；飞书、企业微信、ERP 等围绕它集成，可以替换。

| 角色 | 是谁 | 做什么 |
| --- | --- | --- |
| **员工** | 企业里的每个人 | 在一个对话框里说要办的事；需要本人决定的事以确认卡出现，本人点了才生效 |
| **管家** | 平台内置 agent，每位员工一个 | 能自己办的直接办（回答、只读查询、提醒/待办/日程等草稿）；办不了的选匠人派工，必要时编组；替匠人回答员工说过的事实，其余转问本人；验收匠人结果后交付 |
| **匠人** | 已发布、且该员工有资格使用的业务包入口 | 在各自的沙盒里用各自的连接器与凭据干活；跨包协作只经管家派工，不能互调工具 |
| **平台** | xuanpu（本仓） | 会话调度、挂起/恢复/崩溃接管、沙盒与连接器隔离、预算与配额、按租户的执行追踪、审计与计量 |
| **内核** | taifeng | 单个 agent 的执行循环、技能、执行身份与事件；不知道租户 |

编组有三种：**并行**（各办一块，汇总）、**接力**（上一位的结果交给下一位）、**会诊**（各自给结论，管家比对；分歧大或涉及金额、对外发送、审批的交员工裁决）。编排是声明式计划，不执行模型写的脚本（[ADR 0017](docs/decisions/0017-butler-artisan-orchestration.md)）。

一次请求的路径：

```
员工输入 ─→ 管家一轮
            ├─ 自办：回答 / 只读查询 / 待确认草稿 ─→ 结束
            └─ 派工（单人或编组）─→ 立刻答复「交给了谁、接下来会怎样」，不占住对话
匠人结束或提问 ─→ 平台按持久记录唤醒管家（同一条 trace）─→ 验收 / 代答 / 转问本人 ─→ 交付
```

每次请求是一条 **trace**，管家、派工、编组、匠人、工具调用各是一个 **span**：员工可以看执行树与瀑布（`GET /v1/traces`），也可以选择导出到 OTel。trace 只存元数据，不存正文与参数。

### 业务方写什么

| | 业务方提交 | 平台负责 |
| --- | --- | --- |
| 内容 | 业务包：skills、连接器声明（MCP server、远端 MCP 地址或一份 OpenAPI 文档）、策略、评测用例 | 把它变成管家可派的匠人：版本、可用范围、沙盒、凭据注入、闸门与审计 |
| 扩展方式 | 只走进程外：MCP server、沙盒内脚本、HTTP 接口 | 平台进程内不加载任何业务方代码 |
| 诊断 | 评测用例 | 执行树、瀑布、审计、回放 |

## 入口

- **极简入口**（R5，按租户开启）：概览、对话、输入、提示四样。提醒、待办、日程、事项、文件、交接、审批、共享、连接都在对话里以卡片出现，操作调用原接口。
- **「更多」**：原来的功能页保留，可以直接查看记录和手动操作，不依赖模型可用。两种入口共用数据、权限、规则与审计。
- **macOS 桌面客户端**：首次打开时填写自己的玄圃服务器地址，只保存在本机 `~/.xuanpu/desktop.json`；运行日志在 `~/.xuanpu/logs/`。

## 分层

```
业务包（各租户提交，只含声明与进程外扩展）
        │  ← 以「匠人」身份被管家派工
xuanpu（本仓）：管家 · 编排 · 执行追踪 · 运行时 · 控制面 · 命令行 · 控制台
        │
taifeng-sandbox · taifeng-stores（内核适配包）
        │
taifeng（内核，零租户概念）
```

平台按四个平面组织——计算、效应、状态、治理——一切副作用都要过闸门并留下审计，见 [ADR 0004](docs/decisions/0004-planes-and-effect-gates.md)。

## 试用

跟着[快速上手](docs/guides/quickstart.md)走一遍：在一台机器上启动平台、发布样例业务包 [`examples/support-desk`](examples/support-desk/)、走通一次带审批的对话，不需要 Postgres 和 Docker。

```bash
git clone --recurse-submodules https://github.com/LumoraX/xuanpu.git && cd xuanpu
uv sync --all-packages --all-extras

uv run xuanpu keygen root                                   # 根凭据与摘要
uv run xuanpu keygen master                                 # 主密钥
uv run xuanpu serve --config examples/xuanpu.trial.yaml     # 控制台在 http://127.0.0.1:8420/console/

uv run xuanpu validate examples/support-desk
uv run xuanpu pack examples/support-desk -o support-desk.zip
```

管家、执行追踪与极简入口默认关闭；在配置里对租户开启的方法见[管家](docs/guides/butler.md)与[极简入口](docs/guides/minimal-entry.md)。

## 组件

| 组件 | 包 | 状态 |
| --- | --- | --- |
| 业务包规范 | `xuanpu-spec` | 可用 |
| 平台服务（管家、编排、执行追踪、运行时、控制面） | `xuanpu-server` | 可用；R5 功能按租户开启 |
| 命令行 | `xuanpu-cli` | 可用 |
| Python 客户端 | `xuanpu-sdk-py` | 可用（含管家、待处理项、执行追踪与概览接口） |
| 控制台 | `xuanpu-console` | 可用：极简入口 `/console/entry.html` 与「更多」功能页 |
| macOS 桌面客户端 | `xuanpu-desktop` | Apple Silicon；首次打开填写服务器地址；[安装与边界](docs/guides/desktop-client.md) |
| MCP 网关 | — | 暂缓（ADR 0006）；部署在别处的 MCP server 经 `transport: remote` 直连（ADR 0013） |
| JavaScript 客户端 | `xuanpu-sdk-js` | 未开始 |

## 文档

| 你想 | 看 |
| --- | --- |
| 先跑起来 | [快速上手](docs/guides/quickstart.md) |
| 开启管家、了解派工、编组、验收与代答 | [管家手册](docs/guides/butler.md) · [管家架构](docs/architecture/butler.md) |
| 给员工换成极简入口 | [极简入口](docs/guides/minimal-entry.md) · [架构](docs/architecture/minimal-entry.md) |
| 看一次请求的执行树、瀑布，或导出 OTel | [执行追踪](docs/architecture/execution-trace.md) |
| 把业务场景写成业务包（成为匠人） | [编写业务包](docs/guides/writing-packages.md) |
| 在自己的系统里调用平台 | [应用接入](docs/guides/integrating.md) |
| 配置员工登录、离职事件与首位管理员 | [接入飞书登录](docs/guides/feishu-login.md) |
| 部署、配置、多实例、密钥轮换、备份、安全须知 | [部署与运维](docs/guides/operations.md) |
| 在本机持续试用，或用 Docker 部署到服务器 | [本机开箱](docs/guides/local-trial.md) · [Docker 部署](docs/guides/docker-deployment.md) · [桌面客户端](docs/guides/desktop-client.md) |
| 了解审计里有什么、怎么查、用量怎么算 | [审计与用量](docs/guides/audit-and-usage.md) |
| 查字段与接口 | [业务包规范](docs/architecture/package-spec.md) · [HTTP 接口](docs/architecture/http-api.md) · [命令行](packages/xuanpu-cli/README.md) · [Python 客户端](packages/xuanpu-sdk-py/README.md) |
| 了解设计 | [架构总览](docs/architecture/README.md)：四个平面、一轮对话的路径、租户隔离、已知限制 |
| 了解为什么这么定 | [决策记录](docs/decisions/README.md)，管家—匠人见 [ADR 0017](docs/decisions/0017-butler-artisan-orchestration.md) |
| 给 taifeng 内核提需求 | [内核对接口缺口清单](docs/architecture/kernel-seam-backlog.md) |

## 开发

```bash
git submodule update --init --recursive    # 内核与适配包在 external/ 下
uv sync --all-packages --all-extras
uv run pytest
uv run ruff check --select F,S108,I,TC packages
uv run mypy packages/*/src
```

内核 `taifeng`（≥ 2026.11.14.1）与适配包 `taifeng-sandbox`、`taifeng-stores` 以 git submodule 放在 `external/` 下，开发期从子模块取源码；平台只 import `taifeng.__all__` 里的稳定接口。

测试的前提：

- Postgres 相关用例对真实数据库执行。设置 `XUANPU_TEST_PG_DSN`（须装有 pgvector 扩展）使用已有服务，否则用 Docker 临时拉起 `pgvector/pgvector:pg16`；两者都没有时跳过。
- 沙盒相关用例真的起隔离进程：macOS 用系统自带的 `sandbox-exec`；Linux 需要装 `bubblewrap` 并允许创建用户命名空间。容器后端的用例（含连接器容器、办公执行容器）在本机有 Docker 时才跑。
- 对象存储的 S3 实现用 moto 在本机起一个 S3 服务来测，不连真实的 OSS。

## 许可

Apache-2.0
