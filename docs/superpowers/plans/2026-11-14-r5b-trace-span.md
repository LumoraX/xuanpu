# R5-B trace 与 span 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans，依赖 A。

**Goal:** 每次请求有持久的执行树与瀑布，可选导出 OTel。

**Architecture:** 见 [R5-B 设计](../specs/2026-11-14-r5b-trace-span-design.md)。一个只读事件消费者把执行事实折成 span 行；memory 与 PG 两套存储同契约。

**Tech Stack:** Pydantic、asyncpg、既有事件订阅与 SSE、taifeng `OtelTelemetrySink`。

---

## B0：模型与存储

**Files**：Create `S/tracing/{__init__,models,store,memory,postgres,retention}.py`、`S/controlplane/migrations/<编号>_execution_spans.sql`、`T/tracing/{test_span_store,test_span_isolation,test_span_retention}.py`；Modify `S/config.py`（`execution_trace`）、`S/platform.py`（装配）。

- [x] 先写反例：跨租户、跨员工读取；重复事件不重复记用量；迟到与跳号标 `incomplete`；超过 span 上限停止新增；取消请求不直接变 cancelled。
- [x] 实现 span 模型与两套存储；状态转换写成纯函数并单测；不存正文字段，测试断言参数与输出进不了存储。
- [x] 保留期清理按租户分批，不动审计（`S/tracing/retention.py`，平台启动时开启，每小时一轮）。
- [ ] Run：`uv run pytest packages/xuanpu-server/tests/tracing -q`；提交 `feat: 增加按租户持久的执行 span 存储`。

### B0 进度（2026-11-14，分支 `feat/r5-butler-artisan`，未提交）

- 已写：`S/tracing/{__init__,config,models,rules,apply,store,memory,postgres,waterfall}.py`；迁移 `0021_execution_spans`（R4 候选号顺延）；`T/tracing/*`，存储契约对 memory 与 PostgreSQL 各跑一遍；`S/config.py` 增加 `execution_trace` 段；`examples/xuanpu.yaml`、`docs/guides/operations.md` 补配置说明；迁移版本相关的两个既有测试改到 21。
- 门禁（2026-11-14，Linux x86_64，Python 3.12，PostgreSQL 16.16 + pgvector 0.8.6，taifeng-sandbox 按 CI 钉的 `af3e545`）：
  - `ruff check --select F,S108,I,TC packages`：All checks passed!；`mypy packages/*/src`：Success: no issues found in 498 source files；`uv lock --check`：通过。
  - 全量 `pytest -q`：分支 135 failed / 3735 passed / 31 skipped / 165 errors；同环境 main 134 failed / 3672 passed / 31 skipped / 165 errors。两边失败与错误的用例集合逐条比对一致（差异仅一例 `test_every_config_key_is_documented`，已补 `examples/xuanpu.yaml` 后通过；复跑相关 405 例全过）。共同失败来自环境：Docker 守护进程不可用且镜像仓库不可达（办公沙盒镜像构建失败）、bwrap 沙盒内找不到仓库外的虚拟环境解释器。**CI 环境下的全量绿灯仍需在 GitHub Actions 或本机确认。**
- 待做：`retention.py` 接入既有维护循环（随 B1 一起装配）。

## B1：采集与跨执行关联

**Files**：Create `S/tracing/{collector,binding}.py`、`T/tracing/{test_collector,test_cross_engine_parent,test_restart_gap}.py`；Modify `S/runtime/{events,sessions}.py` 的既有订阅接缝、`S/work_tasks/{service,execution_context}.py`（保存 trace 绑定）。

- [x] 用内核 Sim 客户端造并行同名技能、后台子执行、挂起恢复三类事件流，断言树形、工具归属、同一 span 的新尝试。
- [ ] 任务与会话创建时写入 `trace_id` 与父 span；匠人引擎根尝试据此挂接。未绑定的旧工作显示为独立 trace，不猜父级。
  - 2026-11-14（R5-C2 核对）：仍未做。派工记录已保存 `trace_id`、`parent_span_id` 与 `host_ref`，可在读取时关联；但让匠人会话的采集器写进管家的同一条 trace，需要两个采集器并发写一条 trace，而现行存储按采集器自编的事实序号去重、跳号即标不完整，会互相误判。须先把序号改为存储侧分配（或按采集器分段），属于 R5-B 层改动，另行实施。
