# 内核对接口缺口清单（活文档）

> 平台与适配包只依赖 taifeng 稳定层（`taifeng.__all__`）。下面是平台实现时核实到的缺口，
> 依据是本轮验证的 taifeng `2026.10.1.11`。每一项都应回 taifeng 按其 ADR 0017 立项；立项前平台按「现行处理」一栏绕开，
> 不 import 私有模块、不 monkeypatch。
>
> 某项在内核落地后，从本表删除并更新对应实现。**编号不重排**：ADR 与代码注释按编号引用这些项，
> 已落地的项直接从表里删掉，编号空着（原文见本文件的 git 历史）。第 1、3–14 项已在 taifeng 2026.10.1.10 落地（内核 ADR 0109–0116），
> 平台在 P0 里换用了稳定层的接口。

| # | 缺口 | 影响 | 现行处理 |
| --- | --- | --- | --- |
| 2 | 会话日志后端（`SessionJournalCore`、`JsonlSessionJournalCore`、`WriterLockAdapter` 等，内核 ADR 0114）整体在实验层 | 无法实现共享的会话日志后端；内核的审计模式用不上 | 平台不用（ADR 0011 决策 2）：会话主存走稳定层的 `MessageStore`（taifeng-stores 的 `PostgresMessageStore`），审计走平台自己的审计表 |
| 15 | `CommandExecutor.start(CommandSpec)` 与 `WorkspaceFS` 的调用不带归属标识（会话、提交），只有 `ScriptInvocation` 带（内核 ADR 0109） | 命令执行与文件类工具无法按会话路由到各自的沙盒，执行环境只能按用户拆引擎池（ADR 0011 决策 7） | 引擎池按（租户，业务包，版本，用户）划分 |
| 16 | `EnginePool.create` 只接受一个 `skills_dir`，不接受多目录与来源分层（`FilesystemSkillRegistry.load` 本身支持） | 个人 skill 不能作为单独一层来源加载（ADR 0011 决策 9） | 组装时把应用的 skill 与个人 skill 合成一个目录 |
| 17 | 白名单之外的 skill 授权策略（内核 ADR 0089）与来源信任分层（内核 ADR 0090）在实验层 | 个人 skill 只能靠改本地副本里的子 skill 列表进入会话，不能由策略裁决 | 同上 |
| 18 | `McpStdioClient.spawn` 不给执行器、直接起本机子进程时，读输出的 `StreamReader` 用 asyncio 默认的 64 KiB 行长上限（`mcp/stdio_client.py` 里 `create_subprocess_exec` 没传 `limit`） | 单条 MCP 响应超过 64 KiB 时 `readline` 抛 `ValueError`，读循环崩掉，连接器不可用 | 平台不受影响：平台的 stdio 连接器都经 taifeng-sandbox 的执行器起，守护进程路径单条消息上限 16 MiB，本机后端也已把读取上限放到 16 MiB（taifeng-sandbox `d3ec583`）。只影响不经执行器直接起的用法 |
| 19 | 内核自带的 `LocalWorkspaceFS` 有几处与 `WorkspaceFS` 契约不符：`write_bytes(".")` 在工作区之外建 `<root>.tmp`，失败不清理；临时文件名固定为 `<目标>.tmp`，并发写同一文件互相覆盖；`remove` 对符号链接按解析后的路径操作，删掉的是链接目标而不是链接本身；契约「规范路径以 `root + "/"` 开头」在 `root` 为 `/` 时不成立。`taifeng.testing` 也没有 `WorkspaceFS` 的一致性检查函数 | 文件类工具在本机工作区与经守护进程的工作区上行为不一致；外部实现只能对照内核测试自己写检查 | 平台还没开放文件类工具（P4）。taifeng-sandbox 的 `DaemonWorkspace` 已按契约实现（原子写用不重名的临时文件、删链接本身） |
| 20 | 尚未入 pending 的取消可静默无效 | 刚提交就停止不能直接判定已停止 | 平台等待受理，到期返回 unconfirmed；prepare 阶段另有停止意图；preserve_suspension=True 消除转挂起清除待办的竞态 |
| 22 | 被 `CancelTurn` 打断的那次采样没有用量，也没有估算（`turn_completed` 的 `usage` 为 0） | 网关照样按 prompt 收费；计 0 的话，长上下文加「收到第一个增量就停」可以绕过当天的 token 配额 | 平台按 `introspect()["context_tokens"]` 估算计入 `input_tokens`，审计记一条 `usage_estimated`，指标 `xuanpu_usage_estimated_total` 计数 |
| 23 | 纯过期哨兵与未核销集合求交为空时，内核静默让位 | 内核定时裁决没有独立结果事件 | 平台不把内部哨兵当用户请求；根系统 abort 已由 2026.10.1.11 的 suspension_resolved.aborted=True 明确结束 |
| 24 | `McpStdioClient.spawn` 握手阶段只捕获 `Exception`：被取消（外层 `wait_for` 的启动时限到了）时客户端不关闭，已经起来的进程泄漏 | 每次启动超时漏一个连接器进程 | 平台的本机连接器沙盒记下经执行器起过的全部进程，关运行时时逐个强杀；容器后端随容器销毁 |
| 25 | stdio 客户端把连接器 stdout 上的非 JSON 行原样、不限长度写进日志（`logger.warning("mcp: invalid JSON: %r", line)`） | 连接器可控的内容（可能带着注入给它的凭据）原样进平台日志 | 平台给内核 MCP 的 logger 挂过滤器：字符串与字节串只记长度，附带的异常只记类型（`runtime/connector_process.py`）。代价是这些日志里看不到内容 |
| 26 | HTTP 客户端（`McpHttpClient`）把解析不了的 SSE 数据截前 200 字符按 `%r` 写日志（`_decode_message`），把远端给的不认识的响应 id 原样写日志（`unexpected response id=%r`） | 远端可控的内容原样进日志；内核日志记录里不知道是哪个连接器、该用哪些密钥脱敏 | 同第 25 项的过滤器。建议内核只记长度，或给宿主一个脱敏钩子 |
| 27 | `McpHttpClient._unwrap` 对 `error.code` 直接做 `int()`（字符串抛 `ValueError`，`null` 抛 `TypeError`）；`_read_response` 不捕获 `UnicodeDecodeError` | 远端回一个形状不对的响应，冒出的是 `McpToolError` 之外的异常，越过调用方按 `McpToolError` 写的错误处理 | 平台在远端 MCP 的调用处接住一切异常：交给模型与审计的只有异常类型，日志里记按值脱敏后的文本 |
| 28 | `McpHttpClient` 收到 404（远端的 MCP 会话已过期）不重新握手 | 远端重启或回收会话之后，这个运行时里对它的调用一直失败，直到运行时被回收重新组装 | 未绕开：调用失败照常记过闸记录并交给模型；运行时空闲回收（`runtime_idle_seconds`）后重新组装才恢复 |
| 29 | `McpHttpClient.close()` 发 `DELETE` 结束会话时只接 `httpx.HTTPError`；抛别的异常（例如平台的凭据传输取不到密钥）时跳过关闭自己的 httpx 客户端 | 连接池泄漏 | 平台在 `close()` 外接住平台侧的失败并记告警，无论如何都关掉自己建的传输（连接池在传输里） |

