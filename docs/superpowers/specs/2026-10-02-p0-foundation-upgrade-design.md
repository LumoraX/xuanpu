# P0 底座升级 —— 详细设计

- 日期：2026-10-02
- 状态：用户说「开始按照这个规划实施」后直接进入实现；本文新增的几处判断（第 3 节标 ★ 的）在实现完成时一并请用户过目
- 上游：[平台整体设计](2026-09-30-system-design.md) 第 8 节、[ADR 0011](../../decisions/0011-system-design-decisions.md) 决策 1、8
- 依据：taifeng `main`（`2026.10.1.10`，ADR 0109–0116）；taifeng-sandbox、taifeng-stores、xuanpu 的 `feat/v1-foundation`
- 读者：实现 P0 的工程师与 agent

## 1. 要做成什么

做完之后：

1. 多个平台实例不再需要共用数据目录。会话主存在 Postgres，业务包归档在对象存储，节点本地盘上只剩缓存（业务包解包目录、沙盒工作区），丢了可以重建。
2. 能停止正在生成的那一轮对话，不必关闭会话。
3. 平台代为运行的 MCP 连接器进程跑在沙盒里，不再跑在平台节点上。
4. 能以远端 MCP server 作连接器。
5. 审批通过后执行的调用，过闸审计里带提交标识；人工拒绝的调用也有过闸记录。
6. 收掉写手册时查出的五处：停机收尾时限、`/openapi.json`、拒绝留痕、配额修改、指标端点。

不做的：按用户隔离执行环境（P1、P4）、开放文件类工具（P4）、PDF 附件（P4）、向内核提的第 15–17 项、内核的审计模式。

## 2. 依赖与分支

- 内核不改。三个仓的依赖下限改为 `taifeng>=2026.10.1.10`；内核发到 PyPI 之前，三个仓都在 `[tool.uv.sources]` 里从同级目录 `../taifeng` 取源码（xuanpu 已经这样取另两个包）。发版是用户点头后的操作，不在 P0 里做。
- 三个仓各开 `feat/p0-base`，从 `feat/v1-foundation` 分出。合入顺序：stores、sandbox 先，xuanpu 后。

## 3. 各仓要做的事

### 3.1 taifeng-stores：`PostgresMessageStore` 能当 `EnginePool` 的主存

| 项 | 做法 |
| --- | --- |
| `ThreadInfo` | 改为从 `taifeng` 顶层引入，删掉单点模块 `_kernel_types`；冒烟测试改为钉住「它在稳定层」 |
| ★ 接管时的回放顺序 | 现在的位置序号取墙钟纳秒，另一个节点接管会话时，两台机器的时钟偏差可能让接管前后的消息错位。作为镜像时可以接受（ADR 0004），作为主存不行。做法：`load_thread` 时记下该线程已有的最大位置；之后对该线程的写入取 `max(墙钟, 已有最大 + 1)`。接管一定先回放再写，所以够用。仍须在第一个 `await` 之前同步取位置 |
| 原子批量写 | **不做**。平台经模型网关只用 Chat 协议；Responses 协议要的 `append_atomic_batch` 等真有需要再加。文档写明「配 Responses 客户端时内核会在建池时报错」 |
| 用法 | 用作主存时：`EnginePool.create(message_store=store, thread_directory=store.directory)`，不给 `storage_dir`；同一命名空间不要再挂 `PostgresMessageMirror`（两者写同一张表） |
| 测试 | 增加经 `EnginePool.create(message_store=...)` 跑一轮、释放引擎、换一个池续接的用例；增加「新池时钟落后也不乱序」的用例 |
| 文档 | 新 ADR 0005（取代 ADR 0003 的单点引入、补 ADR 0004 的接管顺序）；更新 README、架构、接口参考 |

### 3.2 taifeng-sandbox：可持续读写的进程、工作区文件协议

| 项 | 做法 |
| --- | --- |
| 本机后端 | `CommandSpec.stdin=True` 时标准输入接管道；进程对象补上 `stdin`、`stdout`、`stderr` 三个流，满足内核的 `StreamingCommandProcess` |
| 线协议 v2 | `process/start` 增加 `stdin` 参数；新增 `process/write {processId, data}`（守护进程写完并 drain 后才回应，背压随响应回传）与 `process/closeStdin {processId}`。`PROTOCOL_VERSION` 升到 2。守护进程源码随包注入，宿主与守护进程总是同一版本 |
| 宿主端进程 | 流式启动时返回新的流式进程：输出通知到达时喂给 `StreamReader`，进程退出或连接断开时结束流。★ 缓冲超过上限时杀掉进程并让读取方报错，**不丢弃输出**（丢了 MCP 的报文就坏了） |
| 按组杀进程 | 与内核 ADR 0108 对齐：进程退出后、结果收集前仍按进程组杀，杀掉它派生的、还占着管道的子进程 |
| 工作区文件协议 | `DaemonWorkspace` 补 `root`、`resolve`，`metadata` 与 `list_directory` 改为返回内核的 `WorkspaceFileInfo`、`WorkspaceEntry`，越界抛 `WorkspacePathError`，路径中段不是目录时 `exists=False`；守护进程整文件写入改为临时文件加 `os.replace` |
| 文档 | 新 ADR 0004（流式进程与工作区文件协议，更新 ADR 0003 的后果）；`protocol.md` 按 v2 重写相关方法；接口参考、README、架构 |

