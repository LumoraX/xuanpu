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

- [x] 先列对照清单：现有 8 个模块、15 张功能卡、5 个浮层，各自在新页的去处（卡片 / 更多 / 管理员）。清单写进本文件，逐项勾。
- [ ] 列表卡与确认卡：提醒、待办、日程、事项、文件、执行安排、交接、共享、连接、业务跟进。操作调用原接口，带原幂等键与版本号。（部分：提醒、待办、日程、事项、文件与草稿确认在卡片内完成；执行安排、交接、共享、连接、业务跟进五类是卡片按钮，进入「更多」的原表单完成，未在卡片内重做这些表单，见 E1 进度）
- [x] 必须本人原生选择的动作做成卡片按钮，由本人点击。
- [x] 进展卡：订阅 trace SSE，展开执行树与瀑布，不完整时标注；刷新后恢复。
- [x] 身份切换与注销清空全部状态；旧异步结果不回填。
- [x] Run：`uv run pytest packages/xuanpu-console/tests -q`；提交 `feat: 增加极简入口与对话内卡片`。

### E1 进度（2026-11-14，macOS arm64，Node v24.22.3）

- 已写：`C/{entry.html,entry.css,entry.js,cards.js,progress-card.js,dashboard.js,tips.js,entry-boot.js}`（资源地址带内容指纹）；`Q/{entry_behavior.cjs,test_entry_behavior.py}`；`Q/test_static.py` 允许 `/v1/dashboard`、`/v1/traces`。新页地址 `/console/entry.html`，E2 前不自动跳转。
- 卡片：草稿确认卡（`/v1/assistant/proposals/{id}/confirm|cancel`，草稿标识即幂等键，刷新后按 `GET /v1/assistant/proposals/{id}` 恢复）；列表卡（提醒取消带 `version`、待办完成带 `version`、日程、事项、文件下载）；交办工作状态卡；匠人提问答复卡（`/v1/assistant/pending/{id}/answer`，请求标识在卡片生成时固定）；通知卡（逐条标已读）；按钮卡（执行安排、交接与审批、共享、连接、业务跟进、个人能力等跳到「更多」原表单，由本人点击）。
- 进展卡：`fetch` 读 `/v1/traces/{id}/events`，执行树按父子嵌套、缺父的挂根层；「看瀑布」读 `/waterfall`，运行中的行不画时长；记录不完整时标注。刷新后由主对话历史里的 `metadata.butler.trace_id` 重建，最近 3 张自动订阅。
- 身份：退出或身份切换时代际加一、中止全部请求与流、清空概览、对话、提示与输入；旧代际的结果一律丢弃（行为测试覆盖迟到的管家回执）。
- 与计划的偏离：未改 `S/butler/tools_read.py`——列表卡在客户端调原模块列表接口，分流与旧页的 `open_workspace` 语义不变；卡片行为测试合并在 `entry_behavior.cjs`，未单列 `cards_behavior.cjs`。
- 测试：`.venv/bin/pytest packages/xuanpu-console/tests -q` → 29 passed；`node tests/entry_behavior.cjs src/xuanpu_console/static` → entry behavior passed。

## E2：提示、切换与清理

**Files**：Create `S/dashboard/tips.py`、`T/dashboard/test_tips.py`、`docs/guides/minimal-entry.md`；Modify `C/index.html`（成为 `?classic=1`）、`docs/guides/{quickstart,local-trial}.md`、`docs/architecture/personal-workspace.md`、`AGENTS.md` 中入口描述（只改措辞以匹配「更多」，不改约束）。

- [x] 提示确定性生成，最多 3 条；系统状态提示同位置。
- [x] `minimal_entry` 开关按租户启用；未启用租户仍是旧页。
- [ ] 真实浏览器验收：首屏四样；各模块在对话内完成一次查看与一次确认；模型屏蔽时概览、通知、「更多」可用；375px；两员工隔离。记录默认页脚本数与体积。（部分：除「各模块在对话内查看与确认」外均已在真实 Chrome 通过，见 [验收记录](../../guides/releases/2026-11-14-r5e-browser-acceptance.md)；该项需要真实模型，待补跑）
- [ ] 旧页保留一个版本后，另起提交删除不再引用的脚本；功能页脚本保留。（按设计延后：本分支保留旧页全部脚本，下一个版本另起提交）
- [ ] 同步文档；提交 `feat: 启用极简入口并保留更多兜底`。

### E2 进度（2026-11-14，macOS arm64，Python 3.12，Node v24.22.3，Chrome 157.0.8103.79）

