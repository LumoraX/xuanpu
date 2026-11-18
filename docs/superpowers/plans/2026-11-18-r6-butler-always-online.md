# R6 管家常在线实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans，逐段实施、保留检查点。

**Goal:** 员工交代一件事后可以立刻交代下一件；每件事管家自己办或交给匠人，办完主动回报；一轮的时限与每次请求的预算随任务伸缩。

**Architecture:** 见 [ADR 0020](../../decisions/0020-butler-always-online.md)。在 R5 管家之上改三处：请求只等短窗口、一轮在平台后台继续；一轮的时限可由管家延长；每次请求的预算可由管家提高。派工、唤醒、交付不变。

约定：`S` = `packages/xuanpu-server/src/xuanpu_server`，`T` = `packages/xuanpu-server/tests`。

---

## A：回执与一轮解耦

**Files**：Modify `S/butler/{config,service}.py`、`S/api/butler.py`；Create `T/butler/test_async_turn.py`。

- [ ] `ButlerConfig.reply_window_seconds`（默认 20，1–120）。
- [ ] `ButlerService.message`：一轮作为后台任务启动并登记；等至多窗口时长。办完返回完整回执（`state: "done"`）；没办完返回 `{"mode": "butler", "state": "running", "request_id", "trace_id"}`。后台任务结束时照旧写主对话、完成认领。
- [ ] 同一 `request_id` 在进行中重试：返回进行中回执，不再跑一轮；结束后重试返回最终回执。
- [ ] 一员工可同时有多轮进行中（不同 `request_id`），互不阻塞。
- [ ] 平台关闭时取消并等待全部后台轮次；被取消的一轮不写半截回复。
- [ ] 测试：快的一轮仍同步返回；慢的一轮先回 `running`、结束后主对话出现回复；进行中再发一句立即处理；同标识重试不重跑。

## B：一轮的动态时限

**Files**：Modify `S/butler/{config,engine,tools,turn}.py`、`S/butler/skills/butler/SKILL.md`；Create `T/butler/test_turn_deadline.py`。

- [ ] `ButlerConfig.turn_seconds`（默认 300，10–86400）、`max_turn_seconds`（默认 86400，60–604800，且不小于 `turn_seconds`）；去掉常量 `TURN_SECONDS`。
- [ ] 引擎用可改期的 `asyncio.Timeout`，句柄放在本轮上下文。
- [ ] 工具 `extend_turn(seconds, reason)`：把截止时间延到至多 `max_turn_seconds`（从本轮开始算）；写审计 `butler_turn_extended`；返回新的截止时间。
- [ ] 到时限：一轮正常收尾，主对话写「这一轮已到时限停下」并列出已派出、仍在进行的工作；不再 503。
- [ ] 技能：长工作优先拆成派工或编组；确需继续自己处理时先 `extend_turn` 并说明理由。

## C：每次请求的预算随任务提高

**Files**：Modify `S/butler/{config,budget,tools}.py` 与预算的持久位置；Create `T/butler/test_budget_raise.py`。

- [ ] 开工先核对：每次请求的已用预算从哪里算（`TraceUsage`），提高后的预算存在哪里才能被唤醒的后续轮次读到；结论写进本文件。
- [ ] 配置：`max_tokens_per_trace` 等三项含义改为起始预算，新增 `max_tokens_per_trace_ceiling`（默认 2000 万）、`max_butler_turns_per_trace_ceiling`（默认 64）、`max_artisans_per_trace_ceiling`（默认 16）。
- [ ] 工具 `raise_budget(kind, to, reason)`：只能提高、不能超过上限；租户每日配额不变；写审计 `butler_budget_raised` 与执行追踪；回复里告诉员工。
- [ ] 预算检查读「起始预算与已提高值中较大者」。

## D：中断后的一轮由唤醒重跑

**Files**：Modify `S/butler/{service,wakeup}.py` 与认领记录；Create `T/butler/test_turn_resume.py`。

- [ ] 开工先核对：主对话认领（`claim_route`）在进程退出后的状态与租约；结论写进本文件。
- [ ] 进行中的一轮可被识别为「持有者已不在」；唤醒循环按同一 `request_id` 重跑一次；重跑失败在主对话写明，原话保留。
- [ ] 派工按派工请求标识回放，不重复派出（复用 R5 幂等）。

## E：极简入口与 SDK

**Files**：Modify `xuanpu-console` 的 `entry.js`、`progress-card.js`；SDK `traces.py`；Node 行为测试。

- [ ] 发送后立即恢复输入；收到 `running` 回执显示「处理中」与进展卡；这一轮结束后把主对话的新消息补进来（按游标读，不重复）。
- [ ] SDK：`send_to_butler` 文档写明 `state`；读超时回到不短于回复窗口即可。

## F：验收与文档

- [ ] 单测、ruff、mypy、全量。
- [ ] 真实模型：连续交代三件事（一件慢、一件快、一件派工），第二、三件不等第一件；慢的一轮延长时限；一次请求提高预算；结果都主动回到主对话。
- [ ] 文档：`docs/architecture/{butler,http-api}.md`、`docs/guides/{butler,operations}.md`、`examples/xuanpu.yaml`、决策索引。
