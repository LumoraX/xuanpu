# 内核对接口缺口清单（活文档）

> 平台与适配包只依赖 taifeng 稳定层（`taifeng.__all__`）。下面是平台实现时核实到的缺口，
> 依据是本轮验证的 taifeng `2026.11.14.1`（`external/taifeng` 子模块，ADR 0018）。每一项都应回 taifeng 按其 ADR 0017 立项；立项前平台按「现行处理」一栏绕开，
> 不 import 私有模块、不 monkeypatch。
>
> 某项在内核落地后，从本表删除并更新对应实现。**编号不重排**：ADR 与代码注释按编号引用这些项，
> 已落地的项直接从表里删掉，编号空着（原文见本文件的 git 历史）。第 1、3–14 项已在 taifeng 2026.10.1.10 落地（内核 ADR 0109–0116），
> 平台在 P0 里换用了稳定层的接口。
> 第 24–29 项（MCP 客户端的握手取消、日志只记元数据、坏响应归类、会话过期恢复、关闭顺序）已在
> taifeng 2026.11.14.1 落地（内核 ADR 0122–0128）。平台对应的兜底（内核 MCP 日志守卫、远端调用处接住一切异常、
> 连接器沙盒强杀起过的进程、`close()` 外接住平台侧失败）保留为纵深防御，不依赖内核单方面保证。

> 升级说明（R5-A1，已完成：平台锁定 taifeng 2026.11.14.1，执行身份进入稳定层，原第 31 项随之删除）：该版本已包含内核 ADR 0117–0129（提交受理与取消、挂起与续跑终态、中断用量、MCP 行长与握手取消、帧解码与日志只记元数据、畸形响应分类、会话过期重连、关闭错误优先级、工作区原子写与一致性检查），对应下表第 18–29 项中的多项。平台的现行处理暂时保留并继续生效；逐项核对后再删除表项与对应绕开代码，不在升级提交里一次性改动。已知可见差异：远端 MCP 的畸形响应现在交给模型的是内核的固定分类文本（如 `[-32000] invalid JSON-RPC error fields`），不再是异常类型名，见 `tests/test_remote_mcp.py`。

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
| 32 | `OtelTelemetrySink` 只接受构造时的资源属性，不支持按事件附加属性（例如宿主自己的 trace 标识） | 平台导出的 OTel span 无法带 `xuanpu.trace_id`，只能按租户分 sink、用 `taifeng.execution_id` 与平台 span 关联 | 平台每租户一个 sink，资源属性带 `xuanpu.tenant`；文档写明关联方式，不包装或改写内核 sink |

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
