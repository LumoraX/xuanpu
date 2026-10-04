# 玄圃持久提醒与通知中心 Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development, independent spec/quality review and TDD. User has authorized implementation and local deployment; no repeated approval gate.

**Goal:** 员工直接说提醒需求，经清楚的时间确认后持久保存，到点由玄圃生成通知，浏览器离线和服务重启不影响任务。

**Architecture:** 现有Postgres控制面增加提醒、通知与渠道投递表；后台只完成短事务状态转换，网络投递走独立租约outbox。网页使用有鉴权、补读和重连的SSE。外部平台是可替换sender。

**Tech Stack:** Python/asyncio、Postgres/asyncpg、FastAPI/httpx、zoneinfo、原生JS。

## 文件与职责

- `packages/xuanpu-server/src/xuanpu_server/reminders/{models,store,memory,postgres,service,worker}.py`：提醒输入/记录、存储协议、两后端、员工操作/后台状态转换。
- `controlplane/migrations/0006_reminders.sql`：随现有迁移部署；旧迁移不变。
- `controlplane/{store,memory,postgres}.py`：公开 reminder_store 同后端子存储。
- `api/reminders.py`：提醒CRUD、通知列表/标读、SSE。
- `reminders/feishu.py`：唯一飞书发送适配，应用凭据来自保管库、目标来自现有用户外部身份映射。
- `platform.py`：装配与start/close/停用监听；`api/app.py`：挂载。
- `assistant.py`、`api/assistant.py`：内置提醒识别与有界草稿输出，仍计量且可取消。
- `static/reminders.js`：提醒确认/管理/通知/SSE；`app.js/identity.js/index.html/style.css`：入口与身份生命周期。
- `tests/reminders/`、console行为测试、`tests/e2e/`：契约、安全与真实浏览器。
- 新ADR修订旧外部来源唯一权威与飞书必需；当前架构/API/运维同步。

## Task 1：持久化与调度（独立实现者）

- [x] 红测试：同request_id重试返回同提醒，改内容409；跨人/租户不可读；过期version修改409。
- [x] 先定义并实现ReminderInput、ReminderRecord、NotificationRecord与ReminderStore协议，冻结接口给集成方。
- [x] 两后端通过同契约：数据库唯一(reminder_id,due_at)，到期通知插入与下一次时间/完成状态同事务。
- [x] due扫描只产生候选，真正提交时重新验证租户、用户、提醒状态和version，取消竞态不能发旧版本。
- [x] 每位用户通知提交顺序必须与游标顺序一致：Postgres发通知事务先锁定该用户行，再锁提醒并分配通知ID；不可用可能乱序提交的裸bigserial直接漏读。
- [x] 一次性补发/重复合并、每日每周时区与DST策略单测；失败下一轮继续，后台可取消。
- [x] API归属、CSRF、错误码、SSE重鉴权及游标补读；发送任务租约/claim_token、5次有界重试。

数据契约示意（实现文件为最终类型来源）：
```python
class ReminderInput(BaseModel):
    title: str  # 1..500 nonblank
    due_at: float  # finite UTC epoch, future, <= 5 years
    timezone: str  # valid IANA
    repeat: Literal['once', 'daily', 'weekly'] = 'once'
    channels: list[Literal['inbox', 'feishu']] = ['inbox']
# ReminderService.create(who, body, request_id) -> ReminderRecord
# list(who) -> list[ReminderRecord]
# update(who, reminder_id, body, version) -> ReminderRecord
# cancel(who, reminder_id, version) -> ReminderRecord
# notifications(who, after_id=0, limit=100) -> list[NotificationRecord]
# read(who, notification_id) -> NotificationRecord
# start() / await close() / await run_once()
```

验证：`uv run pytest -q packages/xuanpu-server/tests/reminders`。必须包括真实Postgres，缺少数据库不能声称持久化验收。

## Task 2：自然语言、UI与渠道集成（根）

- [x] 红测试：无业务包仍能识别提醒；时间不明确澄清；模型不能通过字段指定别人或直接执行。
- [x] RouteOutcome增加reminder_draft；模型看到服务端当前时间和有效时区；输出校验并转成ReminderInput。
- [x] 对话预览卡片展示内容/绝对时间/时区/重复/实际渠道，确认后POST；同一草稿失败重试request_id不变。
- [x] 提醒列表可修改与取消，通知可读；DOM textContent；身份切换取消流、清草稿/游标/列表。
- [x] SSE fetch流带现有鉴权，不把token放URL；重连前列表补读；关闭网页不影响后台。
- [x] 飞书sender用已有应用配置/保管库/身份映射；有界请求、稳定uuid与分类失败；仅用户选择的提醒才投递飞书，不对真实员工发测试消息。
- [x] 浏览器红绿：预览不创建→确认→到期通知→刷新恢复；编辑取消与窄屏、用户隔离。

交互期望：
```text
用户：明天早上8点提醒我准备标注数据复盘
助手卡片：2026-10-06 08:00 / Asia/Shanghai / 一次 / 玄圃通知中心
确认后：提醒已创建；到点站内通知仍在，网页关闭时不会承诺系统弹窗。
```

## Task 3：审查、文档与交付（根+独立审查）

- [x] 独立规格审查→安全与一致性代码审查→修复回归。
- [x] 新ADR、HTTP与架构、管理员运维手册说明调度/部署/补发/失败处理/替换渠道，完整产品路线图分领域记录。
- [x] `uv run pytest -q`、`uv run ruff check --select F,S108,I,TC packages`、`uv run mypy packages/*/src`、`uv lock --check`、构建。
- [ ] 浏览器验收，真实gpt-5.6-luna时间解析，Postgres多实例与停机到期恢复，不向真实员工发测试消息。
- [ ] 合并主线，沿用私有JSON本地迁移重启，验证旧会话/新提醒持久与主线运行路径；记录已完成与未实现领域，不称整个ERP已完成。


## 2026-10-05 合并前验收

- 独立后端审查：原六项及暂停/发送窗口、慢发送阻塞扫描两个组合问题已修；无剩余P1/P2。独立提醒与发送器专项72 passed、1 skipped。
- `uv run pytest -q`：1289 passed、1 skipped，136.04秒。唯一skip是Postgres专属锁序用例的内存参数，真实Postgres参数通过。
- `uv run ruff check --select F,S108,I,TC packages scripts tests/e2e`：All checks passed。
- `uv run mypy packages/*/src`：105个源文件通过；`uv lock --check`及`git diff --check`通过。
- `uv build --all-packages --out-dir /tmp/xuanpu-reminders-release-dist`：五个包的wheel/sdist构建通过；确认server wheel包含0006迁移、console wheel包含提醒脚本。server=2026.10.5.3、console=2026.10.5.5。
- 浏览器：真实CLI+模拟飞书/模型，通过预览不落库、确认、到期SSE、刷新恢复、标读、编辑取消、按状态查看、账号隔离和375px无横向溢出；输出`/tmp/xuanpu-reminders-browser-release/`，测试服务已停止。发送模拟不代表真实飞书发送验收。
- 真实已配置gpt-5.6-luna：明早8点/每周一9点得到明确草稿，缺时间追问；结果`/tmp/xuanpu-reminder-real-model-result.json`。未为真实员工创建提醒或发送测试消息。
- 全量日志`/tmp/xuanpu-reminders-release-tests.log`；构建、门禁和浏览器日志同前缀。临时目录可能清理，不提交其中配置/凭据。
- 合并前主线旧会话抽查：历史14条。以下本地迁移和停机到期验收完成后单独补录。
