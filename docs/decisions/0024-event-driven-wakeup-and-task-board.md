# ADR 0024：事件驱动唤醒、跨轮任务板与同会话顺序执行

- 状态：Proposed（2026-11-27，待实施）
- 关联：修订 [ADR 0020](0020-butler-always-online.md) 决策 1 的同会话并发语义（由无协调裸并发收口为「默认 FIFO 顺序排队 + 分阶段安全强制插入合并」），补充 [ADR 0017](0017-butler-artisan-orchestration.md) 唤醒与跨轮状态机制；不推翻 0017、0020 其余决策
- 规划：[SOUL 与演进主规划](../superpowers/plans/2026-11-26-butler-soul-and-evolution-plan.md) · [运行时、造匠与可观测底座规划](../superpowers/plans/2026-11-27-butler-runtime-forge-and-observability-plan.md)

## 背景

当前管家运行时存在三处体验与效能瓶颈：

1. **唤醒纯轮询**：`butler/wakeup.py` 按 `wake_interval_seconds` 定时全租户扫活跃派工，单跳响应延迟等于轮询周期加审计落盘时间；进入多步接力（如 `web-research` → `doc-analyst`）后延迟会被逐跳放大，且派工增长会带来大量无效读库。
2. **每轮新建引擎导致跨轮失忆**：管家无长驻线程，内核内存 `todo_write` 不跨轮，每轮上下文仅靠主对话最近 6 条记录，员工并发交代多件跨小时/跨天事项时容易丢上下文。
3. **同员工连发无协调并发**：ADR 0020 保持输入框永不禁用，但同一员工连发碎片消息若裸并发执行，容易造成重复建人、漏关联或脏写冲突。

## 决策

1. **事件驱动快路径 + 低频对账兜底（Dual-Path Wakeup）。**
   - 权威状态提交时发射唤醒信号：PostgreSQL 后端在状态/审计提交的**同一事务内**执行 `SELECT pg_notify('butler_wake', payload)`（仅 `COMMIT` 后投递，消除未提交脏读）；内存后端推入 `asyncio.Queue`。
   - Payload 严格只含元数据 `{"t": "<tenant>", "k": "dispatch|group|pending|proactive", "id": "<id>"}`，不含任何对话或文件正文。
   - 消费者按 `(tenant_id, kind, id)` 内存合并去重后执行定点对账（`run_for_dispatch`）与定点编组推进（`advance_group`）；原有全租户 `run_once`（含 `resume_stale`）保留为低频 Reconciler（默认 20–30 秒及进程启动立即执行一次），兜底进程崩溃、网络闪断或漏通知，幂等与 SIGKILL 恢复契约不变。
2. **条件超时器（Conditional Timer）。**
   - 派工、多步接力或待裁决项可持久化 `deadline_at`；完成事件到达即失效，到期未终态由事件循环触发超时提醒或带方案报忧，不使用 `sleep` 占住协程。
3. **跨轮工作记忆与持久任务板（Working Context + `ButlerTaskBoard`）。**
   - 按 `(tenant_id, user_id)` 持久化轻量任务板（含 `board_id`、`trace_id`、`title`、`focus`、`status`、`steps[]`、`blocked_by`、`dispatch_refs`），并与确定性活跃锚点（今日日程、高优待办、未结待处理项）及异步滑动滚动摘要组成 Working Context。
   - 全部作为 **JSON 数据字段** 注入每轮 `UserMessage` payload，不混入静态 `system_prompt`（保住 `SOUL.md` 的 Prompt Cache，防止数据越权为指令）。
4. **同员工主会话默认 FIFO 顺序排队 + 分阶段安全强制插入合并。**
   - 输入框依然永不禁用；前一轮进行中时，新消息先落盘 `<req2>:user` 并返回 `state=queued` 排队，前轮收口后顺序衔接。
   - 员工显式触发强制插入（`mode=interrupt_merge`）时按当前轮次副作用阶段分级处理：无写副作用直接取消合并重跑；仅有可撤回私域写入先倒序执行 `undo` 再合并重跑；已派出匠人/编组或含不可撤回写入时禁止静默抛弃，改为注入在途状态由合并轮次显式处置。系统唤醒交付与用户输入绝不混拼合并。
5. **纯 Web 优先的 SSE 实时状态流。**
   - 统一经现有纯 Web SSE 通道推送 `event: plan`、`focus`、`steps`、`dispatch`、`pending`、`superseded`，支撑极简入口顶部 Live Focus 胶囊与右侧可折叠 Aux Pane（进展 / 待处理 / 产物与 Diff / 轨迹与诊断）；Tauri 2 桌面端保持同源薄壳，不新增本机原生权限。

## 不采用

- **引入 Kafka / RabbitMQ / Redis Streams 等外部消息中间件**：玄圃已有 PostgreSQL 与内存双后端契约，PG `LISTEN/NOTIFY` + 既有持久表 Reconciler 零新增运维组件且天然与事务原子绑定。
- **去掉轮询兜底、只靠 `NOTIFY`**：`NOTIFY` 不持久化，连接闪断或进程重启期间会丢通知，必须保留低频 Reconciler 兜底。
- **把管家改成进程内常驻长线程引擎**：破坏多实例无状态扩展与崩溃安全重跑，且难以兼容静态缓存与防上下文腐烂。

## 影响

- 存储：新增 `butler_task_board` 与同会话队列/合并关联字段（向后兼容迁移，只扩不删）；`wake_store` 与 `dispatch_store` 幂等键不变。
- 运行时：`server/engine/runtime/events.py`、`work_tasks`、`butler/pending.py`、`butler/groups/runner.py` 接入 `WakeBus` 发射点；`butler/wakeup.py` 新增定点推进与事件等待循环。
- HTTP / SSE：`POST /v1/assistant/messages` 增加 `state=queued` 与可选 `mode=interrupt_merge`；SSE 增加 `plan` / `focus` / `superseded` 事件。
