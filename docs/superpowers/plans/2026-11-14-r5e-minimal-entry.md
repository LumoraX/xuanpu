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
- [x] Run：`uv run pytest tests/server/dashboard -q`；提交 `feat: 增加员工概览接口`。

### E0 进度（2026-11-14，macOS arm64，Python 3.12，本机 taifeng 2026.11.14.1 + taifeng-sandbox `af3e545`）

- 已写：`S/dashboard/{__init__,models,service}.py`、`S/api/dashboard.py`（`GET /v1/dashboard`）、`Platform.dashboard`；`ReminderStore.unread_notifications`（memory 与 PG，总数由存储计算）；SDK `dashboard(timezone=None)`；文档 `docs/architecture/minimal-entry.md`、`http-api.md`「员工概览（R5-E0）」、架构总览。
- 取数口径：今天 = 当天活动提醒 + 当天有交集的日程 + 当天及之前到期的未完成待办；等我处理 = 未处理草稿 + 管家待处理项（管家开启时）+ waiting_user 工作 + 轮到本人审批（审批开启时）；进行中 = 活动 trace（追踪开启时才出现）；未读 = 通知中心站内通知（共享与协作通知流不计）。
- 停用员工与机器身份整个请求 403（不是部分块）；关闭的功能对应块或来源不出现。
- 测试（`.venv/bin/pytest tests/server/dashboard tests/server/reminders tests/sdk -q`）：120 passed, 1 skipped；`tests/reminders/test_unread_notifications.py` 对 memory 与 PG 各跑一遍。ruff：All checks passed!；mypy：Success: no issues found in 557 source files。
- 计划写 `uv run pytest`；`uv.lock` 待 taifeng 2026.11.14.1 上 PyPI 后刷新，本机用 `.venv/bin/pytest` 等价执行。

## E1：对话内卡片

**Files**：Create `C/{entry.html,entry.js,entry.css,cards.js,progress-card.js,dashboard.js,tips.js}`、`Q/{entry_behavior.cjs,test_entry_behavior.py,cards_behavior.cjs}`；Modify `S/butler/tools_read.py` 与相关路由（返回列表数据代替 `open_workspace`）、`web/__init__.py`（入口选择）。

- [x] 先列对照清单：现有 8 个模块、15 张功能卡、5 个浮层，各自在新页的去处（卡片 / 更多 / 管理员）。清单写进本文件，逐项勾。
- [x] 列表卡与确认卡：提醒、待办、日程、事项、文件、执行安排、交接、共享、连接、业务跟进。操作调用原接口，带原幂等键与版本号。（五类补齐见「E1 补齐」；需要选文件、上传或下载核验固定成果的少数动作仍在「更多」，理由见对照清单）
- [x] 必须本人原生选择的动作做成卡片按钮，由本人点击。
- [x] 进展卡：订阅 trace SSE，展开执行树与瀑布，不完整时标注；刷新后恢复。
- [x] 身份切换与注销清空全部状态；旧异步结果不回填。
- [x] Run：`uv run pytest tests/web -q`；提交 `feat: 增加极简入口与对话内卡片`。

### E1 进度（2026-11-14，macOS arm64，Node v24.22.3）

- 已写：`C/{entry.html,entry.css,entry.js,cards.js,progress-card.js,dashboard.js,tips.js,entry-boot.js}`（资源地址带内容指纹）；`Q/{entry_behavior.cjs,test_entry_behavior.py}`；`Q/test_static.py` 允许 `/v1/dashboard`、`/v1/traces`。新页地址 `/console/entry.html`，E2 前不自动跳转。
- 卡片：草稿确认卡（`/v1/assistant/proposals/{id}/confirm|cancel`，草稿标识即幂等键，刷新后按 `GET /v1/assistant/proposals/{id}` 恢复）；列表卡（提醒取消带 `version`、待办完成带 `version`、日程、事项、文件下载）；交办工作状态卡；匠人提问答复卡（`/v1/assistant/pending/{id}/answer`，请求标识在卡片生成时固定）；通知卡（逐条标已读）；按钮卡（执行安排、交接与审批、共享、连接、业务跟进、个人能力等跳到「更多」原表单，由本人点击）。
- 进展卡：`fetch` 读 `/v1/traces/{id}/events`，执行树按父子嵌套、缺父的挂根层；「看瀑布」读 `/waterfall`，运行中的行不画时长；记录不完整时标注。刷新后由主对话历史里的 `metadata.butler.trace_id` 重建，最近 3 张自动订阅。
- 身份：退出或身份切换时代际加一、中止全部请求与流、清空概览、对话、提示与输入；旧代际的结果一律丢弃（行为测试覆盖迟到的管家回执）。
- 与计划的偏离：未改 `S/butler/tools_read.py`——列表卡在客户端调原模块列表接口，分流与旧页的 `open_workspace` 语义不变；卡片行为测试合并在 `entry_behavior.cjs`，未单列 `cards_behavior.cjs`。
- 测试：`.venv/bin/pytest tests/web -q` → 29 passed；`node tests/entry_behavior.cjs src/xuanpu_console/static` → entry behavior passed。

