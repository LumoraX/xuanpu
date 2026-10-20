# 部署与运维

面向平台管理员：平台由什么组成、怎么配置、多实例怎么跑、密钥怎么轮换、备份什么、升级怎么做、安全上要把住什么。

## 组成

```
                 ┌────────────────────────┐
  调用方 ─HTTPS─▶│ 反向代理（TLS、粘性路由）│
                 └───────────┬────────────┘
                             │ HTTP + SSE
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        xuanpu serve    xuanpu serve    xuanpu serve      平台实例，无主从
        本地数据目录    本地数据目录    本地数据目录      各自一份，只放缓存
              │              │              │
   ┌──────────┼──────────────┼──────────────┼─────────────┐
   ▼          ▼              ▼              ▼             ▼
Postgres    对象存储       模型网关       沙盒后端      外部系统
控制面、审计 业务包归档    OpenAI 兼容    本机隔离      连接器、HTTP
状态面      （OSS 等）                    或 Docker     接口要连的地方
```

| 部件 | 由谁提供 | 说明 |
| --- | --- | --- |
| 平台实例 | `xuanpu serve` | 一个进程一个实例。实例之间不互相通信，靠 Postgres 里的会话租约协调 |
| Postgres | 部署方 | 控制面（租户、凭据、业务包登记、密钥密文、会话、用量、审计）与状态面（线程目录、会话主存，每个租户一组表） |
| 对象存储 | 部署方 | 业务包归档的正本。S3 兼容服务，首选阿里云 OSS |
| 数据目录 | 部署方 | 每个实例自己的一份本地目录：业务包解包后的缓存、脚本沙盒的工作区、连接器的工作目录。丢了可以重建，**不在实例之间共用** |
| 模型网关 | 部署方 | 支持 Chat Completions 或 Responses 的地址，要支持流式与工具调用。各家模型的接入、限流、回退在网关上做 |
| 沙盒后端 | 部署方 | 本机隔离（macOS seatbelt / Linux bubblewrap）或 Docker。业务包的脚本与 stdio 连接器都在里面跑 |
| 反向代理 | 部署方 | 平台不做 TLS，默认只监听本机 |

## 安装

内核 `taifeng`（≥ 2026.10.1.10）从 PyPI 安装。`taifeng-sandbox`、`taifeng-stores` 尚未发布到 PyPI，目前从源码安装：这两个仓库与 `xuanpu` 检出在同一个父目录下。

```bash
cd xuanpu
uv sync --all-packages --all-extras      # --all-extras 带上 Postgres 与 S3 支持
uv run xuanpu --help
```

要求 Python 3.12+。

## 配置

配置是一个 YAML 文件，完整示例见 [`examples/xuanpu.yaml`](../../examples/xuanpu.yaml)，单机试用的最小示例见 [`examples/xuanpu.trial.yaml`](../../examples/xuanpu.trial.yaml)。

两条规则：

- 取值里的 `${NAME}` 在启动时用进程的环境变量展开。引用了没设置的变量会报错并拒绝启动，不会当成空串。**凭据都走这条路，不要直接写进文件。**
- 拼错的键会报错，不会被悄悄忽略。

### 顶层

