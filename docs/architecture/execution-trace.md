# 执行追踪（活文档）

> R5-B。为什么这么定见 [ADR 0017](../decisions/0017-butler-artisan-orchestration.md) 决策 5；设计与计划见 [R5-B 设计](../superpowers/specs/2026-11-14-r5b-trace-span-design.md)、[R5-B 计划](../superpowers/plans/2026-11-14-r5b-trace-span.md)。

员工的一次请求引出的全部执行记成一条 **trace**，其中每个执行单元是一个 **span**。只存元数据：身份、父子、状态、起止时间、逐尝试用量，以及工具 span 上匠人动手前说的一句旁白（R7，见下文）；不存其余正文、工具参数与输出、推理内容或凭据。正文仍从会话历史按原权限读取。

## 采集

- 内核在每条事件上带稳定层的执行身份（taifeng `ExecutionIdentity`，内核 ADR 0143）：`execution_id`、`attempt_id`、父执行。平台只读这些身份与有限标量字段。
- 每个引擎的事件枢纽（`runtime/events.py`）在同一条全量订阅上把事件同步交给采集器（`tracing/collector.py`）；采集器在后台每 0.5 秒批量写入。登记不让出、不抛异常；写入失败只记日志，trace 标为不完整，对话不受影响。
- 只在 `execution_trace` 开启且列入名单的租户、且会话有主人（员工）时采集；API key 自己的会话不采集。

| 内核事件 | span 事实 |
| --- | --- |
| `turn_started` | 执行 span `ex.<execution_id>`：根执行类型 `artisan`（管家会话将为 `butler`），子执行挂到父执行下；状态 running |
| `execution_updated` | 阶段（model/running → running，failed，cancelled）与本尝试自身用量 |
| `tool_call_started` / `tool_call_completed` | 工具 span `tl.<call_id>` 挂在发起者下；`call_skill`、`await_skills` 等等待子执行的工具让发起者显示 waiting_children |
| `turn_suspended` | waiting_input（续跑是新尝试，回到 running） |
| `turn_completed` / `turn_failed` | completed、failed 或 cancelled（结束原因是代码） |

trace 边界：根执行开始一轮时打开；同一根执行挂起后续跑仍在同一条；根执行完成、失败或取消时结束，同一尝试的迟到事件丢弃，下一轮另开。没见过开头或父执行的执行（晚订阅、冷恢复）单独成 trace 并标不完整。引擎在执行中途被关闭时，未结束的 trace 标不完整。跨引擎的父子见下节。

## 旁白（R7，`tracing/narration.py`，[ADR 0023](../decisions/0023-execution-story.md)）

- 采集器为每个执行攒下它对员工说出口的话（`assistant_text` 增量，上限 2000 字，超出丢开头）；同一执行的 `tool_call_started` 到来时取走、清洗成一句挂到该工具 span 的 `say`（并行的一批只挂第一个），`turn_completed`/`turn_failed`/`turn_suspended` 时清空。
- 清洗：去成对 `<think>` 块与控制字符、折叠空白；不超过 160 字就整段保留，超过时从结尾往前按整句保留（最后一句本身过长则从开头截并加「…」）；为空则没有旁白。真实模型常把「做什么」与「不做什么」拆成两句，只留最后一句会丢掉要紧的那句（R7 真实模型验收发现）。
- `say` 首值为准：重复或迟到的不同旁白不覆盖，也不算结构冲突。推理、工具参数与输出不读。

## 跨引擎关联（`tracing/binding.py`、`butler/trace_link.py`）

- 一条 trace 可以有多个写入方：每个引擎采集器里的每条本地 trace 是一个写入方（名为本地标识），派工记录是一个写入方（`dp.<dispatch_id>`）。`TraceRecord.sources` 记各写入方已应用的最大序号。
- 派工 span `dp.<dispatch_id>`（kind `dispatch`）挂在管家根 span 下，由 `TracedDispatchStore` 在派工每次建与改之后写入，序号就是派工记录的版本；平台上所有派工读写都经这一层（`Platform.dispatches`），版本连续，某次写入失败则下一次跳号、trace 如实标不完整。
- 匠人引擎的采集器在第一次写出从头开始的根 trace 前查一次持久绑定（`RuntimeDefaults.trace_binder`，实现为 `DispatchTraceBinder`）：会话承载按派工记录的 `host_ref`（派工器先建会话、写入 `host_ref`，再发出任务说明，引擎这时才创建）；任务承载按任务的 `request_id`（`butler:<dispatch_id>`）。查到则写进管家的 trace，根执行 span 的父级是派工 span。
- 查不到绑定的会话（员工直接开的业务会话、旧工作）照旧单独成 trace，根 span 无父级，不按时间或内容猜。绑定查询出错时这条 trace 标不完整。