- [x] 采集失败只记错误并标不完整，不影响对话；用独立进程真实终止后重启，断言树可读且缺口有标记。
- [ ] Run：`uv run pytest packages/xuanpu-server/tests/tracing -q`；提交 `feat: 采集执行事实并关联跨引擎父子`。

### B1 进度（2026-11-14，分支 `feat/r5-butler-artisan`）

- 已写：`S/tracing/{events,collector,retention}.py`；`EngineEventHub` 经 `RuntimeDefaults.execution_trace` 自建采集器（`runtime/sessions.py` 已在 800 行上限，只改一行）；`ControlPlaneStore.trace_store`；平台启动/关闭清理任务。
- 测试：`T/tracing/test_collector.py` 用真实内核引擎与脚本化模型跑并行同名 worker、工具、两轮、写入失败；`test_platform_tracing.py` 经平台、会话与事件枢纽端到端；`test_retention.py`。
- 未做：「任务与会话创建时写入 `trace_id` 与父 span」依赖 R5-C 的派工记录，随 C0/C1 实现。冷恢复后旧 trace 不会自动标不完整（见架构文档已知限制）。
- 依赖：内核 taifeng 2026.11.14.1（执行身份进入稳定层）。旧内核下没有执行身份事件，采集器不产生记录。

## B2：接口、瀑布、OTel

**Files**：Create `S/api/traces.py`、`S/tracing/{views,waterfall,otel}.py`、`SDK/traces.py`、`CLI/trace_commands.py`、`T/tracing/{test_trace_api,test_waterfall,test_otel_export}.py`；Modify `S/api/app.py`、`docs/architecture/http-api.md`、`docs/guides/audit-and-usage.md`。

- [x] 四个只读接口与 SSE 增量；分页总数与当前页无关；无权 404。（SSE 见 `S/api/trace_events.py`、`T/tracing/test_trace_events.py`；SDK `follow_trace`；CLI 未加）
- [x] 瀑布行由持久 span 计算（偏移、时长、深度、状态）；运行中的行只给开始时间，不编造时长。
- [x] OTel 配置构造注入、默认关闭；关闭时断言零连接；开启时用本机收集器替身断言父子与属性、无正文。（`S/tracing/otel.py`、`T/tracing/test_otel_export.py`；`xuanpu.trace_id` 无法写入，记缺口第 32 项）
- [ ] 真实模型跑一次并行场景，核对交叠与用量一致，结果入验收记录。
- [x] 同步 architecture 与 guides（execution-trace、http-api、operations、SDK README、缺口清单）；提交 `feat: 开放执行树与瀑布查询及可选 OTel 导出`。

### B2 进度（2026-11-14，macOS arm64，Python 3.12.13，本机 taifeng 2026.11.14.1 + taifeng-sandbox `af3e545`）

- 已写：`S/api/trace_events.py`（SSE：快照 → 变化 → end，`Last-Event-ID` 补齐，每轮重认证）；`S/tracing/views.py`（视图从接入层拆出，避免环依赖）；`S/tracing/otel.py`（`OtelConfig`、按租户懒建内核 `OtelTelemetrySink` 的 `OtelExport`）；`TraceWiring`（`S/tracing/config.py`）经 `RuntimeDefaults.trace_wiring` 交给事件枢纽；配置段 `telemetry.otel`；`xuanpu-server[otel]` 可选依赖，dev 组加 `taifeng[telemetry-otel]`。
- 测试：`T/tracing` 79 passed；SDK 34 passed。
- 未做：CLI 只读命令；真实模型并行场景（依赖 R5-C 派工）。`uv.lock` 仍需在 taifeng 2026.11.14.1 上 PyPI 后刷新。
