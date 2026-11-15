# R5-A：内核升级与稳定层缺口设计

日期：2026-11-14。状态：待实施。上级：[R5 设计](2026-11-14-r5-butler-artisan-design.md)。

## 1 现状

- `uv.lock` 锁定 taifeng 2026.10.1.11；`xuanpu-server` 声明 `taifeng>=2026.10.1.11`；内核本地 `main` 为 2026.11.14.0。
- 执行观测（执行身份、`execution_updated`、执行树、时间线、瀑布、按尝试建 span 的 OTel）在 10 月之后进入内核。
- `ExecutionIdentity`、`ExecutionProjection`、`execution_waterfall` 等从 `taifeng.experimental` 导出；平台红线 3 只允许 `taifeng.__all__`。

## 2 平台真正需要内核提供的

平台要按租户持久化执行事实，内核的进程内投影不持久、冷恢复不重建，所以平台不需要投影类本身，只需要事件里有可靠身份：

| 需要 | 用途 | 当前层 |
| --- | --- | --- |
| `EventMsg.execution` 字段及其形状（execution_id、attempt_id、thread_id、skill_id、model、两个 parent 字段） | span 归属与父子关系 | 类型在实验层 |
| `execution_updated` 事件及 `usage_attempts_*`、`usage_complete` 字段 | 阶段与用量 | 契约在实验层能力文档 |
| `spawn_started` 的 handle → 子执行映射 | 后台子执行收敛 | 同上 |
| `OtelTelemetrySink` 以尝试为 span、带父级 | OTel 导出 | 类在稳定层，行为随版本 |

## 3 给内核的需求（记入 kernel-seam-backlog）

1. 把 `ExecutionIdentity` 与 `EventMsg.execution` 提升到稳定层，并进 `public_api_snapshot`。
2. 把 `execution_updated` 的数据字段写成稳定契约。
3. 可选：提升 `TraceWaterfallRow` 与 `execution_waterfall`，平台可少写一份瀑布计算；不提升则平台自算。

在 1、2 完成前，平台不 import 实验层、不按字段名猜测未承诺的结构；R5-B 的采集功能保持关闭。

## 4 升级本身

- 逐项读内核 2026.10.1.11 → 2026.11.14.0 的变更，列出稳定层的不兼容项与弃用项；`taifeng-sandbox`、`taifeng-stores` 对新内核各跑一遍全量测试。
- 平台只改适配点，不 monkeypatch、不 fork。发现新缺口记 backlog。
- 版本下限改为实际验证过的版本；Docker 镜像里三个包的 wheel 同步。
- 回退：保留旧锁文件与旧镜像；升级不带数据迁移，可直接换回。

## 5 验收

- 三仓全量测试、门禁 ruff、strict mypy 全绿，贴实际输出；`uv lock --check` 通过。
- 现有真实模型验收脚本（工单查询、审批写入、办公任务）在新内核上各跑一次，记录结果。
- 既有事件消费（SSE、回放、审计、用量）在新增字段下行为不变：新增契约测试断言未知字段被忽略而不是报错。