| 键 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `data_dir` | 是 | — | 平台数据目录，必须是绝对路径。每个实例各用各的，只放缓存与工作目录；一个数据目录只能归一个进程（见[数据目录](#数据目录)） |
| `root_key_hash` | 是 | — | 根凭据的摘要，`xuanpu keygen root` 生成 |
| `master_keys` | 是 | — | 加密租户密钥的主密钥，`标识: base64` 的映射，至少一把。`xuanpu keygen master` 生成 |
| `primary_master_key` | 是 | — | 新写入的密钥用哪一把主密钥加密 |
| `models` | 是 | — | 模型网关，见下 |
| `listen.host`、`listen.port` | 否 | `127.0.0.1`、`8420` | 监听地址。默认只监听本机；对外提供服务时放在反向代理后面 |
| `control_plane` | 否 | `memory` | 控制面存储，见下 |
| `state` | 否 | `local` | 状态面后端，见下 |
| `sandbox` | 否 | `local` | 沙盒后端，见下 |
| `object_store` | 否 | `local` | 对象存储，见下 |
| `limits` | 否 | 见下 | 每个会话的上限与回收时间 |
| `audit.capture_requests` | 否 | `false` | 是否把每次发给模型的完整请求写进审计。内容敏感、体量大，排查问题时才开 |
| `metrics.token_hash` | 否 | 不开 | 指标抓取令牌的摘要，`xuanpu keygen metrics` 生成。给了才开 `GET /metrics`（见[监控与排查](#监控与排查)） |
| `public_url` | 否（启用飞书登录时必填） | 无 | 平台对外的地址，即浏览器与飞书访问平台用的那个，见下 |
| `identity` | 否 | 见下 | 员工身份（飞书登录）的平台级设置 |
| `lease_seconds` | 否 | `60` | 会话租约时长，最小 5。实例崩溃后，别的实例最多等这么久才能接手它的会话 |
| `shutdown_drain_seconds` | 否 | `30` | 停机时给在途对话多少秒跑完；HTTP 服务器等已打开的事件流发完的时限是它再加 5 秒（见[停机与滚动升级](#停机与滚动升级)） |
| `proactive_interval_seconds` | 否 | `1800` | 每用户语义业务评估的最短间隔（秒），最小60；调用使用既有模型、租户配额和员工计量 |
| `proactive_scan_seconds` | 否 | `60` | 主动规则与语义任务扫描频率（秒，1至300）；与模型评估间隔独立 |
| `instance_id` | 否 | 随机 | 本实例的标识，写进会话租约与 `/healthz`。多实例时建议显式设置成稳定且互不相同的值，方便按它做路由和排查 |

### `models`

`models.protocol` 默认 `chat`，也可为 `responses`（OpenAI Responses）或 `codex_responses`（Codex 网关方言）。三个档位共用协议，平台复用 taifeng 稳定客户端，不根据模型名猜测，也不自动换协议。Responses 的地址仍填以 `/v1` 结尾的基地址，客户端请求 `/responses`。模型名与可用协议由网关决定，不能用 Chat 请求失败来断定模型不存在。

Responses 当前只支持 `state.backend: local`；`control_plane.backend: postgres` 可以照常用于保存租户、用户与审计。配置了外部 Postgres 会话主存和非 Chat 协议时，启动立即报错，不等到第一次对话才失败。本地持续使用见[本地部署](local-trial.md)。

| 键 | 说明 |
| --- | --- |
| `base_url` | 网关地址，以 `/v1` 结尾 |
| `api_key` | 平台访问网关的凭据，所有租户共用。与 `api_key_secret` 二选一 |
| `api_key_secret` | 一个密钥名。给了它，平台就用**各租户自己**存在保管库里的这条密钥访问网关，网关可以据此按租户计费、限流。租户没配这条密钥时对话会失败（`secret_missing`） |
| `tiers.fast` / `balanced` / `deep` | 三个档位各对应网关上的哪个模型，三个都要配。每个档位有 `model`（必填）与 `timeout_seconds`（默认 300） |

业务包只写档位，不写模型名。换模型改这里再重启实例，不用动业务包。

### `control_plane`

| 键 | 说明 |
| --- | --- |
| `backend` | `memory`：进程退出即丢失，只用于试用。`postgres`：正式部署 |
| `postgres.dsn` | 连接串 |
| `postgres.schema` | 表所在的 schema，默认 `public`。表名都带 `xp_` 前缀 |
| `postgres.pool_min` / `pool_max` | 连接池大小，默认 1 / 10 |
| `postgres.connect_timeout_seconds` | 新建一条连接的时限（秒），默认 10 |
| `postgres.command_timeout_seconds` | 单条语句的时限（秒），默认 60。库卡住时请求与会话续期会报错，而不是一直挂着。连接池满时取连接没有时限、会排队，所以 `pool_max` 要按并发留足 |

### `state`

| 键 | 说明 |
| --- | --- |
| `backend` | `local`：线程目录与会话主存是数据目录 `threads/` 下的本机文件，只适合单实例。`postgres`：线程目录与会话主存都在库里，各实例从库里续接会话，不需要共享数据目录 |
| `postgres.*` | 同上。可以和控制面用同一个库、同一个 schema，但连接池是各自的一个 |

`postgres` 后端给每个租户建一组带专属前缀的表，**建表发生在该租户第一次对话时**。所以这个连接用的数据库角色需要在 schema 里建表的权限；建表受 `command_timeout_seconds` 约束，以后若有大表上的迁移，要临时调大状态面的这一项。

`postgres` 后端只支持 Chat 协议的模型客户端（内核对外部会话主存的限制）；选择 `responses` 或 `codex_responses` 时必须使用 `state.backend=local`；控制面仍可使用 PostgreSQL。

### `sandbox`

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `backend` | `local` | `local`：操作系统级隔离。`docker`：每个运行时一个脚本容器，业务包有 stdio 连接器时再加一个连接器容器 |
| `image` | `python:3.12-slim` | 容器镜像，里面必须有 `python3`（3.9+）。业务包脚本能用什么命令，取决于镜像里装了什么 |
| `memory_mb` | `1024` | 容器内存上限 |
| `cpus` | `1.0` | 容器 CPU 配额 |
| `docker_binary` | `docker` | docker 命令 |
| `docker_env` | 空 | 运行 docker 命令时的环境变量。平台不把自己的环境传给子进程，所以要在这里给出 `PATH`，以及 `HOME` 或 `DOCKER_HOST`，docker 客户端才找得到守护进程 |
| `connector_readable_roots` | 空 | 只用于 `local` 后端（`docker` 后端配了会报错）：连接器沙盒里额外可读的目录，必须是绝对路径。本机连接器沙盒用受限读策略，系统运行时目录（见[安全须知](#安全须知)）之外默认都读不到，所以要把连接器自己的解释器与依赖所在的目录列进来。Python 虚拟环境要列两个：虚拟环境目录（`sys.prefix`）与它背后的解释器安装目录（`sys.base_prefix`），少一个解释器就起不来。启动时解析符号链接后检查：目录必须存在；不能是 `/`；不能与 `data_dir`、本机对象存储目录相同或互为上下级。**列进来的目录对所有租户的连接器都可读**，只放解释器与依赖 |
| `connector_image` | 同 `image` | 只用于 `docker` 后端（`local` 后端配了会报错）：连接器容器用的镜像，里面必须有 `python3`（3.9+），还要装好业务包里 stdio 连接器需要的运行时（如 `node`），否则连接器起不来 |

`memory_mb`、`cpus`、`docker_binary`、`docker_env` 只对 `docker` 后端有意义。

业务包声明了 stdio 连接器时，每个运行时另开一个**连接器沙盒**，与脚本的沙盒分开：只读业务包目录，只写这个运行时的连接器工作目录 `<data_dir>/connectors/<租户目录>/<名>/<版本>/`，**允许出网**（脚本是否出网仍按清单）。连接器进程的 `HOME`、`TMPDIR`、`WORKSPACE` 都指向这个工作目录，环境里只有最小的 `PATH`、`LANG` 加上清单声明的变量与密钥。`docker` 后端下连接器容器挂载方式与脚本容器相同、带 `xuanpu.role=connectors` 标签，`memory_mb`、`cpus` 与进程数上限对两个容器各自生效。清单里没有 stdio 连接器就不开这个沙盒。设计理由见 [ADR 0013](../decisions/0013-connector-sandbox-and-remote-mcp.md)。

| 后端 | 前提 | 隔离强度 | 适用 |
| --- | --- | --- | --- |
| `local`（macOS） | 系统自带 `sandbox-exec`，SIP 开着 | 文件与网络访问受限，本机 Unix 套接字除 DNS 外不通；不限内存与 CPU。**沙盒里能读到同一用户下其他进程（包括平台进程）的完整参数与初始环境** | 只用于开发试用 |
| `local`（Linux） | 装了 `bubblewrap`，且允许创建用户命名空间 | 文件与网络访问受限，另有独立的进程、IPC 与挂载命名空间，看不到平台进程；不过滤 Unix 套接字，没有 seccomp、Landlock；不限内存与 CPU | 可用于生产，按[安全须知](#安全须知)放置平台的文件 |
| `docker` | 平台节点能访问 Docker 守护进程；平台进程与守护进程看到的 `data_dir` 路径一致 | 容器：去掉全部 capability、禁止提权、只读根文件系统、脚本容器默认断网、限制内存 / CPU / 进程数 | **生产推荐** |

沙盒后端不可用时，对话会以 `503 runtime_unavailable` 失败。平台**不会**退回到无隔离执行，连接器也不会退回到在平台节点上直接起。

生产环境用 Linux，推荐 `docker`：本机隔离不限制资源用量，一个失控的脚本或连接器可以占满节点；各后端剩下的缺口见[安全须知](#安全须知)。

### `object_store`

业务包归档与以后的员工文件放在对象存储里。**多实例部署必须用 `s3`**（阿里云 OSS 等 S3 兼容服务）；`local` 只落本机目录，只用于试用与单机。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `backend` | `local` | `local`：本机目录。`s3`：S3 兼容服务，需要安装 `xuanpu-server[s3]` |
| `local.root` | `<data_dir>/objects` | 存放对象的目录。给了必须是绝对路径 |
| `s3.endpoint` | — | 服务地址，如 `https://s3.oss-cn-hangzhou.aliyuncs.com`（OSS 的 S3 兼容 endpoint） |
| `s3.bucket` | — | 桶名 |
| `s3.region` | — | 地域，如 `cn-hangzhou` |
| `s3.prefix` | 空 | 加在所有对象键前面的前缀，如 `xuanpu/`；为空，或不以 `/` 开头、以 `/` 结尾 |
| `s3.access_key_id` | — | 访问密钥标识，用 `${VAR}` 从环境取，不要直接写进文件 |
| `s3.secret_access_key` | — | 访问密钥，同上，与主密钥同等对待 |
| `s3.addressing_style` | `virtual` | `virtual`：虚拟主机风格，阿里云 OSS 只接受这种。`path`：路径风格，本机 moto、MinIO 等用 |

`backend` 为 `s3` 时必须给出 `s3` 段。S3 客户端（boto3）会读取标准的 `AWS_*` 与 `HTTP(S)_PROXY` 环境变量，部署时注意不要残留无关的 AWS 配置或代理设置。

`s3.endpoint` 的写法按阿里云 OSS 的 S3 兼容文档；平台的 S3 实现经本机 moto 测过，尚未对真实的 OSS 做过冒烟，首次部署时先发布一个业务包、建一个会话确认能读写。

### `limits`

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `max_session_tokens` | `2000000` | 单个会话累计 token 的上限。业务包可以调低，不能调高 |
| `max_iterations` | `60` | 单轮对话里模型与工具往返次数的上限。同上 |
| `connector_timeout_seconds` | `60` | 连接器单次请求的超时。HTTP 与远端 MCP 连接器另受业务包里声明的 `timeoutSeconds` 约束，取两者中小的；远端 MCP 的握手与列工具也按它算 |
| `connector_startup_seconds` | `30` | 启动一个 stdio 连接器（起进程、握手、列出工具）或连上一个远端 MCP server（握手、列出工具）的时限。超时算连接器启动失败，对话以 `503 runtime_unavailable` 失败，已经起来的进程会被收回 |
| `session_idle_seconds` | `300` | 会话多久没有对话就释放它的引擎。会话保持活动，下次对话时从存储续接 |
| `runtime_idle_seconds` | `900` | 运行时（一个租户的一个业务包版本）多久没有会话使用就回收：关掉引擎池、连接器（进程与远端连接）、连接器沙盒、脚本沙盒 |

后两项是内存与承载量之间的旋钮：调小，常驻内存少，但续接更频繁、每次续接都要重新加载会话；调大则相反。

### `public_url` 与 `identity`

`public_url` 只写源——协议、主机与端口，如 `https://xuanpu.example.com`，不带路径、查询串或用户名密码。平台的接口与控制台都挂在根路径下（`/v1/…`、`/console/`），飞书的回调地址就是 `<public_url>/v1/auth/feishu/callback`；平台不支持挂在反向代理的子路径下。加载时规整成浏览器 `Origin` 头的写法：协议与主机名转小写，去掉默认端口（http 的 80、https 的 443）与末尾的 `/`。主机名只能含字母、数字、点与连字符（国际化域名写成 punycode），或是 IP 地址（IPv6 写在方括号里）；端口 1–65535；空白、`%` 编码一律拒绝。

它还用来校验员工浏览器发来的写请求确实来自平台自己的页面（`Origin` 必须等于它，见[接口文档的浏览器登录一节](../architecture/http-api.md#浏览器登录)）。**没配置 `public_url` 时平台不认登录 cookie**（员工无法用浏览器登录，API key 不受影响）；启用飞书登录必须配置。本地用 HTTP 开发时配：

```yaml
public_url: http://127.0.0.1:8420
identity:
  cookie_secure: false     # 登录 cookie 名变成 xuanpu_session，不带 Secure
```

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `identity.session_ttl_seconds` | `604800`（7 天） | 登录会话的默认有效期，60 到 2592000（30 天）。从登录时刻算起，不随使用延长；租户的飞书配置可以覆盖（同样的范围） |
| `identity.cookie_secure` | `true` | 登录 cookie 是否只走 HTTPS。为 `true` 时 cookie 名是 `__Host-xuanpu_session`（带 `Secure`，浏览器拒收别的子域写来的同名 cookie）；本地用 HTTP 开发时设为 `false`，cookie 名是 `xuanpu_session`、不带 `Secure`，否则浏览器不会带上它 |
| `identity.reconcile_interval_seconds` | `86400` | 按飞书通讯录对账的周期，最小 60 |
| `identity.default_tenant` | 无 | 登录链接省略 `tenant` 时用的租户，只有一家企业的部署用得上 |
| `identity.login_landing_path` | `/console/` | 登录完成或失败后的默认落地页。必须是站内路径：以 `/` 开头，不以 `//` 或 `/\` 开头，不含反斜杠与空白 |
| `identity.feishu_open_base`、`identity.feishu_accounts_base` | `https://open.feishu.cn`、`https://accounts.feishu.cn` | 飞书开放接口与授权页的基地址，与 `public_url` 一样只写源、同样规整。接 Lark 国际版，或测试时指向模拟服务端才需要改 |

飞书登录的接口与租户的飞书配置见[接口文档的飞书登录一节](../architecture/http-api.md#飞书登录)：没配 `public_url` 时不能启用飞书登录（配置接口报 `400`，登录跳回落地页带 `login_error=tenant_unavailable`）。通讯录对账与登录清理共用每 5 分钟的维护循环，逐个检查 active 且已绑定飞书企业的租户；`identity.reconcile_interval_seconds` 从上次成功完成起计算（默认每日）。

启用飞书登录时还要注意：

- **出站网络**：平台要能直连 `identity.feishu_open_base`（默认 `https://open.feishu.cn`），用来换令牌、取员工信息。飞书客户端**不读环境变量里的代理设置**（`HTTPS_PROXY` 等），出口只能是直连或网络层的透明代理。`identity.feishu_accounts_base`（默认 `https://accounts.feishu.cn`）是飞书授权页，由员工的浏览器访问，平台自己不连它。每次调飞书总时限 10 秒，响应体超过 1 MiB 按出错处理。
- **限速**：`/v1/auth/feishu/login` 与 `/v1/auth/feishu/callback` 不需要凭据，在反向代理上按来源地址限速（见下文[反向代理](#反向代理)）。平台自己也有几道防线：开始登录写的登录尝试 10 分钟就过期，`return_to` 最长 512 个字符；回调里 `state` 形状不对不查库、授权码形状不对不调飞书；每个租户同时在调飞书的登录最多 8 个，再来的最多排队 5 秒，等不到就跳回落地页带 `login_error=identity_unavailable`；匿名请求刷不出审计记录。
- **清理**：每个实例每 5 分钟清一次过期的登录尝试与登录会话（不需要配置）。清理出错只在标准错误里记一条，下一轮再试。
- **访问日志**：回调地址的查询串里有授权码。`xuanpu serve` 的访问日志把这一行的查询串抹成 `?…`（`GET /v1/auth/feishu/callback?… HTTP/1.1`）；2026-10-04 已用真实 CLI 与模拟飞书登录验证，见[接入手册的实际日志](feishu-login.md#6-本地验证边界与真实应用核对清单)。反向代理的访问日志要自己处理，比如不记这个路径的查询串。
- **事件回调**：离职事件经 `POST <public_url>/v1/identity/feishu/events/<租户>` 推给平台（见[接口文档的飞书事件回调一节](../architecture/http-api.md#飞书事件回调)），所以这个地址要能从飞书的服务器访问到（公网可达）。它不需要平台的凭据、只认飞书的签名，在反向代理上同样按来源地址限速；请求体最大 1 MiB。签名不符的请求只计指标 `xuanpu_feishu_events_total{result="bad_signature"}`、记一条告警，不写审计。

### 飞书仅登录模式

只配 App ID 与 App Secret 即可启用登录。`encrypt_key_ref`、`verification_token_ref` 可省略、为空或 null；接口以 null 返回未配引用，`events_enabled` 仅在两项齐备时为 true。可以先保存 Verification Token、以后再启用 Encrypt Key。事件未启用时回调拒绝请求，不接受明文事件或明文地址验证；后续补齐密钥即可启用，不影响已有用户与企业绑定。

控制面迁移 `0005_identity_optional_event_secrets.sql` 允许事件密钥引用为空，保留旧配置。升级时先运行迁移，再启动新版本。

## 初次部署

```bash
# 1. 生成凭据。根凭据交给平台管理员保管；摘要和主密钥进配置（经环境变量）
xuanpu keygen root
xuanpu keygen master
xuanpu keygen metrics      # 可选：要开指标端点时生成抓取令牌（见「指标」）

# 2. 建控制面的表（状态面的表在每个租户第一次对话时自动建）
xuanpu migrate --config /srv/xuanpu/xuanpu.yaml

# 3. 启动
xuanpu serve --config /srv/xuanpu/xuanpu.yaml

# 4. 探活
curl http://127.0.0.1:8420/healthz
```

控制面的表结构落后于代码时，`serve` 会在启动时直接报错退出，不会带着旧表结构跑。

之后开通租户、签发凭据的步骤见[快速上手](quickstart.md#3-开通租户)。

### 反向代理

平台只说 HTTP。对外提供服务时前面放一个做 TLS 的反向代理，并注意事件流：

- 关闭响应缓冲（平台已带 `X-Accel-Buffering: no`，nginx 会照办）；
- 读超时大于 15 秒（平台每 15 秒发一次保活）；
- 发布业务包的请求体最大 64 MiB；
- 启用飞书登录时，对 `/v1/auth/feishu/*` 按来源地址限速（比如每个地址每分钟几十次）。这两个接口不需要凭据，正常员工一次登录各访问一次。`/v1/identity/feishu/events/*`（飞书事件回调）也不需要凭据，同样限速，并且要允许飞书的服务器访问到。

`/console/` 是控制台的静态页面，不需要的话在代理上屏蔽这个路径。`/metrics` 只给 Prometheus 抓，不要对外暴露。框架自动生成的接口描述 `/openapi.json` 与 `/docs`、`/redoc` 都是关着的：

```bash
curl -i localhost:8420/openapi.json
# HTTP/1.1 404 Not Found
# …
# {"detail":"Not Found"}
```

## 多实例

实例是无主从的：每个实例都能处理任何请求，加实例就是加承载量。要满足五件事：

1. **控制面用 Postgres**，所有实例指向同一个库。
2. **`state.backend` 用 `postgres`**，会话主存与线程目录才在库里，别的实例才接得了会话。
3. **`object_store.backend` 用 `s3`**，所有实例指向同一个桶（与前缀）。业务包发布到哪个实例，归档都写进对象存储；别的实例用到时自己取、自己解进缓存。
4. **`data_dir` 各实例各用各的**，放在本机盘上就行，不需要共享卷。里面只有缓存与工作目录（见[数据目录](#数据目录)）。**不要让多个实例（或同一台机器上的多个进程）共用一个数据目录**：缓存的锁只在进程内，启动时还会清掉暂存目录的残留，共用会互相删掉对方正在用的文件。
5. **`root_key_hash`、`master_keys` 各实例一致**；`instance_id` 各不相同。

### 会话粘性

一个会话同一时刻只由一个实例处理（单写者）。实例处理会话时持有它的租约，定期续期。请求落到别的实例上，会得到：

```json
{"error": {"code": "session_busy", "message": "会话 … 正由另一个实例持有", "owner": "xuanpu-2"}}
```

所以负载均衡要**按会话粘性路由**：路径里的会话标识（`/v1/sessions/{id}/…`）相同的请求发到同一个实例。平台不自带这层路由。建会话、发布业务包、查审计这些请求可以发给任意实例。

租约什么时候放手：

| 情况 | 多久之后别的实例可以接手 |
| --- | --- |
| 会话空闲超过 `session_idle_seconds` | 立即（持有者主动释放） |
| 实例正常停机 | 立即（停机时释放全部会话） |
| 实例崩溃 | 最多 `lease_seconds` |

接手的实例从库里续接会话，对话历史完整；崩溃时正在进行的那一轮会丢，调用方需要重发。

租约的几个边界情况：

- 实例准备引擎（取业务包、组装运行时、续接历史）期间没人续期。准备好之后、放进本实例之前，平台会再确认一次持有；这期间租约到期过（哪怕又被自己续上了），这次请求按 `503 runtime_unavailable` 失败，**调用方直接重试即可**。
- 续期与回收空闲会话、空闲运行时在同一个维护循环里串行执行，维护卡住（关闭运行时很慢、库迟迟不返回）太久时，租约可能在持有者还在写时到期。
- 租约期限按各实例自己的墙钟判断：**各实例要跑 NTP**。时钟偏差超过「租约时长减续期间隔」时，两个实例可能短暂同时写一个会话。

### 停机与滚动升级

`serve` 收到 `SIGTERM` 后：

1. 不再接受新连接；
2. 已经打开的事件流继续，直到那一轮对话结束，最多等 `shutdown_drain_seconds` 加 5 秒（带小数的向上取整到整秒）。到期还没发完的事件流被断开（调用方看到连接中断，可以经回放接口补齐已审计的事件），那几轮对话转到后台接着跑。再发一次终止信号会跳过等待、强制退出；
3. 进入平台关闭流程后，先取消并等待身份维护任务，避免对账继续停用员工或占用租约。取消时的审计和租约释放各有最多 30 秒的清理时限；
4. 员工停用后还在后台跑的收尾（飞书离职事件触发的关会话，见[员工停用与离职](#员工停用与离职)）再等最多 5 秒，到期取消；
5. 调用方已经断开、但还在后台跑的对话（包括第 2 步到期被断开的），再等最多 `shutdown_drain_seconds`，到期取消；
6. 释放本实例持有的全部会话，写完审计，退出。

所以从收到 `SIGTERM` 到退出，需预算约 `2 × shutdown_drain_seconds + 10` 秒，加上身份维护取消清理最多两段 30 秒，以及释放会话、写审计的时间；这不是所有存储操作的全局硬上限。滚动升级时一个一个地停、升、起。编排系统的强杀等待时间（如 Kubernetes 的 `terminationGracePeriodSeconds`）要比这个长：被强杀的实例来不及释放会话，别的实例要等租约到期才能接手。

## 凭据与密钥

| 东西 | 在哪 | 丢了会怎样 |
| --- | --- | --- |
| 根凭据 | 平台管理员手里。平台只存摘要 | 重新生成一个，换掉配置里的 `root_key_hash`，重启 |
| 主密钥 | 配置（经环境变量） | **租户存的密钥全部无法解开**，只能让租户重新录入 |
| 租户 API key | 调用方手里。平台只存摘要 | 吊销后重新签发 |
| 租户密钥 | Postgres 里，AES-256-GCM 加密，与租户和密钥名绑定 | — |

### 轮换主密钥

```yaml
master_keys:
  k1: ${XUANPU_MASTER_KEY_OLD}
  k2: ${XUANPU_MASTER_KEY_NEW}
primary_master_key: k2          # 新写入的用 k2；k1 留着，旧密文还要靠它解
```

1. 加入新密钥并把 `primary_master_key` 指向它，滚动重启全部实例。
2. 让每个租户把密钥重新写一遍（`PUT /v1/secrets/<名字>`，取值不变也行）——重写后就是用新主密钥加密的。**目前没有批量重新加密的工具。**
3. 确认没有旧密文之后，从配置里删掉旧主密钥。

删早了的后果：用旧主密钥加密的密钥在使用时报 `500 secret_corrupted`，重新录入才能恢复。

### 租户管理

| 操作 | 接口 |
| --- | --- |
| 新建（可带配额） | `POST /v1/admin/tenants` |
| 停用 / 启用 | `PUT /v1/admin/tenants/{id}/status`，`{"status": "suspended"}` 或 `"active"` |
| 修改配额 | `PUT /v1/admin/tenants/{id}/quota`，整体替换（见下） |
| 签发 API key | `POST /v1/admin/tenants/{id}/keys` |

停用后，该租户的全部 API key 立即失效；已经在进行的那一轮对话会跑完。

改配额是**整体替换**：请求体里没给的项恢复为不限，不保留原值。只想调一项，先 `GET /v1/admin/tenants` 看现值，把另外两项照原样带上：

```bash
curl -X PUT localhost:8420/v1/admin/tenants/acme/quota \
     -H "Authorization: Bearer $ROOT_KEY" -H 'Content-Type: application/json' \
     -d '{"max_active_sessions": 200, "max_daily_tokens": 10000000}'
# {"tenant_id":"acme","name":"Acme","status":"active","quota":{"max_active_sessions":200,"max_daily_tokens":10000000,"max_concurrent_turns":null},"created_at":1790898324.358114}

curl -X PUT localhost:8420/v1/admin/tenants/acme/quota \
     -H "Authorization: Bearer $ROOT_KEY" -H 'Content-Type: application/json' \
     -d '{"max_daily_tokens": 0}'
# {"error":{"code":"invalid_request","message":"请求参数不合法","problems":["body.max_daily_tokens：Input should be greater than or equal to 1"]}}
```

新值立即生效：配额每次检查都现读，下一次建会话、发消息就按新值。已经超出新上限的不受影响（活动会话不会被关掉），只拦之后的。每次修改在该租户的审计里记一条 `tenant_quota_changed`，带改之前与改之后的值。

目前没有删除租户的接口。

### 员工停用与离职

员工有三种途径被停用：租户管理员调 `PUT /v1/users/{id}/status`（原因 `admin`）、飞书推来的离职事件（原因 `feishu_resigned`、`feishu_frozen` 或 `feishu_exited`，见[接口文档的飞书事件回调一节](../architecture/http-api.md#飞书事件回调)），以及按飞书通讯录的每日对账。三种都走同一条停用流水线：

1. 把员工标为停用，记下原因与时间；
2. 删掉他的全部登录会话：浏览器里的 cookie 下一个请求就是 `401`，代他调用是 `403`。然后，状态真的变了的，才记一条审计 `user_disabled`、指标 `xuanpu_users_disabled_total{reason}` 计一次。审计排在最后，是为了它写不进去时（这次停用照样报错）停用与删登录会话都已经落下；代价是重试时状态已是停用，这条 `user_disabled` 不会补写——平台日志里有错误、`xuanpu_audit_write_failures_total` 会计一次；
3. 依次执行停用之后的收尾。现在只有一项：关闭他名下全部活动会话（审计 `session_closed`，`actor` 是 `system:user-disabled`），正在进行的对话随之取消。某项收尾失败时记一条 `user_cleanup_failed`（哪一项、什么异常类型；细节在平台日志「停用后的收尾」里），不影响其余的。

- **重跑收尾**：对已经停用的员工再停用一次（`PUT /v1/users/{id}/status`，`{"status": "disabled"}`），状态不变、不再记 `user_disabled`，但第 3 步全部重跑。看到 `user_cleanup_failed` 时这样重试；响应里的 `cleanup_failed` 列出这一次仍然失败的收尾。
- **离职事件的收尾在后台跑**：飞书要求 3 秒内响应，事件回调同步做完前两步就回 `200`，关会话放在后台。每个实例同时在跑的后台收尾最多 8 个，多出来的排队，一阵批量离职不会占满连接池、拖慢正常请求；同一名员工已有后台收尾在跑时不另开，只在它跑完之后再跑一轮（期间来的重推不论几次都并成这一轮），所以不会有两个收尾同时关他的同一个会话。实例停机时最多再等 5 秒，没跑完的取消、记一条告警；实例崩溃时没跑完的直接丢了。两种情况都有兜底：主人已停用的会话，任何人都不能再发消息、送审批结果（`403 session_owner_disabled`），空闲会话照常回收；要彻底关掉，再停用他一次。
- **会话在别的实例上**：关闭以控制面里的记录为准，立即生效，之后任何实例都不再接受这个会话的消息与审批结果。持有它的那个实例不会收到通知：正在跑的那一轮跑完为止（要审批的写操作停在审批点），引擎按空闲回收（`limits.session_idle_seconds`）或停机时释放。与在别的实例上调 `DELETE /v1/sessions/{id}` 是同样的行为。
- **离职的管理员也会被停用**：管理员经接口降级、停用别人时，平台不让租户里一名启用的管理员都不剩（`409 conflict`）；但离职事件与对账不受这一条限制——离职的人必须停用，哪怕他是最后一名管理员。这时由平台管理员兜底：用根凭据给这个租户签发一把租户管理员的 API key，再用它把某名员工设为管理员：

  ```bash
  curl -X POST localhost:8420/v1/admin/tenants/acme/keys -H "Authorization: Bearer $ROOT_KEY" \
       -H 'Content-Type: application/json' -d '{"name": "bootstrap-admin", "role": "tenant_admin"}'
  # {"key":"xp_7321ed4ed6a1_…","key_id":"7321ed4ed6a1","tenant_id":"acme","name":"bootstrap-admin","role":"tenant_admin","act_as_users":false,"created_at":1790948455.255163,"revoked_at":null}

  curl "localhost:8420/v1/users?status=active" -H "Authorization: Bearer xp_7321ed4ed6a1_…"
  # {"users":[{"user_id":"usr_bc35eab7ea2b12fe8eddc08a","display_name":"张三","avatar_url":null,"role":"tenant_member","status":"active","disabled_reason":null,"created_at":1790948467.995469,"last_login_at":1790948467.995498,"disabled_at":null,"unverified_since":null}],"next_after":null}

  curl -X PUT localhost:8420/v1/users/usr_bc35eab7ea2b12fe8eddc08a/role -H "Authorization: Bearer xp_7321ed4ed6a1_…" \
       -H 'Content-Type: application/json' -d '{"role": "tenant_admin"}'
  # {"user_id":"usr_bc35eab7ea2b12fe8eddc08a","display_name":"张三","avatar_url":null,"role":"tenant_admin","status":"active",…}

  curl -X DELETE localhost:8420/v1/keys/7321ed4ed6a1 -H "Authorization: Bearer xp_7321ed4ed6a1_…"
  # {"revoked":true}
  ```

  最后一步吊销这把临时的 key。第一个管理员也是这样产生的。他是唯一的管理员时，再想把他降为成员是 `409`：`{"error":{"code":"conflict","message":"这是租户里最后一名启用的管理员，不能降级或停用"}}`。离职事件把他停用之后，审计里按 `user_id` 查得到全过程：

  ```
  user_created      user:usr_bc35…   {"provider": "feishu", "display_name": "张三", "role": "tenant_member"}
  login_succeeded   user:usr_bc35…   {"provider": "feishu"}
  user_role_changed key:7321ed4ed6a1 {"before": "tenant_member", "after": "tenant_admin"}
  user_disabled     system:feishu-event {"reason": "feishu_resigned", "event_id": "evt_local_1"}
  ```

  （本机试用实例上实际跑的：飞书接口指向一个本地的模拟服务端，员工登录一次之后设为管理员，再推一条签了名的离职事件。）
- **重新启用**：`PUT /v1/users/{id}/status`，`{"status": "active"}`，只改状态、记 `user_enabled`。已关闭的会话不恢复，员工重新登录即可。员工在飞书里仍是离职状态的，以后的对账会再把他停用。

## 数据目录

每个实例一份，必须是本机目录的绝对路径。正式部署（`state.backend: postgres`、`object_store.backend: s3`）下，里面只有可以重建的东西：

```
<data_dir>/
├── cache/
│   ├── packages/<租户目录>/<包名>/<版本>/          业务包解包后的文件，用到时从对象存储重建
│   ├── packages/<租户目录>/<包名>/.<版本>.digest   这个版本目录对应的归档指纹
│   └── .staging/                                 发布与重建缓存时的暂存目录，启动时清空
├── workspaces/<租户目录>/<包名>/<版本>/            脚本沙盒的工作区
└── connectors/<租户目录>/<包名>/<版本>/            连接器沙盒的工作目录（业务包有 stdio 连接器才有）
```

试用与单机配置（`state`、`object_store` 都是 `local`）下另外还有两样，它们**不能丢**：

```
<data_dir>/
├── objects/tenants/<租户>/packages/<包名>/<版本>/<sha256>.zip   本机对象存储：业务包归档的正本
└── threads/<租户目录>/<包名>/<版本>/                            会话主存（内核的 JSONL 文件与线程索引）
```

`<租户目录>` 是「租户标识-摘要」，如 `acme-822b33ad`。一台用试用配置跑过几个业务包的实例：

```bash
ls <data_dir>
# cache  connectors  objects  threads  workspaces
```

几条规则：

- **一个数据目录只能归一个进程。** 业务包缓存的锁只在进程内；启动时会清空 `cache/.staging/`，别的进程正在用的暂存目录会被删掉。
- **`cache/` 可以在实例停机时整个清空**，下次用到某个业务包版本时从对象存储重新取归档、核对指纹、解包。运行中清空要重启实例（运行时手里拿着缓存目录的路径）。节点断电或宿主机崩溃之后，建议停机清空一次 `cache/`：缓存写入不做 `fsync`，指纹文件在、内容没落盘的情况查不出来。
- 缓存不做淘汰：版本不可变、体积有限，只增不减。
- `workspaces/`、`connectors/` 不自动清理，不跨实例共享，也不必备份：脚本与连接器不该把要长期保留的东西放在这里。`docker` 后端下这两处的文件可能属 root。
- 版本号的预发布段可以含大写字母。在大小写不敏感的文件系统上（如 macOS 默认），只差大小写的两个版本（`1.0.0-RC`、`1.0.0-rc`）缓存目录相撞，交替使用时反复重建。

## 备份

| 备份什么 | 里面有什么 |
| --- | --- |
| Postgres | 租户、凭据摘要、业务包登记、密钥密文、会话记录、用量、**审计**；`state.backend: postgres` 时还有**会话主存**与线程目录 |
| 对象存储 | 业务包归档的正本。用 `local` 对象存储时就是 `<data_dir>/objects/`，**不能丢** |
| 主密钥 | 没有它，库里的密钥密文解不开 |
| `<data_dir>/threads/` | 只在 `state.backend: local` 时有：会话主存，**不能丢** |

`<data_dir>` 里的其余部分（`cache/`、`workspaces/`、`connectors/`）不必备份。

Postgres 与对象存储要一起恢复：业务包登记里记着归档的键与指纹，只恢复其中一样，取用时会报归档缺失（`503`）或指纹不符（`400 package_invalid`）。

## 升级

```bash
xuanpu migrate --config /srv/xuanpu/xuanpu.yaml     # 先迁移表结构
# 再滚动重启实例
```

迁移在一个事务里执行，并持有咨询锁，多处同时运行也只会执行一次。新版本的实例遇到旧表结构会拒绝启动，所以顺序是先迁移、后升级实例。

### 从第一版升级

先按下面的 P1 控制面迁移说明安排维护窗口；若还未完成 P0，再同时处理状态面与业务包归档变更。

### P1 身份迁移与后续枚举升级

P1 控制面迁移为 `0004_identity.sql`，增加用户、外部身份、登录、配置、对账与按用户用量表，并给会话和审计增加 `user_id`。旧会话没有主人，保持 `NULL`，不会自动分给员工。身份状态跟控制面一样持久化；正式部署须使用 PostgreSQL，内存控制面重启即丢。

迁移在事务中使用普通 `CREATE INDEX`，**不是 `CREATE INDEX CONCURRENTLY`**；给大审计表建索引会阻塞写入，升级前估算表大小、备份并安排维护窗口，不能把首次 P1 迁移当作无停顿滚动升级。迁移完成再启动新版本。

用户 ID 是 `usr_` 加 24 位十六进制。模型严格校验角色、停用原因等枚举；以后引入新枚举值，应先让所有读取实例升级到能识别新值的版本，再开放写入新值。不要让旧实例读取不认识的新角色或停用原因。

首次升级后的飞书应用、权限、密钥与首位管理员配置见[接入飞书登录](feishu-login.md)。对账未验证标记需要人工处理，不能根据查不到用户就手工批量停用。

### P0 状态面升级

第一版里业务包解在共享的数据目录 `packages/` 下，会话主存是数据目录 `threads/` 下的 JSONL 文件，`state.backend: postgres` 只是把消息另外镜像进库。升级后：

- 业务包归档改放对象存储，登记记录的 `location` 改存对象存储的键。第一版的登记没有迁移工具（第一版没有正式部署过）：升级后用到第一版发布的版本时报 `503 runtime_unavailable`（平台日志里是「登记的归档键 … 与按记录算出的 … 不符」），同名同版本又不能再发布（`409`）。只能升版本号重新发布，新会话用新版本。
- `state.backend: postgres` 时，会话主存直接用库里的那张消息表，不再读数据目录里的 JSONL。**第一版的镜像是尽力写入的，可能缺条**：升级前有活动会话的，以节点本地的 JSONL 为准，镜像缺了的会话升级后历史不全；本地 JSONL 里有、库里完全没有的线程，续接时报错（`resume_thread_id … not found`），对话失败。所以升级前先让会话都结束（或确认镜像完整），并保留旧的数据目录，必要时回退。
- 多实例改为各用各的数据目录，原来的共享卷可以在确认不再需要回退之后撤掉。

## 监控与排查

| 看什么 | 怎么看 |
| --- | --- |
| 实例是否存活 | `GET /healthz`，不需要凭据，返回版本与实例标识 |
| 请求日志 | 标准输出（uvicorn 的访问日志） |
| 平台的告警与错误 | 标准错误。连接器启动与调用失败（已脱敏的排障线索）、审计写入失败、会话续期失败、停机时被取消的对话数都在这里 |
| 某个会话发生了什么 | 审计：`GET /v1/audit?session_id=…`（见[审计与用量](audit-and-usage.md)） |
| 用量 | `GET /v1/usage` |
| 指标 | `GET /metrics`，Prometheus 文本格式，见下 |

### 指标

```bash
uv run xuanpu keygen metrics
# 指标抓取令牌（只显示这一次，交给 Prometheus）：
#   <令牌>
# 写进配置文件的 metrics.token_hash：
#   <摘要>
```

摘要写进配置 `metrics.token_hash`（经环境变量展开），令牌交给 Prometheus：

```yaml
scrape_configs:
  - job_name: xuanpu
    authorization:
      credentials_file: /etc/prometheus/xuanpu-metrics-token
    static_configs:
      - targets: ["10.0.0.11:8420", "10.0.0.12:8420"]
```

每个实例只报自己的数，按实例抓。没配 `metrics.token_hash` 时 `/metrics` 是 404，令牌不对是 401。一台试用实例跑过几轮对话（批准一次、拒绝一次、停下一轮）之后的实际输出（`curl -s localhost:8420/metrics -H "Authorization: Bearer $METRICS_TOKEN" | grep -v '^#'`，节选）：

```
xuanpu_sessions_live 2.0
xuanpu_turns_in_flight 0.0
xuanpu_runtimes_live 2.0
xuanpu_turns_total{outcome="completed"} 4.0
xuanpu_turns_total{outcome="suspended"} 2.0
xuanpu_turns_total{outcome="cancelled"} 1.0
xuanpu_turns_total{outcome="resume_rejected"} 0.0
xuanpu_connector_calls_total{decision="allowed",transport="http"} 2.0
xuanpu_connector_calls_total{decision="rejected",transport="http"} 1.0
xuanpu_connector_calls_total{decision="allowed",transport="remote"} 0.0
xuanpu_audit_write_failures_total 0.0
xuanpu_gate_backfill_failures_total{stage="unconfirmed"} 0.0
xuanpu_usage_estimated_total 1.0
```

| 指标 | 类型 | 标签 | 含义 |
| --- | --- | --- | --- |
| `xuanpu_sessions_live` | gauge | — | 本实例当前持有引擎的会话数 |
| `xuanpu_turns_in_flight` | gauge | — | 本实例当前进行中的对话数 |
| `xuanpu_runtimes_live` | gauge | — | 本实例当前组装着的运行时数 |
| `xuanpu_turns_total` | counter | `outcome` | 结束的对话轮数：`completed`、`failed`、`suspended`、`cancelled`，以及送回审批被内核拒绝的 `resume_rejected`、只核销了一部分的 `resume_partial` |
| `xuanpu_connector_calls_total` | counter | `transport`、`decision` | 过闸记录数：`allowed`、`denied`（策略拒绝）、`rejected`（人工拒绝） |
| `xuanpu_audit_write_failures_total` | counter | — | 审计写入失败的次数。大于 0 说明审计不完整，查数据库 |
| `xuanpu_gate_backfill_failures_total` | counter | `stage` | 人工拒绝的过闸记录没补上的次数（`incomplete` 待办缺字段、`write` 写库失败、`unconfirmed` 等不到内核对那次送回的结论）。送回在内核里排队时就被停下的不算：内核没处理它，待办还在，可以再送 |
| `xuanpu_usage_estimated_total` | counter | — | 被停下的一轮按上下文估算入账的次数 |
| `xuanpu_usage_write_failures_total` | counter | — | 一轮对话结束时用量写库失败的次数（先写租户总量，成了再写按员工的；哪笔失败都计一次，租户那笔失败时员工那笔不写）。这一轮照常结束，但没写成的不再补记：`/v1/usage` 与当天的 token 配额、`/v1/usage/users` 会少算，查数据库 |
| `xuanpu_logins_total` | counter | `result` | 飞书登录的结果：`succeeded`，或失败原因（`feishu_denied`、`not_in_app_scope`、`state_invalid`、`tenant_mismatch`、`tenant_unavailable`、`user_disabled`、`identity_unavailable`，含义见[接口文档](../architecture/http-api.md#飞书登录)）。`identity_unavailable` 持续上涨说明平台连不上飞书或应用凭据不对，看平台日志里「飞书登录失败」的告警 |
| `xuanpu_users_disabled_total` | counter | `reason` | 被停用的员工数，按原因：`admin`（管理员停用）、`feishu_resigned`、`feishu_frozen`、`feishu_exited`（飞书离职事件或对账）。只在状态确实从启用变成停用时计，重复停用不计 |
| `xuanpu_feishu_events_total` | counter | `result` | 飞书事件回调的请求数：`url_verification`（回了 URL 验证）、`accepted`（处理了离职事件）、`ignored`（合法但无事可做：别的事件类型、平台里没有这名员工、租户还没绑定飞书企业）、`bad_signature`（签名不符，或没有签名又不是 URL 验证）、`rejected`（签名对了，Verification Token、App ID 或飞书企业对不上）、`bad_request`（请求体超过 1 MiB，或签名对了而内容解不开）、`unknown_tenant`（租户没有可用的飞书配置）、`internal_error`（平台内部出错，回了 `500`：存储报错、保管库里的密文解不开等，看平台日志）。`bad_signature`、`rejected` 持续出现说明飞书后台的 Encrypt Key、Verification Token 与平台保管库里的不一致，或有人在刷这个地址；看平台日志里「飞书事件回调…被拒」的告警 |

指标不带租户标签（避免标签基数随租户数膨胀）；按租户看用量用 `GET /v1/usage`。

常见问题：

| 现象 | 查什么 |
| --- | --- |
| 启动时报表结构落后 | 没跑 `xuanpu migrate` |
| 启动时报配置引用了未提供的变量 | 环境变量没设 |
| 启动时报 `sandbox.connector_readable_roots` 不合要求 | 目录不存在、是 `/`，或与 `data_dir`、本机对象存储目录相同或互为上下级 |
| 对话返回 `503`，提示隔离后端不可用 | Linux 上没装 `bubblewrap` 或不允许用户命名空间；Docker 后端连不上守护进程（检查 `docker_env`） |
| 对话返回 `503`，提示连接器启动失败 | 业务包里的 stdio 连接器在连接器沙盒里起不来，或远端 MCP 连不上。细节只在平台日志里，见下文[连接器起不来](#连接器起不来) |
| 对话返回 `503`，提示「请重试」 | 会话的持有在准备期间中断过（租约到期过）、准备期间别的实例换了它的线程，或取租约时持有者刚好变化。直接重试 |
| 对话返回 `503`，提示对象存储暂时不可用、归档缺失 | 对象存储连不上或凭据不对（日志里有异常）；归档缺失是登记记录指向的对象不在了，检查对象存储与 Postgres 是不是一起恢复的 |
| 对话返回 `503`，提示本实例正在停机 | 实例收到了终止信号，换一个实例 |
| 对话返回 `400 package_invalid`，提示归档与登记的指纹不符 | 对象存储里的归档被改过，或对象存储与 Postgres 不是同一时刻的备份 |
| 大量 `409 session_busy` | 负载均衡没有按会话做粘性路由 |
| 审计里出现 `audit_gap` | 事件产生得比平台消费得快，内核丢了事件。通常是节点过载 |
| Docker 沙盒残留容器 | 实例被强杀时来不及清理，见下文[清理残留](#清理残留) |

### 连接器起不来

调用方只拿到 `503 runtime_unavailable` 和异常类型；平台日志（标准错误）里有一条「连接器 … 启动失败」，带按值脱敏后的异常文本、退出码与 stderr 末尾约 2 KiB（注入给它的密钥明文都换成 `***`）。一个往 stderr 写了带密钥的一行、再以退出码 3 退出的 stdio 连接器，平台日志里是这样的：

```
连接器 crm 启动失败：RuntimeError: 'mcp connection closed'；退出码 3；stderr 末尾 'login failed with token=***\n'
请求失败：连接器 'crm' 启动失败：RuntimeError（runtime_unavailable）
```

常见的原因：

- 本机后端读不到连接器的解释器或依赖：装到系统目录，或把所在目录列进 `sandbox.connector_readable_roots`（Python 虚拟环境要把 `sys.prefix` 与 `sys.base_prefix` 两个都列上）。
- `docker` 后端的 `connector_image` 里没装连接器要的运行时（如 `node`）。
- 第一次启动现场下载依赖（`uvx`、`npx` 这类写法），超过了 `limits.connector_startup_seconds`。运行时要预装进镜像或节点。
- 连接器想写工作目录之外的地方：它只能写 `$HOME`、`$TMPDIR`、`$WORKSPACE`（都指向它的工作目录）。
- 清单白名单里的工具 server 没有提供：日志里是另一条「连接器 … 缺少声明的工具 …」。
- 远端 MCP：地址不通、证书不被信任（平台不读环境里的代理与证书配置）、凭据不对；超时也算在 `connector_startup_seconds` 里。

### 清理残留

`docker` 后端下实例被强杀时，容器来不及销毁。平台起的容器都带 `xuanpu.role`（`scripts` 是脚本容器，`connectors` 是连接器容器）、`xuanpu.tenant`、`xuanpu.package`、`xuanpu.version` 标签。先停掉这台节点上的实例，再按标签找出来清掉（下面是一次实际的强杀之后）：

```bash
docker ps -a --filter label=xuanpu.role \
    --format 'table {{.ID}}\t{{.Label "xuanpu.role"}}\t{{.Label "xuanpu.package"}}\t{{.Status}}'
# CONTAINER ID   role         package    STATUS
# 309866b2f677   scripts      crm-desk   Up 19 seconds
# 0cc5249cf557   connectors   crm-desk   Up 19 seconds

docker rm --force $(docker ps -aq --filter label=xuanpu.role)
# 309866b2f677
# 0cc5249cf557
```

同一台 Docker 主机上有几个实例时，只清已经停掉的那个实例的：给 `DockerSandboxProvider` 传 `labels`（例如部署标识）的话可以按它再筛一层；用 `xuanpu serve` 时容器上没有实例标识，就只在整台主机的实例都停下时清。

本机后端：Linux 的 bubblewrap 带 `--die-with-parent`，平台进程退出时沙盒里的进程随之被杀。macOS 上靠连接器自己在标准输入关闭时退出（常见的 MCP server 都这样，样例里的连接器实测随之退出）；不退出的，按工作目录找出来（连接器的工作目录在业务包缓存里）：

```bash
lsof -nP -d cwd | grep '<data_dir>/cache/packages'
# 没有输出就是没有残留
```

## 容量与并发

平台实例本身几乎不做计算，时间都花在等模型和等外部接口上。承载量主要由这几样决定：

- **常驻内存**：每个活着的会话一个引擎，每个「租户 + 业务包版本」一个运行时（含连接器进程、连接器沙盒与脚本沙盒）。靠 `session_idle_seconds`、`runtime_idle_seconds` 回收。`docker` 后端下每个运行时最多两个容器，`memory_mb`、`cpus` 与进程数上限各算一份。
- **模型网关的限额**：平台不排队，网关拒绝就是这一轮失败。
- **Postgres 连接**：每个实例两个连接池（控制面、状态面各一个，同一个库也是两个），各自最多 `pool_max` 条。库的 `max_connections` 至少要有「实例数 ×（控制面 `pool_max` + 状态面 `pool_max`）」，再给迁移与运维留余量。默认每个实例最多 20 条，Postgres 默认的 `max_connections`（100）只够约 5 个实例。`pool_max` 按每个实例的并发对话数定：取连接没有时限，池满时请求与会话续期都会排队。
- **租户配额**：`max_active_sessions`、`max_daily_tokens`、`max_concurrent_turns`。注意 `max_concurrent_turns` 是每个实例各自计数的，N 个实例下租户实际能同时跑的对话数最多是它的 N 倍。

审计表只增不减，平台不做清理与归档。按你们的留存要求自行分区或归档（见[审计与用量](audit-and-usage.md#留存)）。

## 安全须知

部署方要自己把住的几处，平台替你做不了。设计上的取舍见 [ADR 0013](../decisions/0013-connector-sandbox-and-remote-mcp.md)。

### 沙盒后端怎么选

业务包的脚本与 stdio 连接器都在沙盒里跑，信任边界就是沙盒后端：

| 后端 | 结论 |
| --- | --- |
| macOS 本机（seatbelt） | **只用于开发试用，不能承载不受信任的业务包。** 沙盒里的进程能读到同一用户下其他（非系统）进程的完整参数与初始环境，平台进程环境里的主密钥、数据库连接串、网关凭据都在其中——内核对这种读取（`KERN_PROCARGS2`）不走沙盒检查，seatbelt 配置拦不住。出网时还能连本机任意 TCP 端口；隔离依赖 SIP 开着 |
| Linux 本机（bubblewrap） | 可用于生产，前提是按下一节放好平台的文件。启动器只用空环境，连接器的环境经封口的 memfd 传进沙盒，`LD_PRELOAD` 一类变量到不了沙盒外；有独立的进程命名空间，看不到平台进程。剩下的面：不过滤 Unix 套接字（挂进沙盒的目录里的套接字连得上，出网时宿主的抽象 Unix 套接字也连得上；受限读不挂 `/run`、`/var/run`，宿主服务的套接字默认看不到），没有 seccomp、Landlock；不限资源 |
| `docker` | **生产推荐。** 去掉全部 capability、禁止提权、只读根文件系统、资源有上限。连接器容器要出网，按「出网」一节约束 |

### 平台自己的文件放在哪

本机后端的受限读策略下，脚本沙盒与连接器沙盒都读得到这些系统目录：

| 平台 | 沙盒里可读的系统目录 |
| --- | --- |
| Linux | `/usr`、`/bin`、`/sbin`、`/lib`、`/lib32`、`/lib64`、`/etc`、`/opt` |
| macOS | `/bin`、`/sbin`、`/usr`、`/System`、`/Library`、`/opt`、`/private/etc`、`/private/var/db`、`/dev`、`/Applications/Xcode.app` |

此外是业务包目录，以及（只对连接器沙盒）`connector_readable_roots`。沙盒里的进程以平台进程的系统用户运行，平台用户读得到的文件它都读得到。所以：

- **`data_dir`、配置文件、带凭据的环境变量文件、本机对象存储目录，不要放在上面这些目录下面。** 特别注意 Linux 上整个 `/etc` 可读：`/etc/xuanpu/` 下平台用户读得到的文件，连接器沙盒里都读得到。本文的例子把配置放在 `/srv/xuanpu/`、数据目录放在 `/var/lib/xuanpu`，都不在沙盒可读的范围里。带凭据的环境变量文件如果一定要放在 `/etc` 下，就设成只有 root 可读，由 systemd 以 root 身份读入（`EnvironmentFile=`）再交给平台进程。
- **`connector_readable_roots` 只列连接器的解释器与依赖**：列进来的目录对所有租户的连接器都可读。
- 平台的密钥（主密钥、数据库连接串、网关凭据、对象存储的访问密钥）不要放在任何挂进沙盒的目录里。

### 出网

连接器沙盒允许出网，「出网」就是平台节点能到的地方连接器都能到：

- 本机后端：包括 `127.0.0.1` 上的 Postgres、模型网关、平台自己的接口，以及节点所在的内网。
- `docker` 后端：连接器容器在 Docker 的默认网络上，能到宿主的网关地址、内网与云厂商的元数据地址（阿里云是 `100.100.100.200`，另有通用的 `169.254.169.254`）。
- HTTP 连接器与远端 MCP 的请求从平台节点发出，目标由租户管理员在清单里写，平台不限（含内网与元数据地址）；回包交给模型，等于一次能读到结果的 SSRF。

平台目前没有按连接器限制出网的配置，要靠部署的网络策略：

- `docker` 后端在宿主的 `DOCKER-USER` 链上丢弃容器发往元数据地址与内网段的包；平台节点自己的出口按同样的原则收紧。
- 数据库不要用 `trust` 认证，模型网关、平台接口都要凭据：本机后端的连接器连得到 `127.0.0.1`。

### 连接器

- **同一业务包内的连接器互信**：它们共用一个连接器沙盒与工作目录。`docker` 与 macOS 下它们能看到彼此的进程环境（含注入的密钥）；bubblewrap 下彼此的进程看不到，但工作目录是共用的。不信任彼此的连接器放进不同的业务包。
- **平台不管连接器自己泄露凭据**：连接器拿到明文后发给谁、写到哪里，平台管不了。平台只保证日志、错误文本里按值脱敏（stdio 连接器**成功**的结果不脱敏，远端 MCP 的脱敏）。
- 连接器的 `env` 不能设置 `LD_*`、`DYLD_*`（发布时校验，组装时再拦一次）。
- 连接器要的运行时要预装：本机后端装在系统目录或列进 `connector_readable_roots`，`docker` 后端装进 `connector_image`。第一次启动受 `limits.connector_startup_seconds` 约束。
- 每个运行时的连接器有自己的 `HOME`（就是它的工作目录）。工作目录在节点本地，不备份、不跨实例共享、不自动清理；`docker` 下里面的文件可能属 root。

### 其他

- **生产环境用 Linux，推荐 `docker` 后端**，并控制镜像内容（脚本镜像与连接器镜像）。
- **根凭据、主密钥、指标抓取令牌、对象存储访问密钥的保管**：经环境变量或密钥管理系统注入，不写进配置文件，不进版本库。
- **TLS 与访问控制在反向代理上做。** 平台默认只监听 `127.0.0.1`。
- **`audit.capture_requests` 默认关着是有原因的**：打开后完整的提示词与对话正文都会进审计表。

已知限制的完整清单见[架构总览](../architecture/README.md#已知限制)。


### 通讯录对账运维

管理员可通过 `POST /v1/identity/feishu/reconcile` 立即对账，正常返回检查、停用、查不到人数；409 表示租约被占，503 表示本次未完成。失败不推进每日周期，下一次维护（约 5 分钟后）重试。飞书报错前已经停用的员工保持停用。查不到通常与通讯录可见范围有关，只设未验证标记；检查应用权限及可见范围，修复后再对账。返回员工但缺状态不会被推断为离职，因此应同时核对员工状态字段权限。

应用令牌只在内存保存，过期前 5 分钟更新，应用或密钥变化后下次对账重新获取；不写日志与审计。对账与登录共享每租户出站并发限制。对账租约 120 秒，在批次和每名员工处理前原子续期，单步最多 30 秒；失租停止。没有整租户运行时限。关闭平台先取消维护，再关闭停用流水线和飞书连接；失败审计与释放租约也有 30 秒时限。存储不可达时释放可能失败，租约到期后其他实例可接手。

| 指标 | 类型 | 标签 | 含义 |
| --- | --- | --- | --- |
| `xuanpu_reconcile_runs_total` | counter | `result` | `succeeded`、`failed`、`cancelled`；只计已认领的运行，不计未到周期/被占跳过 |
| `xuanpu_reconcile_users_total` | counter | `outcome` | `checked`、`disabled`、`unverified`；checked 包含后两项，失败时已处理计数保留，重试可能再次检查 |

指标不带租户标签；租户级排查使用审计的 `identity_reconciled`。缺状态本身暂无独立指标。

升级已有身份数据库时，先执行包含 `0005` 的迁移，并将所有服务实例升级，再保存空事件密钥引用；旧版本不能读取这种配置。