### E1 补齐（2026-11-14，Node v24.22.3）

- 已写：`C/module-cards.js`（业务跟进、执行安排、企业连接）、`C/collab-cards.js`（共享事项、工作交接、组织审批、管家起草的交接）；`cards.js` 不再给这五类只放按钮卡；新页脚本 6 → 8 个（未压缩 51492 字节）。
- 请求体与功能页逐字段一致：业务跟进 `POST /v1/proactive/{id}/actions`（`version`、`item_version`、动作，延后/提醒由本人选时间）；执行安排确认原样提交服务端预览过的请求（含其 `request_id`）加 `confirmed:true`，暂停/恢复/取消带 `expected_version=state_version`；连接同意 `POST …/consents`、撤销带 `expected_version`；共享草稿幂等键 `草稿标识:shared:摘要前32位`；交接动作 `{request_id, confirmed, expected_version}`，签收带 `delivery_version`，退回/补充/请求取消/争议须本人写原因；审批决定 `POST /v1/approval-requests/{id}/{approve|reject|return|withdraw}`；管家起草的交接由本人选来源事项、接收人、审批规则、资料版本，幂等键 `handoff:草稿标识:请求体摘要`。同一张卡重试复用冻结请求；4xx 不重试，结果未知（5xx/网络）可用同一请求重试。
- 留在「更多」的动作（原生页面必需）：企业微信绑定跳转到 `open.weixin.qq.com` 官方授权页；交接的转交、补充资料、发布与交付成果（要选或上传文件）；下载并按 SHA-256 核验固定成果（审批前核对）；通讯录预览（管理员）。
- 测试：`node tests/entry_behavior.cjs src/xuanpu_console/static` → entry behavior passed（新增 6 例：业务跟进草稿重试复用同一请求、执行安排确认与暂停带版本、连接同意与撤销、共享草稿幂等键、交接与审批动作（原因必填、需选文件的动作指向更多）、管家起草的交接幂等键）；`.venv/bin/pytest tests/web -q` → 30 passed。

## E2：提示、切换与清理

**Files**：Create `S/dashboard/tips.py`、`T/dashboard/test_tips.py`、`docs/guides/minimal-entry.md`；Modify `C/index.html`（成为 `?classic=1`）、`docs/guides/{quickstart,local-trial}.md`、`docs/architecture/personal-workspace.md`、`AGENTS.md` 中入口描述（只改措辞以匹配「更多」，不改约束）。

- [x] 提示确定性生成，最多 3 条；系统状态提示同位置。
- [x] `minimal_entry` 开关按租户启用；未启用租户仍是旧页。
- [x] 真实浏览器验收：首屏四样；各模块在对话内完成一次查看与一次确认；模型屏蔽时概览、通知、「更多」可用；375px；两员工隔离。记录默认页脚本数与体积。（2026-11-17 补齐「各模块在对话内查看与确认」：真实 Chrome 加真实模型跑两轮：一轮 13/13，另一轮 12/13（「打开提醒列表」时管家用文字列出而没给列表卡）——提醒、待办、日程、事项各确认一次并经原模块接口核对已保存，九个模块各查看一次；其余项见 [R5-E 浏览器验收](../../guides/releases/2026-11-14-r5e-browser-acceptance.md)，本次见 [R5 剩余验收](../../guides/releases/2026-11-17-r5-remaining-acceptance.md)）
- [ ] 旧页保留一个版本后，另起提交删除不再引用的脚本；功能页脚本保留。（按设计延后：本分支保留旧页全部脚本，下一个版本另起提交）
- [x] 同步文档；提交 `feat: 启用极简入口并保留更多兜底`。（`33fd7f4`）

### E2 进度（2026-11-14，macOS arm64，Python 3.12，Node v24.22.3，Chrome 157.0.8103.79）

