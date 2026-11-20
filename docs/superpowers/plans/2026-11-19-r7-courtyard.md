# R7 小院 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 员工发一句话后，进展卡里的小院按真实 trace 演出管家与匠人的工作，「经过」面板用角色自己的话讲每一步。

**Architecture:** 服务端在 trace 采集时给工具 span 挂一句「旁白」（`say`），并新增派工细节只读接口；前端把原型拆成纯模型、画法、场景、卡片四个脚本，接到既有进展卡的 trace 增量流上。

**Tech Stack:** Python 3.12 / FastAPI / pydantic / asyncpg；原生 JS（无构建）+ 内联 SVG；Node `vm` 行为测试。

**Spec:** `docs/superpowers/specs/2026-11-19-r7-courtyard-design.md`

## Global Constraints

- 所有 Python 模块顶部 `from __future__ import annotations`；中文 docstring；文件 ≤ 800 行、函数 ≤ 80 行。
- 禁止 silent fallback；采集器 `handle` 不让出、不抛异常。
- `say` 最长 160 字；缓冲上限 2000 字；`result` 截到 600 字。
- 只给本人：员工身份启用，他人的 404，机器身份 403。
- 前端资源带指纹（`scripts/refresh_console_assets.py` 生成）；375px 无横向溢出；`prefers-reduced-motion` 不播动画。
- 门禁：`uv run pytest`、`uv run ruff check --select F,S108,I,TC packages`、`uv run mypy packages/*/src`。

## Review Focus

- 模型一轮里先说话再并行调用多个工具：旁白只挂到第一个工具，其余为空，不重复。（Task 1 测）
- 旁白里混有 `<think>` 块或换行、表情：清洗后仍是一句可读的话，不出现标签。（Task 1 测）
- 派工细节在派工刚建、还没有任何唤醒事件时被读取：`checks`、`result` 为 null，`items` 为空，不报错。（Task 3 测）
- 刷新页面时 trace 已结束：小院直接画终态，不重播整段动画，也不卡在半截。（Task 5 测）
- 派工细节请求失败（404/网络）：小院按会话承载默认动作继续，经过里不出现空信纸。（Task 5 测）

---

### Task 1: 旁白采集（`say`）

**Files:**
- Create: `packages/xuanpu-server/src/xuanpu_server/tracing/narration.py`
- Modify: `packages/xuanpu-server/src/xuanpu_server/tracing/models.py`（`SpanFact.say`、`SpanRecord.say`）
- Modify: `packages/xuanpu-server/src/xuanpu_server/tracing/rules.py`（`new_span`/`apply_fact` 带上 `say`，首值为准）
- Modify: `packages/xuanpu-server/src/xuanpu_server/tracing/events.py`（`Draft.say`）
- Modify: `packages/xuanpu-server/src/xuanpu_server/tracing/collector.py`（每执行缓冲）
- Modify: `packages/xuanpu-server/src/xuanpu_server/tracing/views.py`（输出 `say`）
- Test: `packages/xuanpu-server/tests/tracing/test_narration.py`

**Interfaces:**
- Produces: `narration(text: str) -> str | None`；`SAY_MAX = 160`；`SpanRecord.say: str | None`；`span_view(...)["say"]`

- [ ] Step 1: 写失败测试
  - `narration("好的。我先去档案柜翻上月的报销单。") == "我先去档案柜翻上月的报销单。"`
  - `narration("<think>x</think>\n查一下\n流水") == "查一下 流水"`；`narration("  ") is None`
  - 超长：结果 `len == 160` 且以「…」开头
  - 采集器：同一执行 `assistant_text` ×2 → `tool_call_started`(c1) → `tool_call_started`(c2)：`tl.c1.say` 为合并句，`tl.c2.say is None`
  - `assistant_reasoning` 不进入任何 `say`；`turn_completed` 后再来的工具 `say is None`
  - `apply_fact`：已有 `say` 时新事实带不同 `say` 不覆盖、不标 incomplete
- [ ] Step 2: `uv run pytest packages/xuanpu-server/tests/tracing/test_narration.py` → FAIL
- [ ] Step 3: 实现。缓冲 `dict[execution_id, str]`，上限 2000 字丢头部；`drafts()` 保持纯函数，采集器在得到工具 started 草稿后用 `dataclasses.replace(draft, say=...)` 挂上。
- [ ] Step 4: 跑 `packages/xuanpu-server/tests/tracing` 全部 → PASS（含两套存储契约）
- [ ] Step 5: Commit `feat: trace 工具 span 带上匠人旁白（R7-1）`

### Task 2: 投递时追加说话约定

**Files:**
- Modify: `packages/xuanpu-server/src/xuanpu_server/butler/dispatcher.py`（`NARRATE_HINT`，`_start_task`/`_start_session` 投递 `instruction + "\n\n" + NARRATE_HINT`）
- Test: `packages/xuanpu-server/tests/butler/test_single_dispatch.py`

**Interfaces:**
- Produces: `NARRATE_HINT = "（每次动手前，先用一句话告诉员工你要做什么。）"`

- [ ] Step 1: 测试：会话承载发出的首条消息以原说明开头、以 `NARRATE_HINT` 结尾；派工记录 `instruction` 不含它；同一请求重试仍回放原派工。
- [ ] Step 2: 运行 → FAIL；Step 3: 实现；Step 4: 运行 `tests/butler` → PASS
- [ ] Step 5: Commit `feat: 派工投递附带说话约定（R7-2）`

