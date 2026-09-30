# 内核对接口缺口清单（活文档）

> 平台与适配包只依赖 taifeng 稳定层（`taifeng.__all__`）。下面是第一版实现时核实到的缺口，
> 依据是 taifeng `2026.9.30.1`。每一项都应回 taifeng 按其 ADR 0017 立项；立项前平台按「现行处理」一栏绕开，
> 不 import 私有模块、不 monkeypatch。某项在内核落地后，从本表删除并更新对应实现。

| # | 缺口 | 影响 | 现行处理 |
| --- | --- | --- | --- |
| 1 | `EnginePool.create` 不接受外部 `MessageStore`，固定用本机 JSONL | 多实例无法共享会话主存 | 主存仍是节点本地 JSONL；线程目录走 `PostgresThreadDirectory`，消息经 `IndexHook` 镜像到 Postgres 供查询与审计；会话租约保证单写者 |
| 2 | 会话日志、写者锁、原子批量写协议未公开（`JsonlSessionJournalCore` 在实验层，`WriterLockAdapter`、`AtomicBatchMessageStore` 未导出） | 无法实现共享日志后端；外部 `MessageStore` 不能配 Responses 协议的模型客户端 | 见 taifeng-stores ADR 0002；`PostgresMessageStore` 只声明支持 Chat 协议 |
| 3 | `ThreadInfo` 出现在稳定协议 `MessageStore.list_threads` 的签名里，但未从顶层导出 | 外部包无法在只用稳定层的前提下完整实现 `MessageStore` | taifeng-stores ADR 0003：签名类型经单点模块引入，冒烟测试钉住 |
| 4 | `make_shell_exec_tool`、`make_file_read_tool`、`make_file_write_tool`、`make_http_request_tool` 等内置工具工厂不在稳定层 | 平台无法在稳定层内给模型提供前台 shell 与文件工具 | 第一版只开放脚本（`run_script`）、后台命令（`run_in_background` + `wait_for_task`，均在稳定层）与 MCP 连接器 |
| 5 | `WorkspaceFS` 协议不存在，文件类工具直接读写本机目录 | 命令在沙盒里跑、文件却在宿主机上读写 | 容器后端靠同路径挂载保证视图一致；`taifeng-sandbox` 守护进程已提供文件访问方法，等协议落地后接入 |
| 6 | `register_mcp_tools`、`bind_mcp_tools` 不在稳定层（后者在实验层） | 平台要自己把 MCP 工具包成 `ToolSpec` | `xuanpu-server` 连接器桥只用 `McpStdioClient`、`ToolSpec`、`ToolResult` 自行包装 |
| 7 | `McpStdioClient.spawn` 直接起本机子进程，不经 `CommandExecutor` | 由平台代为运行的 MCP server 进不了沙盒 | 第一版连接器进程跑在平台节点上，只注入声明过的密钥与最小环境；需要隔离的连接器由业务方自行部署为远端服务 |
| 8 | 模拟模型客户端（`SimClient`）不在稳定层 | 下游包无法用内核自带的模拟器写测试 | 平台自带一个只依赖稳定层类型的脚本化模型客户端，仅用于测试与本地演示 |
| 9 | `TelemetrySink` 协议未导出 | 适配包无法按契约发 `directory_cursor_reset` 等事件 | 适配包改为写日志；平台侧审计走 `engine.subscribe_all_envelopes()` |
| 10 | `CancelTurn` 不在稳定层（稳定层的操作只有 `UserMessage`、`Resume`、`SendToPeer`、`UpdateInstructions`） | 平台无法只取消进行中的那一轮对话 | 第一版只提供关闭会话（`EnginePool.release(force=True)`），关闭即取消 |
| 11 | 审批通过后重跑工具时，`ToolContext.extras` 里没有 `submission_id`（正常派发路径有） | 工具无法把这次执行归到具体的一轮对话 | 平台的过闸审计按线程标识归属到会话 |
| 12 | `McpHttpClient` 在实验层，不在稳定层 | 平台无法以 HTTP 传输接远端 MCP server 或 MCP 网关（ADR 0006） | MCP 连接器第一版只支持 stdio；以 HTTP 传输接 MCP 等它晋升稳定层。按 OpenAPI 直接调用的 HTTP 连接器（ADR 0008）不经 MCP，不受此项影响 |
| 13 | `ScriptInvocation` 只有 `descriptor`、`args`、`cancel`，不带线程或会话标识 | `ScriptExecutor` 不知道这次脚本属于哪个会话，做不出按会话隔离的沙盒工作区 | 同一运行时（租户 + 业务包版本）下的会话共用一个工作区 |