- 已写：`S/dashboard/{tips,config}.py`（确定性提示、`MinimalEntryConfig`）；概览响应带 `tips`；`ServerConfig.minimal_entry`（名单内租户须同时开启 butler，配置校验）；`Platform.minimal_entry`；`GET /v1/me` 增加 `entry`（minimal / classic）；旧页 `identity.js` 读到 minimal 且非 `?classic=1`、非管理员入口时跳到 `entry.html`，新页读到 classic 时回旧页。
- 文档：`docs/guides/minimal-entry.md`（新）、`docs/architecture/minimal-entry.md`、`http-api.md`（`/v1/me` 的 `entry`、概览 `tips`）、`personal-workspace.md`、`operations.md`、`quickstart.md`、`local-trial.md`、使用手册索引、`examples/xuanpu.yaml`、`AGENTS.md`（只在「功能」入口后注明新页里叫「更多」，约束不变）。
- 测试：`.venv/bin/pytest tests/server/dashboard tests/server/test_config.py tests/server/identity tests/web tests/sdk -q` → 575 passed, 7 skipped；ruff：All checks passed!；mypy：Success: no issues found in 559 source files。
- 真实浏览器（`scripts/acceptance/r5_minimal_entry_browser.py`，375px 经 `r5_minimal_entry_cdp.cjs` 走 DevTools 协议模拟设备，因为无头窗口不能窄于约 500px）：首屏四样、概览四块、提示、「更多」、两员工隔离、375px 无横向溢出全部通过；模型调用尝试 0；默认页 6 个脚本 29489 字节、样式 3791 字节。记录见 `docs/guides/releases/2026-11-14-r5e-browser-acceptance.md`。
- 全量（沙盒外，`.venv/bin/pytest -q packages`）：3761 passed / 60 failed / 49 errors / 443 skipped；失败与错误用例集合与 main 已知的环境项（办公容器找不到 `docker`）逐条一致，无新增。
- 未完成：真实浏览器里「各模块在对话内查看与确认」需要真实模型（管家给草稿），本环境没有模型凭据；系统状态提示缺「连接授权过期」「预算将尽」两类；首次打开 `/console/` 会先加载旧页再跳转；旧脚本删除按设计放到下一个版本。

## 对照清单

E1 第一步填写（2026-11-14，以 `static/index.html` 实际内容为准）。计划写「8 个模块、15 张功能卡、5 个浮层」；实际是 8 个侧栏模块、15 张功能卡、6 个浮层（另有 2 个内嵌面板），全部列出。去处：**卡片** = 在新页对话里看与操作（调用原接口、原幂等键与版本）；**卡片按钮** = 对话里出现明确按钮，由本人点击后在「更多」里完成原生选择或确认；**更多** = 只在功能页（`index.html?classic=1`）；**管理员** = `?admin=1` 不变。新页不删除任何功能逻辑。

侧栏模块（8）：

- [x] 交办工作 → 卡片（受理回执与状态）+ 更多（成果下载与行动项确认）
- [x] 执行安排 → 卡片（草稿确认、列表、暂停/恢复/取消，带版本）+ 更多（新建表单、修改、执行明细）
- [x] 文件 → 卡片（列表、下载）+ 更多（上传、文件夹、版本、回收站）
- [x] 事项 → 卡片（列表）+ 更多（编辑、关联资料）
- [x] 待办 → 卡片（列表、标记完成，带版本号）+ 更多
- [x] 日程 → 卡片（列表）+ 更多（编辑）
- [x] 个人能力 → 更多
- [x] 助手设置 → 更多

功能卡（15）：

- [x] 执行安排 → 同上
- [x] 交办工作 → 同上
- [x] 企业连接 → 卡片（状态、本人资料授权同意、撤销，带版本）+ 绑定按钮（跳转企业微信官方授权页，原生页面必需）+ 更多（通讯录预览等管理员动作）
- [x] 工作交接与审批 → 卡片（收到的交接、接收/开始/取消/签收/退回等、审批决定、管家起草的交接）+ 更多（转交、补充资料、提交成果、下载核验固定成果——需要选择或核验文件）；轮到本人审批的数量进概览「等我处理」
- [x] 共享协作 → 卡片（列表、管家起草的新建确认）+ 更多（邀请成员、发布资料、上传）
- [x] 组织与职责 → 更多（含获授权同事的配置）
- [x] 我的能力 → 更多
- [x] 发票报销记录与审批 → 更多
- [x] 我的文件 → 同「文件」
- [x] 事项与待办 → 同「事项」「待办」
- [x] 日程 → 同「日程」
- [x] 个人能力与设置 → 更多
- [x] 提醒 → 卡片（草稿确认卡；列表、取消，带版本号）+ 更多（编辑、历史状态）
- [x] 业务跟进 → 卡片（列表、完成/新进展/不再跟进/延后/提醒、管家草稿确认，带版本）+ 更多（跟进设置）
- [x] 通知 → 概览「未读通知」+ 卡片（最近通知、标为已读）+ 更多（通知中心）

浮层与面板（6 + 2）：

- [x] 通知面板 `notifications-panel` → 卡片（通知卡）
- [x] 工作窗口 `workspace-window` → 卡片（列表卡）+ 更多
- [x] 协作面板 `collaboration-panel` → 卡片 + 更多（文件类动作）
- [x] 共享面板 `shared-panel` → 卡片 + 更多（成员与资料）
- [x] 组织面板 `organization-panel` → 更多
- [x] 连接面板 `connections-panel` → 卡片 + 更多（管理员动作）
- [x] 内嵌：提醒面板 `reminders-panel` → 卡片（列表卡）
- [x] 内嵌：能力面板 `capability-panel` → 更多

管理员页签（业务包、能力配置、审计、用量、用户）→ 管理员（`?admin=1`），不进新页。
