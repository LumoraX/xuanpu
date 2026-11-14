# 执行追踪（活文档）

> R5-B。为什么这么定见 [ADR 0017](../decisions/0017-butler-artisan-orchestration.md) 决策 5；设计与计划见 [R5-B 设计](../superpowers/specs/2026-11-14-r5b-trace-span-design.md)、[R5-B 计划](../superpowers/plans/2026-11-14-r5b-trace-span.md)。

员工的一次请求引出的全部执行记成一条 **trace**，其中每个执行单元是一个 **span**。只存元数据：身份、父子、状态、起止时间、逐尝试用量；不存正文、工具参数与输出、推理内容或凭据。正文仍从会话历史按原权限读取。

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

trace 边界：根执行开始一轮时打开；同一根执行挂起后续跑仍在同一条；根执行完成、失败或取消时结束，同一尝试的迟到事件丢弃，下一轮另开。没见过开头或父执行的执行（晚订阅、冷恢复）单独成 trace 并标不完整。引擎在执行中途被关闭时，未结束的 trace 标不完整。跨引擎的父子（管家派工给匠人）由 R5-C 的派工记录传递，尚未实现。

## 规则（`tracing/rules.py`、`tracing/apply.py`）

- 事实按 trace 内序号应用：已应用的序号跳过；跳号、被拒事实、结构冲突（类型、父级、引用与已知不同）都只把 trace 或 span 标为不完整，不覆盖已知值。
- 同一尝试到达终态后，它的迟到状态事实不复活 span；新尝试可以重新运行。取消请求本身不是状态，见到真实终态才是 cancelled。
- 用量按尝试保存绝对值：同尝试的新快照覆盖，不同尝试相加；尚无用量或任一尝试未知时为未知（null），不用 0 冒充。
- 单条 trace 的 span 数与单个 span 的尝试数有上限，超出停止新增并标截断或不完整。

## 存储（`tracing/memory.py`、`tracing/postgres.py`）

与控制面同后端（`ControlPlaneStore.trace_store`）。PostgreSQL 表 `trace`、`trace_span`（迁移 0021），主键带租户，span 随 trace 级联删除；写入锁住 trace 行，与内存实现共用 `apply_batch`，契约测试对两个实现各跑一遍。定期清理（`tracing/retention.py`）每小时按租户分批删除超过保留天数的 trace，不动审计。

## 读取

`GET /v1/traces`、`/v1/traces/{id}`、`/v1/traces/{id}/waterfall`、`/v1/traces/{id}/events`（见 [HTTP 接口](http-api.md#执行追踪r5-b)），只给本人：需要当前启用的员工身份，他人的与不存在的一律 404，机器身份 403。瀑布由持久 span 计算：偏移、时长、深度；运行中的行没有时长；缺父或成环的 span 记为深度 0 的孤儿，不递归挂死。

增量流（`api/trace_events.py`）每秒重读持久记录：首帧 `snapshot`，之后 `spans` 只带有变化的 span，trace 不再活跃时发 `end` 并关闭。事件 `id` 是已应用的最大事实序号；`Last-Event-ID` 与当前序号相同时不重发快照，落后时重发一次完整快照（存储不按 span 记序号，快照是唯一可靠的补齐方式）。每轮按原请求凭据重新认证并重查员工状态，登出、停用或换人即结束流。

## OTel 导出（`tracing/otel.py`）

可选，`telemetry.otel.enabled` 默认 false，关闭时不构造任何 sink、不建连接。开启时平台持有一个 `OtelExport`（随 `RuntimeDefaults.otel_export` 注入），对 `execution_trace` 名单内的租户各懒建一个内核 `OtelTelemetrySink`，资源属性带 `xuanpu.tenant`；事件枢纽在采集 span 的同一条订阅上把事件交给该 sink。端点只由平台管理员配置，不读 `OTEL_*` 环境变量。正文过滤沿用内核：参数、输出、增量、摘要与完整提示词（`llm_request_recorded`）不导出。导出失败由内核 sink 吞下并记告警。平台关闭时统一 flush 并关闭。

OTel span 不带 `xuanpu.trace_id`：内核 sink 只接受构造时的资源属性（[缺口清单](kernel-seam-backlog.md)第 32 项）。按 `taifeng.execution_id` 与平台 span `ex.<execution_id>` 关联。

## 已知限制

- 冷恢复不重建内核的父子关系：进程重启前后的同一请求会分成两条 trace，前一条若未结束不会自动标不完整（采集器随进程消失）。
- 增量流是轮询持久记录（每秒一次），不是事件推送；延迟约为采集批量间隔加轮询间隔。
- OTel span 缺 `xuanpu.trace_id`，见上。
- 管理员没有跨员工的 trace 视图；按 span 元数据的管理查询走既有审计入口。