### 3.3 xuanpu：对象存储与业务包归档

- **对象存储协议**放在 `xuanpu_server/objectstore/`（ADR 0011 决策 8）：`put(key, data, *, content_type)`、`get(key)`、`exists(key)`、`delete(key)`。两个实现：
  - `LocalObjectStore(root)`：落本地目录，用于试用与测试；
  - `S3ObjectStore`：S3 兼容接口，首选阿里云 OSS（ADR 0009）。★ 用 `boto3`，阻塞调用放进线程；可选依赖 `xuanpu-server[s3]`。OSS 要求虚拟主机风格寻址，配置里给 `addressing_style`，默认 `virtual`。测试用 `moto` 起一个本机 S3 服务。
- 预签名直传等 P3 再加，P0 只要上面四个方法。
- **配置**新增 `object_store` 段：`backend: local | s3`；`local.root`（默认 `<data_dir>/objects`）；`s3.endpoint`、`bucket`、`region`、`prefix`、`access_key_id`、`secret_access_key`（经 `${VAR}` 从环境展开，与主密钥同一种做法）、`addressing_style`。
- **业务包发布**：校验通过后，归档写进对象存储，键为 `tenants/<租户>/packages/<名>/<版本>/<sha256 十六进制>.zip`——带上指纹，两个实例并发发布同一版本、内容不同时不会互相覆盖（实现时审查发现后改的）；登记记录的 `location` 改存这个键（第一版未发布，没有旧数据要迁）。登记失败时，只有登记记录不指向本次的键才删掉本次写入的对象。
- **用到时解到本地缓存**：`<data_dir>/cache/packages/<租户目录>/<名>/<版本>/`。缓存里有就直接用；没有就取归档、核对 `digest`、解到暂存目录再改名，同一个包并发只解一次。`ResolvedPackage.root` 指向缓存目录。缓存不做淘汰（版本不可变，体积有限），文档写明可以随时清空。

### 3.4 xuanpu：会话主存进 Postgres

- `state.backend: postgres` 时，每个运行时的引擎池用一个新的 `PostgresMessageStore`（租户专属前缀），`thread_directory` 用它自带的目录，不再给 `storage_dir`、不再挂消息镜像。
- `state.backend: local` 保持现状（试用与单机）。
- `StateBackends` 增加 `message_store: Callable[[], MessageStore] | None`。
- 效果：别的实例接手会话时从库里续接，`data_dir/threads` 不再使用。

### 3.5 xuanpu：停止生成

- `SessionManager.cancel(principal, session_id, submission_id=None)`：不给 `submission_id` 就停掉这个会话在本实例上全部在途的对话。逐个提交 `taifeng.CancelTurn`。被停的那一轮以 `turn_completed` 结束，`data.end_reason` 是 `cancelled`，事件流照常收尾。
- 会话不在本实例上：租约在别的实例手里时报 `session_busy`（带 `owner`，与现在一致，由反向代理的粘性路由避免）；谁都不持有就说明没有在途对话，返回空列表。
- 只管在途的对话。挂起等审批的那一轮不在这里处理，拒绝仍走 `resume`。
- 审计记一条 `turn_cancel_requested`（控制面，带被停的提交标识）。
- 接口 `POST /v1/sessions/{id}/cancel`，请求体 `{"submission_id": 可选}`，响应 `{"cancelled": [提交标识…]}`。Python 客户端加 `sessions.cancel`；控制台对话框在流式输出时显示「停止」。

### 3.6 xuanpu：连接器进沙盒、远端 MCP

- **stdio 连接器**经 `McpStdioClient.spawn(..., executor=...)` 在沙盒里起。★ 每个运行时为连接器单独开一个沙盒，与脚本的沙盒分开：
  - 只读：业务包目录；可写：这个运行时的连接器工作目录 `<data_dir>/connectors/<租户目录>/<名>/<版本>/`；
  - **允许出网**（连接器就是去连外部系统的；脚本是否出网仍按清单）；
  - 环境：现有的最小环境、清单声明的变量与密钥，加上 `HOME`、`TMPDIR` 指向连接器工作目录；
  - 本机后端用受限读策略，额外可读目录由配置 `sandbox.connector_readable_roots` 给出（连接器自己的解释器、运行时所在目录）；容器后端用 `sandbox.connector_image`（默认同 `sandbox.image`），镜像里要装好连接器需要的运行时。
  - 清单里没有 stdio 连接器就不开这个沙盒。
