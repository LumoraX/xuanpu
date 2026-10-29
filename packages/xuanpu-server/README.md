# xuanpu-server

xuanpu 平台服务：控制面（租户、凭据、业务包版本、密钥、配额、审计、用量）、运行时（按业务包组装 taifeng 引擎、承载会话、处理审批挂起与续跑）、评测运行器与 HTTP 接口。

通常经 `xuanpu serve` 启动（见 [`xuanpu-cli`](../xuanpu-cli/README.md) 与[部署与运维](../../docs/guides/operations.md)）。本文讲把它当库来用：嵌进自己的 ASGI 服务，或者替换其中的部件。

设计见 [`docs/architecture/`](../../docs/architecture/README.md)。

当前版本 `2026.10.23.1` 支持已发布子Skill在公开会话接口挂起与恢复：确认目标由同会话可信审计决定，调用方只发送 `resolutions`。原链祖先持久化及待办审计完成后才发布该操作的终态；重复决定回放原回执，不重复执行。

## 嵌入

```python
import os
from pathlib import Path

from xuanpu_server.api import create_app
from xuanpu_server.bootstrap import open_platform
from xuanpu_server.config import load_config

# 平台代码自己不读环境变量：配置里 ${NAME} 用到的变量由调用方显式传入
config = load_config(Path("xuanpu.yaml"), variables=dict(os.environ))

async with open_platform(config) as platform:
    app = create_app(platform)      # 交给任意 ASGI 服务器；应用的生命周期里会启动与关闭平台
```

`create_app(platform, console_dir=…)` 给了 `console_dir` 就把控制台挂在 `/console/`。

多实例部署装 `postgres` 与 `s3` 两个 extra：`xuanpu-server[postgres,s3]`（控制面、状态面进 Postgres，业务包归档进 S3 兼容的对象存储）。

## 不经 HTTP，直接调用

`Platform` 是接入层背后的全部能力，HTTP 接口只是它的一层翻译：

```python
from xuanpu_server.platform import ROOT_PRINCIPAL

tenant = await platform.create_tenant(ROOT_PRINCIPAL, tenant_id="acme", name="Acme")
issued = await platform.issue_key(ROOT_PRINCIPAL, tenant_id="acme", name="admin", role="tenant_admin")
admin = await platform.authenticate(issued.key)       # → Principal

await platform.put_secret(admin, "tickets-token", "…")
await platform.packages.publish(admin, archive_bytes, activate=True)

session = await platform.sessions.create(admin, package="support-desk")
turn = await platform.sessions.send_message(admin, session.session_id, "你好")
async for event in turn.stream.events():              # taifeng.EventMsg
    ...
```

| 入口 | 内容 |
| --- | --- |
| `platform.authenticate(凭据)` | 凭据 → `Principal`。之后所有调用都带着它，租户边界由它给出 |
| `platform.create_tenant` / `set_tenant_status` / `set_tenant_quota` / `issue_key` / `revoke_key` | 租户与凭据 |
| `platform.put_secret` / `delete_secret`，`platform.vault` | 密钥 |
| `platform.packages` | 业务包：`publish`、`activate`、`list_versions`、`active_version`、`resolve` |
| `platform.sessions` | 会话：`create`、`get`、`list_sessions`、`send_message`、`resume`、`cancel`（停止生成）、`pending`、`replay`、`close` |
| `platform.evals` | 评测：`run` |
| `platform.store` | 控制面存储，审计与用量的查询在这里：`query_audit`、`list_usage` |
| `platform.objects` | 对象存储（业务包归档的正本） |
| `platform.metrics` | 指标：`render()` 出 Prometheus 文本格式。指标总在计；构造时给了 `metrics_token_hash` 才经 `GET /metrics` 对外（`platform.metrics_enabled`、`platform.verify_metrics_token`） |
| `platform.start()` / `platform.close()` | 启动后台维护 / 停机（等在途对话、释放会话、关闭运行时） |

出错统一抛 `xuanpu_server.errors.PlatformError` 的子类，每个都带稳定的 `code` 与 HTTP `status`。