### Task 3: 派工细节接口

**Files:**
- Modify: `packages/xuanpu-server/src/xuanpu_server/butler/wake_store.py`、`wake_postgres.py`（`events_for_dispatch`）
- Create: `packages/xuanpu-server/src/xuanpu_server/api/dispatches.py`（`dispatch_detail(...)`、`dispatches_router(platform)`）
- Modify: 路由注册处（与 `groups_router` 同处）
- Test: `packages/xuanpu-server/tests/butler/test_dispatch_detail.py`；`tests/butler/test_wake_store.py` 补 `events_for_dispatch`

**Interfaces:**
- Produces: `WakeStore.events_for_dispatch(owner: TraceOwner, dispatch_id: str) -> tuple[ButlerEvent, ...]`；`GET /v1/assistant/dispatches/{dispatch_id}` 返回设计文档里的字段

- [ ] Step 1: 测试：本人读到 `instruction`、`host`、`artisan`；无事件时 `checks/result` 为 null、`items == []`；有决定后 `checks` 为五项、`result` ≤ 600 字；问题与代答项列出且代答带 `source_quote`；他人 404；机器身份 403。
- [ ] Step 2: 运行 → FAIL；Step 3: 实现（取最近一个 `decision` 非空的事件）；Step 4: 运行 → PASS
- [ ] Step 5: Commit `feat: 派工细节只读接口（R7-3）`

### Task 4: 前端纯模型与画法

**Files:**
- Create: `packages/xuanpu-console/src/xuanpu_console/static/courtyard-art.js`
- Create: `packages/xuanpu-console/src/xuanpu_console/static/courtyard-model.js`
- Create: `packages/xuanpu-console/tests/courtyard_behavior.cjs`、`tests/test_courtyard_behavior.py`

**Interfaces:**
- Produces（全局，挂在 `Courtyard` 命名空间）：
  - `Courtyard.art.charMarkup(look) -> string`、`lookFor(pkg) -> {species,color,acc,name}`、`loadLooks()/saveLooks(looks)`
  - `Courtyard.model.stationFor(toolName, state) -> "cabinet"|"books"|"computer"|"tv"|"bed"|"bench"`
  - `Courtyard.model.diff(prev: SpanMap, next: SpanMap, details) -> Beat[]`（Beat: `{type, actor, span, ...}`，type ∈ `wake|brief|work|done_tool|ask|seal|ruling|return|check|fail|reply`）
  - `Courtyard.model.story(spans, details) -> Item[]`（Item: `{who, type, text, place?, tool?, ms?, running?, quote?}`）
  - `Courtyard.model.reasonText(code) -> string`

- [ ] Step 1: 测试：归类表逐项；`diff` 对「派工出现 → 匠人执行 → 工具 → 完成」给出 brief→wake→work→done_tool→return→check 顺序；`story` 用 `say`，无 `say` 时模板句；`waiting_input`+question 给 ask；空 details 不报错；同一快照两次 diff 无节拍。
- [ ] Step 2: 运行 → FAIL；Step 3: 实现（画法从原型搬，`lookFor` 用包名的 FNV 哈希取样子、颜色、配饰）；Step 4: → PASS
- [ ] Step 5: Commit `feat: 小院纯模型与角色画法（R7-4）`

### Task 5: 场景与进展卡接入

**Files:**
- Create: `packages/xuanpu-console/src/xuanpu_console/static/courtyard-scene.js`、`courtyard-card.js`、`courtyard.css`
- Modify: `progress-card.js`（有 `Courtyard.card` 且支持 SVG 时由它渲染；执行树与瀑布放进「执行树」页签）
- Modify: `entry.html`、`scripts/refresh_console_assets.py`（也刷新 `entry.html`）、`tests/test_entry_behavior.py`（脚本清单、允许 `/v1/assistant/dispatches`、`/v1/assistant/groups`）
- Test: `tests/courtyard_behavior.cjs` 增卡片用例；`tests/entry_behavior.cjs` 原用例不改仍过

**Interfaces:**
- Consumes: Task 4 全部；`GET /v1/traces/{id}/events`、`/v1/assistant/dispatches/{id}`、`/v1/assistant/groups/{id}`
- Produces: `Courtyard.card.mount(progress, container)`、`Courtyard.card.update(progress, {snapshot: bool})`

- [ ] Step 1: 测试：快照（trace 已结束）→ 不排动画，经过列出全部条目；派工细节 404 → 经过无信纸条目、小院仍渲染；无 `createElementNS` → 退回执行树。
- [ ] Step 2: 运行 → FAIL；Step 3: 实现（场景从原型搬，导演改为消费 `diff` 节拍）；Step 4: `uv run pytest packages/xuanpu-console` → PASS
- [ ] Step 5: 用无头 Chrome 打开入口页（假后端或原型自检）截图核对 375px 与 980px
- [ ] Step 6: Commit `feat: 进展卡换成小院（R7-5）`

### Task 6: 文档与门禁

**Files:**
- Create: `docs/decisions/0023-execution-story.md`（放宽 0017 决策 5：允许 `say`）
- Modify: `docs/decisions/README.md`、`docs/architecture/execution-trace.md`、`butler.md`、`minimal-entry.md`、`http-api.md`、`docs/guides/minimal-entry.md`

- [ ] Step 1: 写文档
- [ ] Step 2: 跑三道门禁，贴命令与关键输出
- [ ] Step 3: Commit `docs: R7 小院与执行旁白`