- **远端 MCP**：清单的连接器新增传输 `transport: remote`，字段 `url`（必填，`https://`，试用可 `http://`）、`auth`（复用 HTTP 连接器的 `bearer` / `header` 写法，只写密钥名）、`timeoutSeconds`。工具名前缀仍是 `mcp__`。规范仍是 `xuanpu/v1`（新增可选取值）。运行时用 `McpHttpClient`；★ 凭据经自定义的 httpx 传输在每次请求发出时从保管库取出加到请求头，客户端对象里不长期持有明文。

### 3.7 xuanpu：过闸审计补齐

- 审批后重跑工具时内核已带 `submission_id`（触发重跑的那次 `Resume` 的标识），闸门现有代码直接用上。加测试钉住。
- `resume` 里被拒绝的请求，按挂起时记下的请求内容补一条过闸记录 `connector_call`，`decision` 为 `rejected`，`detail` 为 `rejected_by_user`；只对连接器工具的请求补。

### 3.8 xuanpu：五处收尾

| 项 | 做法 |
| --- | --- |
| 停机收尾时限 | `serve` 给 uvicorn 设 `timeout_graceful_shutdown`，取 `shutdown_drain_seconds` 再加 5 秒 |
| `/openapi.json` | 关掉（`openapi_url=None`） |
| 拒绝留痕 | 见 3.7 |
| 修改配额 | `PUT /v1/admin/tenants/{id}/quota`，平台管理员；审计 `tenant_quota_changed`；Python 客户端 `admin.set_quota` |
| 指标 | ★ `GET /metrics`，Prometheus 文本格式，用 `prometheus_client`，每个平台一个独立的登记表。凭据是单独的抓取令牌：配置 `metrics.token_hash`（摘要，与根凭据同一种做法），不配就不开（404）。指标不带租户标签（防止基数膨胀；按租户看用量走 `/v1/usage`） |

指标第一批：

| 名字 | 类型 | 标签 |
| --- | --- | --- |
| `xuanpu_sessions_live` | gauge | — |
| `xuanpu_turns_in_flight` | gauge | — |
| `xuanpu_turns_total` | counter | `outcome`：completed / failed / suspended / cancelled |
| `xuanpu_runtimes_live` | gauge | — |
| `xuanpu_connector_calls_total` | counter | `transport`、`decision` |
| `xuanpu_audit_write_failures_total` | counter | — |

## 4. 对照七条规则

| 规则 | P0 怎么满足 |
| --- | --- |
| 1 副作用过闸、留审计 | 远端 MCP 走同一个闸门；人工拒绝补记过闸记录；停止生成记控制面审计 |
| 2 凭据只以名字出现 | 远端 MCP 的凭据在请求发出时现取；对象存储的访问密钥是部署配置，与主密钥同等对待，不进日志 |
| 3 每条数据有主人 | 对象键以租户开头；缓存与连接器工作目录按租户分目录；会话主存按租户前缀分表 |
| 4 不加载业务方代码 | 连接器进程从平台节点挪进沙盒，更彻底 |
| 5 只依赖内核稳定层 | 用到的 `CancelTurn`、`McpHttpClient`、`message_store`、`StreamingCommandProcess`、`WorkspaceFS`、`ThreadInfo` 都在 `taifeng.__all__` |
| 6 外部内容收紧审批 | 不涉及（P6） |
| 7 员工内容管理员默认看不到 | 不涉及（P1、P6） |

## 5. 测试

- 三个仓各自的测试、ruff 门禁、mypy strict 全绿（AGENTS.md 完成定义）。
- 需要 Postgres 与 Docker 的用例照现在的做法：有环境变量用现成服务，没有就用 Docker 临时拉起，都没有就跳过。
- xuanpu 增加一个两实例用例：实例 A 跑一轮后放手，实例 B 用**另一个数据目录**接手同一会话并续上一轮（共享 Postgres 与对象存储）。这是 M0 的验收。

## 6. 文档

- xuanpu：架构总览（状态面、已知限制）、HTTP 接口（取消、配额、指标）、业务包规范（`remote` 传输）、运维手册（多实例不再要共享卷、对象存储配置、指标、配额）、编写业务包（连接器沙盒、远端 MCP）、对接手册（停止生成）、审计与用量（拒绝记录）、样例配置、内核缺口清单（落地的项删掉，编号不重排）。
- 新 ADR 0013：连接器进程进沙盒、远端 MCP 传输。
- 两个适配包各一份新 ADR（见 3.1、3.2）。
