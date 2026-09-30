# xuanpu（玄圃）

基于 [taifeng](https://github.com/LumoraX/taifeng) 的企业级 agent 平台：业务方只设计自己的业务场景，调度、恢复、隔离、存储都交给平台。

> 玄圃：昆仑山上众神居住的园子。泰逢是山海经里的神，玄圃是承载它们的地方。

> **状态**：第一版。业务包规范、平台服务（控制面 + 运行时 + HTTP 接口 + 评测）、命令行、Python 客户端、控制台可用；独立的 MCP 网关未开始。第一版的取舍与已知限制见 [ADR 0005](docs/decisions/0005-first-release-scope.md)。

## 定位：像用数据库一样用 agent

| | 数据库 | xuanpu |
| --- | --- | --- |
| **业务方写什么** | 表结构、SQL | 业务包：skills、连接器声明（MCP server 或一份 OpenAPI 文档）、策略、评测用例 |
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

```bash
uv sync --all-packages --all-extras

# 1. 生成根凭据与主密钥
uv run xuanpu keygen root
uv run xuanpu keygen master

# 2. 写配置（参照 examples/xuanpu.yaml），迁移表结构，启动
uv run xuanpu migrate --config xuanpu.yaml
uv run xuanpu serve --config xuanpu.yaml     # 控制台在 http://127.0.0.1:8420/console/

# 3. 业务方：校验并打包业务包
uv run xuanpu validate ./support-desk
uv run xuanpu pack ./support-desk -o support-desk.zip
```

```bash
# 4. 平台管理员开通租户、签发凭据
curl -X POST localhost:8420/v1/admin/tenants -H "Authorization: Bearer $ROOT_KEY" \
     -H 'Content-Type: application/json' -d '{"tenant_id": "acme", "name": "Acme"}'
curl -X POST localhost:8420/v1/admin/tenants/acme/keys -H "Authorization: Bearer $ROOT_KEY" \
     -H 'Content-Type: application/json' -d '{"name": "admin", "role": "tenant_admin"}'

# 5. 租户管理员配密钥、发布业务包
curl -X PUT localhost:8420/v1/secrets/crm-token -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' -d '{"value": "…"}'
curl -X POST 'localhost:8420/v1/packages?activate=true' -H "Authorization: Bearer $KEY" \
     --data-binary @support-desk.zip

# 6. 发布前先跑评测
uv run xuanpu eval --key $KEY --package support-desk

# 7. 建会话、对话（响应是事件流）
curl -X POST localhost:8420/v1/sessions -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' -d '{"package": "support-desk"}'
curl -N -X POST localhost:8420/v1/sessions/$SESSION/messages -H "Authorization: Bearer $KEY" \
     -H 'Content-Type: application/json' -d '{"text": "你好"}'
```

## 组件

| 组件 | 包 | 状态 |
| --- | --- | --- |
| 业务包规范 | `xuanpu-spec` | 可用 |
| 平台服务（运行时 + 控制面） | `xuanpu-server` | 可用（第一版） |
| 命令行 | `xuanpu-cli` | 可用 |
| Python 客户端 | `xuanpu-sdk-py` | 可用 |
| 控制台 | `xuanpu-console` | 可用（服务启动后访问 `/console/`） |
| MCP 网关 | — | 已评估：建议采用 IBM ContextForge 接远端连接器，不自研（ADR 0006，待拍板） |
| JavaScript 客户端 | `xuanpu-sdk-js` | 未开始 |

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/architecture/README.md](docs/architecture/README.md) | 架构总览：四个平面、一轮对话的路径、租户隔离、审计、已知限制 |
| [docs/architecture/package-spec.md](docs/architecture/package-spec.md) | 业务包规范 |
| [docs/architecture/http-api.md](docs/architecture/http-api.md) | HTTP 接口、事件流、审批 |
| [docs/architecture/kernel-seam-backlog.md](docs/architecture/kernel-seam-backlog.md) | 内核稳定层的对接口缺口与平台的现行处理 |
| [docs/decisions/](docs/decisions/README.md) | 决策记录 |

## 开发

```bash
uv sync --all-packages --all-extras
uv run pytest
uv run ruff check --select F,S108,I,TC packages
uv run mypy packages/*/src
```

`taifeng-sandbox` 与 `taifeng-stores` 尚未发布到 PyPI，开发期从同级目录取源码：三个仓库要检出在同一个父目录下。

测试的前提：

- Postgres 相关用例对真实数据库执行。设置 `XUANPU_TEST_PG_DSN`（须装有 pgvector 扩展）使用已有服务，否则用 Docker 临时拉起 `pgvector/pgvector:pg16`；两者都没有时跳过。
- 沙盒相关用例真的起隔离进程：macOS 用系统自带的 `sandbox-exec`；Linux 需要装 `bubblewrap` 并允许创建用户命名空间。

## 许可

Apache-2.0
