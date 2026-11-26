# ADR 0026：本地双层 Transcript 完整保留与跨节点透明检索

- 状态：Proposed（2026-11-27，待实施）
- 关联：补充 [ADR 0017](0017-butler-artisan-orchestration.md) 决策 5（执行追踪 `trace_store` 仍只存元数据与旁白，不存大正文；完整执行现场独立归口至本 ADR 的 Transcript 存储）与 [ADR 0023](0023-execution-story.md)
- 规划：[运行时、造匠与可观测底座规划](../superpowers/plans/2026-11-27-butler-runtime-forge-and-observability-plan.md)

## 背景

目前玄圃的会话中间信息落盘状态并不统一：

1. 管家每轮临时会话（[butler/engine.py](../../server/engine/butler/engine.py)）默认往本机 `<data_dir>/butler/<租户>/threads/` 写盘，但没有统一索引、没有按 `trace_id`/`span_id` 组织，也无脱敏与容量治理，既不便排查又存在长期攒盘风险。
2. 业务会话在 PostgreSQL 状态面下主存进库，但执行过程细节（工具完整输出、Hook 决策、报错堆栈、上下文裁剪过程）分散在审计、日志与本地临时目录中，缺少统一回放视角。
3. 平台处于高频迭代期，模型与编排链路均可能出现隐蔽异常；**不仅开发者需要还原完整现场，未来玄圃本身也需要具备「自我排查（Self-Diagnosis）」能力**，若直接丢弃本地中间过程，出了问题将无从定位。同时在多节点部署下，各节点本地落盘的记录必须对集群内任意节点透明可查。

## 决策

1. **统一保留本地完整双层 Transcript（Fast Local Write）。**
   - 管家每轮（`bt_*`）、匠人会话轮、WorkTask 与 Forge 造匠轮均在执行节点本地统一写入规范化目录（目录 `0700`、文件 `0600`）：
     `<data_dir>/transcripts/<tenant_dir>/<yyyy-mm-dd>/<trace_id>/<span_id>.compact.jsonl` 与 `<span_id>.full.jsonl`。
   - **紧凑版（`compact.jsonl`）**：每步记录 `step_index`、`ts`、`trace_id`、`span_id`、`parent_span_id`、`kind`、`status`、`duration_ms`、`usage`、摘要内容与 `truncated_fields` 标记，供快速扫描与定位异常步。
   - **完整版（`full.jsonl`）**：保留完整模型交互、工具参数/输出、Hook 决策与错误堆栈，支持按行号精确切片回读，避免把几十万字上下文一次性灌入排查者或自检 Agent 窗口。
   - 原 `butler/engine.py` 散落的 `butler/<租户>/threads` 统一收口到此机制，并配置热盘保留策略（TTL + 最大容量上限），防止无限攒盘。
2. **统一脱敏红线（可复现逻辑，不留凭据明文）。**
   - 所有内容写入 Transcript 前强制经过平台统一 `sanitize` 脱敏管道，剥离密钥、Cookie、`Authorization` 头、连接器凭据明文与签名。
   - Transcript 严格按 `tenant_id` 与角色权限隔离，严禁跨租户读取，也不经 OTel 对外导出。
3. **「各自节点落盘、全节点均可查」的三态透明读取架构。**
   - **全局轻量索引（PG `transcript_index`）**：仅存元数据指针（`trace_id`、`span_id`、`parent_span_id`、`tenant_id`、`user_id`、`kind`、`node_id`、`node_addr`、`local_relpath`、`compact_object_key`、`full_object_key`、`byte_size`、`line_count`、`status`、`created_at`、`archived_at`），不把大正文塞进关系数据库。
   - **三态读取器（`TranscriptReader`，集群内任意节点可用）**：
     1. **本节点直读（Local Fast Read）**：`record.node_id == self.node_id` 且本地存在 → 直接按行切片读取；
     2. **跨节点签名热读（Node-to-Node Signed Proxy）**：记录落在其他活跃节点且尚未归档 → 通过集群内部带短期签名票据的只读接口 `GET /internal/nodes/{node_id}/transcripts/{span_id}?mode=compact|full&start_line=&end_line=` 流式拉取；
     3. **冷归档兜底（Shared `ObjectStore` Cold Read）**：轮次终态后后台低优异步压缩（`.jsonl.zst`）上传至共享 `ObjectStore` 并回填 `object_key`（节点优雅停机或热盘淘汰前强制刷出），保证原节点重启或缩容后任意节点仍可完整回读。
4. **多步接力中间产物沿用同一原则。**
   - 沙盒执行中先写各自节点本地盘；跨步/跨匠人移交的产物在验收冻结时写入不可变 `ObjectStore` 并记入产物索引，确保上游节点 A 的产物可被下游节点 B 的沙盒只读挂载。
5. **预留「玄圃自我排查（Self-Diagnosis）」只读原语。**
   - 在权限校验之后提供 `list_failed_traces` 与 `read_transcript(trace_id, span_id, mode, start_line, end_line)` 只读接口；先读 `compact.jsonl` 定位故障步，再按行读 `full.jsonl` 还原根因，未来可由内置诊断能力联动 ADR 0025 的 `ArtisanForge` 自动提出修复草稿。

## 不采用

- **把管家临时轮改成纯内存（`EphemeralMessageStore`）且不留本地 Transcript**：省盘，但在开发与高频演进阶段丢失完整调用现场，无法事后复盘，也阻断未来自我排查能力。
- **把每一步完整工具输出与提示词全部实时写入 PostgreSQL**：极易撑爆数据库 WAL 与磁盘 I/O，拖慢主业务事务；用「本地 JSONL + PG 极小索引 + `ObjectStore` 异步冷归档」在写性能、成本与跨节点可查性之间取得最佳平衡。
- **跨节点直接共享 NFS / 网络挂载盘作为本地写盘路径**：网络抖动会直接卡死每一步执行且破坏 `FileInstanceGuard` 与缓存独占语义。

## 影响

- 存储：新增 PG `transcript_index` 表（只存元数据，向后兼容迁移）；`ObjectStore` 新增 `tenants/<t>/transcripts/<trace_id>/<span_id>.{compact,full}.jsonl.zst` 归档键空间。
- 运行时：在 `EngineEventHub` 与 `butler/engine.py` 接入统一 Transcript Writer 与后台压缩归档协程；新增集群内部签名只读代理路由 `/internal/nodes/{node_id}/transcripts/{span_id}`。
- 运维与安全：本地 `transcripts/` 强制 `0700`/`0600` 权限、统一脱敏与容量/TTL 自动清理；纯 Web Aux Pane「轨迹与诊断」标签按权限开放切片查看。
