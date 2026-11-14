# R5-E 极简入口实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans。E0 依赖 B；E1、E2 依赖 C。

**Goal:** 员工页只有概览、对话、输入、提示；其余经对话触达，「更多」保留手动兜底。

**Architecture:** 见 [R5-E 设计](../specs/2026-11-14-r5e-minimal-entry-design.md)。新页与旧页并存一个版本；卡片复用原模块的接口、权限与幂等键。

**Tech Stack:** FastAPI、原生 JS、既有 Node 行为测试与浏览器验收脚本。

---

## E0：概览接口

**Files**：Create `S/dashboard/{__init__,service,models}.py`、`S/api/dashboard.py`、`T/dashboard/{test_dashboard,test_dashboard_isolation,test_dashboard_partial_failure}.py`；Modify `S/api/app.py`、`SDK/client.py`、`docs/architecture/http-api.md`。

- [x] 四块各自取数：今日（提醒、日程、到期待办）、等我处理、进行中（活动 trace）、未读通知；每块数量加至多 5 条摘要。
- [x] 数量在授权过滤后计算；单块失败只影响该块；全程不调模型。
- [x] 反例：跨员工、停用员工、功能开关关闭的块不出现而不是报错。
- [x] Run：`uv run pytest packages/xuanpu-server/tests/dashboard -q`；提交 `feat: 增加员工概览接口`。

### E0 进度（2026-11-14，macOS arm64，Python 3.12，本机 taifeng 2026.11.14.1 + taifeng-sandbox `af3e545`）

- 已写：`S/dashboard/{__init__,models,service}.py`、`S/api/dashboard.py`（`GET /v1/dashboard`）、`Platform.dashboard`；`ReminderStore.unread_notifications`（memory 与 PG，总数由存储计算）；SDK `dashboard(timezone=None)`；文档 `docs/architecture/minimal-entry.md`、`http-api.md`「员工概览（R5-E0）」、架构总览。
- 取数口径：今天 = 当天活动提醒 + 当天有交集的日程 + 当天及之前到期的未完成待办；等我处理 = 未处理草稿 + 管家待处理项（管家开启时）+ waiting_user 工作 + 轮到本人审批（审批开启时）；进行中 = 活动 trace（追踪开启时才出现）；未读 = 通知中心站内通知（共享与协作通知流不计）。
- 停用员工与机器身份整个请求 403（不是部分块）；关闭的功能对应块或来源不出现。
- 测试（`.venv/bin/pytest packages/xuanpu-server/tests/dashboard packages/xuanpu-server/tests/reminders packages/xuanpu-sdk-py/tests -q`）：120 passed, 1 skipped；`tests/reminders/test_unread_notifications.py` 对 memory 与 PG 各跑一遍。ruff：All checks passed!；mypy：Success: no issues found in 557 source files。
- 计划写 `uv run pytest`；`uv.lock` 待 taifeng 2026.11.14.1 上 PyPI 后刷新，本机用 `.venv/bin/pytest` 等价执行。

## E1：对话内卡片

**Files**：Create `C/{entry.html,entry.js,entry.css,cards.js,progress-card.js,dashboard.js,tips.js}`、`Q/{entry_behavior.cjs,test_entry_behavior.py,cards_behavior.cjs}`；Modify `S/butler/tools_read.py` 与相关路由（返回列表数据代替 `open_workspace`）、`packages/xuanpu-console/src/xuanpu_console/__init__.py`（入口选择）。

- [ ] 先列对照清单：现有 8 个模块、15 张功能卡、5 个浮层，各自在新页的去处（卡片 / 更多 / 管理员）。清单写进本文件，逐项勾。
- [ ] 列表卡与确认卡：提醒、待办、日程、事项、文件、执行安排、交接、共享、连接、业务跟进。操作调用原接口，带原幂等键与版本号。
- [ ] 必须本人原生选择的动作做成卡片按钮，由本人点击。
- [ ] 进展卡：订阅 trace SSE，展开执行树与瀑布，不完整时标注；刷新后恢复。
- [ ] 身份切换与注销清空全部状态；旧异步结果不回填。
- [ ] Run：`uv run pytest packages/xuanpu-console/tests -q`；提交 `feat: 增加极简入口与对话内卡片`。

## E2：提示、切换与清理

**Files**：Create `S/dashboard/tips.py`、`T/dashboard/test_tips.py`、`docs/guides/minimal-entry.md`；Modify `C/index.html`（成为 `?classic=1`）、`docs/guides/{quickstart,local-trial}.md`、`docs/architecture/personal-workspace.md`、`AGENTS.md` 中入口描述（只改措辞以匹配「更多」，不改约束）。

- [ ] 提示确定性生成，最多 3 条；系统状态提示同位置。
- [ ] `minimal_entry` 开关按租户启用；未启用租户仍是旧页。
- [ ] 真实浏览器验收：首屏四样；各模块在对话内完成一次查看与一次确认；模型屏蔽时概览、通知、「更多」可用；375px；两员工隔离。记录默认页脚本数与体积。
- [ ] 旧页保留一个版本后，另起提交删除不再引用的脚本；功能页脚本保留。
- [ ] 同步文档；提交 `feat: 启用极简入口并保留更多兜底`。

## 对照清单

（E1 第一步填写）