- 已写：`S/dashboard/{tips,config}.py`（确定性提示、`MinimalEntryConfig`）；概览响应带 `tips`；`ServerConfig.minimal_entry`（名单内租户须同时开启 butler，配置校验）；`Platform.minimal_entry`；`GET /v1/me` 增加 `entry`（minimal / classic）；旧页 `identity.js` 读到 minimal 且非 `?classic=1`、非管理员入口时跳到 `entry.html`，新页读到 classic 时回旧页。
- 文档：`docs/guides/minimal-entry.md`（新）、`docs/architecture/minimal-entry.md`、`http-api.md`（`/v1/me` 的 `entry`、概览 `tips`）、`personal-workspace.md`、`operations.md`、`quickstart.md`、`local-trial.md`、使用手册索引、`examples/xuanpu.yaml`、`AGENTS.md`（只在「功能」入口后注明新页里叫「更多」，约束不变）。
- 测试：`.venv/bin/pytest packages/xuanpu-server/tests/dashboard packages/xuanpu-server/tests/test_config.py packages/xuanpu-server/tests/identity packages/xuanpu-console/tests packages/xuanpu-sdk-py/tests -q` → 575 passed, 7 skipped；ruff：All checks passed!；mypy：Success: no issues found in 559 source files。
- 真实浏览器（`scripts/acceptance/r5_minimal_entry_browser.py`，375px 经 `r5_minimal_entry_cdp.cjs` 走 DevTools 协议模拟设备，因为无头窗口不能窄于约 500px）：首屏四样、概览四块、提示、「更多」、两员工隔离、375px 无横向溢出全部通过；模型调用尝试 0；默认页 6 个脚本 29489 字节、样式 3791 字节。记录见 `docs/guides/releases/2026-11-14-r5e-browser-acceptance.md`。
- 全量（沙盒外，`.venv/bin/pytest -q packages`）：3761 passed / 60 failed / 49 errors / 443 skipped；失败与错误用例集合与 main 已知的环境项（办公容器找不到 `docker`）逐条一致，无新增。
- 未完成：真实浏览器里「各模块在对话内查看与确认」需要真实模型（管家给草稿），本环境没有模型凭据；系统状态提示缺「连接授权过期」「预算将尽」两类；首次打开 `/console/` 会先加载旧页再跳转；旧脚本删除按设计放到下一个版本。

## 对照清单

E1 第一步填写（2026-11-14，以 `static/index.html` 实际内容为准）。计划写「8 个模块、15 张功能卡、5 个浮层」；实际是 8 个侧栏模块、15 张功能卡、6 个浮层（另有 2 个内嵌面板），全部列出。去处：**卡片** = 在新页对话里看与操作（调用原接口、原幂等键与版本）；**卡片按钮** = 对话里出现明确按钮，由本人点击后在「更多」里完成原生选择或确认；**更多** = 只在功能页（`index.html?classic=1`）；**管理员** = `?admin=1` 不变。新页不删除任何功能逻辑。

侧栏模块（8）：

- [x] 交办工作 → 卡片（受理回执与状态）+ 更多（成果下载与行动项确认）
- [x] 执行安排 → 卡片按钮（草稿须本人在原表单确认）+ 更多
- [x] 文件 → 卡片（列表、下载）+ 更多（上传、文件夹、版本、回收站）
- [x] 事项 → 卡片（列表）+ 更多（编辑、关联资料）
- [x] 待办 → 卡片（列表、标记完成，带版本号）+ 更多
- [x] 日程 → 卡片（列表）+ 更多（编辑）
- [x] 个人能力 → 更多
- [x] 助手设置 → 更多

功能卡（15）：

- [x] 执行安排 → 同上
- [x] 交办工作 → 同上
- [x] 企业连接 → 卡片按钮（绑定与授权须本人原生确认）→ 更多（`?classic=1&connections=1`）
- [x] 工作交接与审批 → 卡片按钮（接受、审批由本人点击）→ 更多；轮到本人审批的数量进概览「等我处理」
- [x] 共享协作 → 卡片按钮（资料选择须本人原生选择）→ 更多
- [x] 组织与职责 → 更多（含获授权同事的配置）
- [x] 我的能力 → 更多
- [x] 发票报销记录与审批 → 更多
- [x] 我的文件 → 同「文件」
- [x] 事项与待办 → 同「事项」「待办」
- [x] 日程 → 同「日程」
- [x] 个人能力与设置 → 更多
- [x] 提醒 → 卡片（草稿确认卡；列表、取消，带版本号）+ 更多（编辑、历史状态）
- [x] 业务跟进 → 卡片按钮（决定须本人在原表单确认）→ 更多
- [x] 通知 → 概览「未读通知」+ 卡片（最近通知、标为已读）+ 更多（通知中心）

浮层与面板（6 + 2）：

- [x] 通知面板 `notifications-panel` → 卡片（通知卡）
- [x] 工作窗口 `workspace-window` → 卡片（列表卡）+ 更多
- [x] 协作面板 `collaboration-panel` → 卡片按钮 → 更多
- [x] 共享面板 `shared-panel` → 卡片按钮 → 更多
- [x] 组织面板 `organization-panel` → 更多
- [x] 连接面板 `connections-panel` → 卡片按钮 → 更多
- [x] 内嵌：提醒面板 `reminders-panel` → 卡片（列表卡）
- [x] 内嵌：能力面板 `capability-panel` → 更多

管理员页签（业务包、能力配置、审计、用量、用户）→ 管理员（`?admin=1`），不进新页。