## 规则（`tracing/rules.py`、`tracing/apply.py`）

- 事实按写入方各自的序号应用：同一写入方已应用的序号跳过（重复投递不重复记），某写入方跳号标不完整，不同写入方互不影响；写入方数量上限 256，超出的批次丢弃并标不完整。被拒事实、结构冲突（类型、父级、引用与已知不同）都只把 trace 或 span 标为不完整，不覆盖已知值。
- 同一尝试到达终态后，它的迟到状态事实不复活 span；新尝试可以重新运行。取消请求本身不是状态，见到真实终态才是 cancelled。
- 用量按尝试保存绝对值：同尝试的新快照覆盖，不同尝试相加；尚无用量或任一尝试未知时为未知（null），不用 0 冒充。
- 单条 trace 的 span 数与单个 span 的尝试数有上限，超出停止新增并标截断或不完整。

## 存储（`tracing/memory.py`、`tracing/postgres.py`）

与控制面同后端（`ControlPlaneStore.trace_store`）。PostgreSQL 表 `trace`、`trace_span`（迁移 0021），主键带租户，span 随 trace 级联删除；写入锁住 trace 行，与内存实现共用 `apply_batch`，契约测试对两个实现各跑一遍。定期清理（`tracing/retention.py`）每小时按租户分批删除超过保留天数的 trace，不动审计。

## 读取

`GET /v1/traces`、`/v1/traces/{id}`、`/v1/traces/{id}/waterfall`、`/v1/traces/{id}/events`（见 [HTTP 接口](http-api.md#执行追踪r5-b)），只给本人：需要当前启用的员工身份，他人的与不存在的一律 404，机器身份 403。瀑布由持久 span 计算：偏移、时长、深度；运行中的行没有时长；缺父或成环的 span 记为深度 0 的孤儿，不递归挂死。

增量流（`api/trace_events.py`）每秒重读持久记录：首帧 `snapshot`，之后 `spans` 只带有变化的 span，trace 不再活跃时发 `end` 并关闭。事件 `id` 是整条 trace 已应用事实的总数（`last_seq`，单调递增）；`Last-Event-ID` 与当前序号相同时不重发快照，落后时重发一次完整快照（存储不按 span 记序号，快照是唯一可靠的补齐方式）。每轮按原请求凭据重新认证并重查员工状态，登出、停用或换人即结束流。

## OTel 导出（`tracing/otel.py`）

可选，`telemetry.otel.enabled` 默认 false，关闭时不构造任何 sink、不建连接。开启时平台持有一个 `OtelExport`（随 `RuntimeDefaults.otel_export` 注入），对 `execution_trace` 名单内的租户各懒建一个内核 `OtelTelemetrySink`，资源属性带 `xuanpu.tenant`；事件枢纽在采集 span 的同一条订阅上把事件交给该 sink。端点只由平台管理员配置，不读 `OTEL_*` 环境变量。正文过滤沿用内核：参数、输出、增量、摘要与完整提示词（`llm_request_recorded`）不导出。导出失败由内核 sink 吞下并记告警。平台关闭时统一 flush 并关闭。

OTel span 不带 `xuanpu.trace_id`：内核 sink 只接受构造时的资源属性（[缺口清单](kernel-seam-backlog.md)第 32 项）。按 `taifeng.execution_id` 与平台 span `ex.<execution_id>` 关联。

## 已知限制

- 冷恢复不重建内核的父子关系：进程重启前后的同一请求会分成两条 trace，前一条若未结束不会自动标不完整（采集器随进程消失）。
- 增量流是轮询持久记录（每秒一次），不是事件推送；延迟约为采集批量间隔加轮询间隔。
- OTel span 缺 `xuanpu.trace_id`，见上。
- 管理员没有跨员工的 trace 视图；按 span 元数据的管理查询走既有审计入口。