## 可以替换的部件

外部依赖都经 `Platform` 的构造参数注入。`open_platform(config)` 按配置文件装好了一套默认实现；要换其中一样，自己构造 `Platform`，把对应的参数换成自己的实现：

| 参数 | 协议 | 默认实现 | 什么时候换 |
| --- | --- | --- | --- |
| `store` | `controlplane.store.ControlPlaneStore` | `InMemoryControlPlane`、`PostgresControlPlane` | 控制面要放进别的数据库 |
| `vault` | `secrets.SecretVault` | `EncryptedVault`（密文存在控制面里） | 接 HashiCorp Vault、云 KMS |
| `models` | `runtime.ModelClientFactory` | `GatewayModelClientFactory`（OpenAI Chat 兼容网关） | 模型接入方式不同，或测试时用脚本化的模型 |
| `sandboxes` | `runtime.SandboxProvider` | `LocalSandboxProvider`、`DockerSandboxProvider` | 接别的隔离后端（K8s、云沙盒） |
| `states` | `runtime.StateBackendFactory` | `LocalStateBackends`、`bootstrap.PostgresStateBackends` | 会话主存、线程目录、长期记忆放到别处 |
| `objects` | `objectstore.ObjectStore`（`put`、`get`、`exists`、`delete`） | `objectstore.LocalObjectStore`、`objectstore.S3ObjectStore`（`bootstrap.object_store(config)` 按配置建） | 业务包归档放到别的存储。不给就用 `<data_dir>/objects` 下的本机目录，只适合单机：**多实例必须注入同一个共享的对象存储** |

`StateBackendFactory.for_tenant(tenant_id)` 返回的 `StateBackends` 有四个槽，都是 taifeng 的稳定协议：

| 槽 | 是什么 |
| --- | --- |
| `message_store` | 会话主存的**工厂**（`Callable[[], MessageStore]`）：每个运行时调用一次，得到这个运行时引擎池专用的存储实例，池关闭时关闭它。给了它，会话主存就在外部存储里，不再用数据目录，多实例接手会话不需要共享数据目录 |
| `thread_directory` | 线程目录。给了 `message_store` 时要与它配套（同一个库、同一个命名空间），否则线程列表与实际存的消息对不上；这时不给，内核用空目录。两者都不给时用数据目录下的本机索引 |
| `index_hook` | 线程生命周期的订阅方（例如把消息镜像到别处）。**不能与 `message_store` 同时给**：构造时直接报错——外部主存与消息镜像同时挂上，多半是两者写同一张表、每条消息写两遍。这是有意的取舍：用外部主存时就不能再挂别的镜像 |
| `memory_store` | 长期记忆 |

默认的 Postgres 实现（`PostgresStateBackends`）填 `message_store`（每个运行时一个 taifeng-stores 的 `PostgresMessageStore`，共用一个连接池）与 `thread_directory`（按租户共享的 `PostgresThreadDirectory`），每个租户一组带专属前缀的表，首次使用时建表。外部主存只支持 Chat 协议的模型客户端（内核在建池时拒绝 Responses 协议）。**长期记忆（`memory_store`）目前没有接进配置文件**——要用的话，实现自己的工厂，把 taifeng-stores 的 `PgvectorMemoryStore`（需要你提供嵌入实现）填进去。

`data_dir` 只能归一个进程：业务包缓存的锁只在进程内，`platform.start()` 会清掉暂存目录的残留。同一台机器上起几个 `Platform`，就给几个不同的数据目录。

两条约束对自己的实现同样适用：

- 只依赖 taifeng 的稳定层（`taifeng.__all__`）；
- 所有读写都限定在调用传入的租户之内。

## 测试

```bash
uv run pytest packages/xuanpu-server
```

Postgres 相关用例对真实数据库执行（`XUANPU_TEST_PG_DSN`，或本机有 Docker 时临时拉起）；沙盒相关用例真的起隔离进程。前提见仓库根目录的 README。