## 另外的约定说明

不算缺口，但平台依赖这些行为，内核改动时要知会：

- 送回审批被拒（`suspension_resolve_rejected`）与只核销一部分（`suspension_partially_resolved`）之后，内核不再为这次送回发任何事件。平台把这两种事件当作这次送回的终结事件。
- `CancelTurn` 自己那个提交上，内核另发一条 `engine_log`（`cancelled turn <目标提交>`）。平台不为它登记事件流，只进审计。
- 取消紧跟在送回批准之后时，已批准的工具仍会执行：它在续跑那一轮开始之前执行，取消只停之后的采样。

## R0 新确认的结果核实边界

已落地第 21、30 项由内核 ADR 0117 处理。普通人工决定重发已核销项时整次拒绝；
纯过期哨兵仅处理剩余项。根系统中止以 aborted=True 明确结论，平台不记成功或额外采样用量。

批准后直接执行工具时再次出现 SuspendSignal，当前内核以 turn_failed(kind=SuspendSignal)
结束该 Resume，没有创建新的结果未知待办。玄圃保留 outcome_unknown 回执并拒绝再次批准。
R0 不开放该路径的自动重发或自动补结果；须人工核实外部系统。R1 也不得自动恢复这一类写操作。
relaxed 直接写超时则会正常生成 tool_outcome_unknown 待办，暂停模型以防自行重试。
