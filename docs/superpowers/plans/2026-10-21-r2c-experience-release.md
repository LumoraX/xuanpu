# R2-C 工作动态、员工入口与生产验收 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use superpowers:subagent-driven-development only when the user explicitly selects delegated execution. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 员工能确认后台安排、理解恢复状态、处理等待项，并以可复核的故障与生产证据交付整个R2。

**Architecture:** 复用WorkTask事件与现有通知，新增本人执行安排入口及安全展示字段。控制台、对话、SDK和CLI调用同一服务；验收分别证明源码、数据、执行、模型、浏览器和部署。

**Tech Stack:** FastAPI、原生JavaScript/CSS、Python SDK/CLI、现有pytest/Node行为测试、真实PG/对象存储/Linux Docker、既有镜像与部署脚本。

前置：[R2-A](2026-10-21-r2a-durable-execution.md)和[R2-B](2026-10-21-r2b-time-event-triggers.md)。设计见[R2设计](../specs/2026-10-21-r2-durable-work-design.md)。原规划阶段不直接部署；用户随后明确授权“继续 R2-C，按计划推进。直到全部完成”。2026-10-22 已执行功能、部署、收尾，并在用户随后明确授权后完成共享宿主整机重启验收，既定强制验收全部签收；实测见 [R2验收记录](../../guides/releases/2026-10-21-r2-acceptance.md)。

## Task C0：本人状态视图、通知、SDK与CLI

Files:
- Create: `packages/xuanpu-server/src/xuanpu_server/work_tasks/views.py`、`activity.py`
- Modify: `packages/xuanpu-server/src/xuanpu_server/api/work_tasks.py`、`api/task_definitions.py`、`work_tasks/store.py`、`workspace/notifications.py`、`reminders/service.py`
- Modify: `packages/xuanpu-sdk-py/src/xuanpu_sdk/client.py`、`models.py`、`packages/xuanpu-cli/src/xuanpu_cli/main.py`
- Test: `packages/xuanpu-server/tests/work_tasks/test_activity.py`、`test_execution_views.py`、`packages/xuanpu-sdk-py/tests/test_sdk.py`、`packages/xuanpu-cli/tests/test_cli.py`

- [x] WorkTask展示新增execution_stage、attempt_no、next_run_at、block_reason、recovering、source_occurrence引用；原八status不改含义。集中序列化排除process_owner、epoch、宿主路径、暂存对象key、原始工具参数和凭据。
- [x] 扩展原`GET /v1/work-tasks/{id}/events`呈现阶段、恢复和结果，按本人持久游标分页；恢复时不能重置游标或丢原R1事件。不建重复聊天历史。
- [x] 在员工事务内以`task_id:semantic_transition_id`生成一条通知，只推等待决定、最终成果、最终失败、预算/授权阻塞及未知结果。轮询、重复outbox、相同重放和每次短退避不刷通知。
- [x] SDK增加create/get/list/update/pause/resume/cancel_task_definition、list_occurrences、create_capability_task、reconcile_task_effect；请求保持strict schema和本人身份，自动网络重试仍用同一request_id。
- [x] CLI增加`work-tasks`和`task-definitions`对应操作，默认只显示本人摘要；不允许命令行传任意tenant/user/host path覆盖身份，不把密钥写进命令输出。
- [x] 无员工机器凭据403，其他成员/管理员以自己的员工身份404；统计只给数量/状态，不给私有目标和文件名；model不可用时查询、暂停、取消、下载、本人原生确认都可用。

目标测试：
```python
async def test_repeated_recovery_creates_one_meaningful_notification(recovery_world):
    task = await recovery_world.accept_office()
    await recovery_world.run_until('inputs_pinned')
    await recovery_world.crash()
    await recovery_world.reopen()
    await recovery_world.drain()
    first = await recovery_world.notifications(task.task_id)
    await recovery_world.reconcile_again()
    assert await recovery_world.notifications(task.task_id) == first
    assert sum(n.kind == 'work_task_completed' for n in first) == 1
```

harness本Task新增notifications（读真实本人通知队列）、reconcile_again（驱动同一worker补读），不通过手工覆盖通知列表伪造幂等。

Run: `uv run pytest packages/xuanpu-server/tests/work_tasks/test_activity.py packages/xuanpu-server/tests/work_tasks/test_execution_views.py packages/xuanpu-sdk-py/tests packages/xuanpu-cli/tests/test_cli.py -q`。
Expected: 本人字段完整、跨人不可见、无重复通知、SDK原R1六接口回归通过。

Commit: `feat: 展示后台任务动态并开放统一客户端接口`。

## Task C1：对话确认、直接入口与工作卡

Files:
- Create: `packages/xuanpu-server/src/xuanpu_server/assistant_task_definitions.py`
- Create: `packages/xuanpu-console/src/xuanpu_console/static/task-definitions.js`
- Modify: `packages/xuanpu-server/src/xuanpu_server/assistant.py`、`assistant_workspace.py`
- Modify: `packages/xuanpu-console/src/xuanpu_console/static/work-tasks.js`、`workspace.js`、`workspace-chat.js`、`index.html`、`style.css`
- Test: `packages/xuanpu-server/tests/task_triggers/test_assistant_definitions.py`、`packages/xuanpu-console/tests/task_definition_behavior.cjs`、`test_task_definition_behavior.py`、`work_task_behavior.cjs`

- [x] 对话意图分清“到时提醒我”与“到时替我执行”；缺材料、时间、能力或授权时只澄清。模型只能产草稿，服务端重新解析真实目录、时区和输入，确认按钮才创建definition。
- [x] 确认卡展示目标、首次/重复时间、时区、明确文件范围/版本策略、错过与重叠行为、最大次数与预算、失效条件；普通员工看自然名称，不展示内部package、epoch或幂等键。
- [x] 独立“执行安排”入口直接创建、查看、暂停、恢复、修改、取消同一记录。主对话仍为默认入口，不把模块配置表作为员工开始工作的前提。
- [x] 工作卡新增排队、正在恢复、第N次执行、等待批准/核实、预算不足；安全重算显示“重新执行”，已冻结阶段显示“继续保存成果”，不能都叫无损续跑。
- [x] 结果未知仅提供核实指引和服务端验证的回执查询，不提供绕过屏障的通用重试按钮；失败可安全重新执行的任务显示服务端决定的可用动作。
- [x] 身份切换丢弃迟到响应；保存/暂停/取消断网后用原request_id重试，不改成新请求；旧state_version返回409则刷新，不重复创建。
- [x] 默认通知入口统一；窄屏和键盘焦点可操作，标签与状态不只靠颜色。R2关闭时仍展示旧任务及必要直接操作，不出现不可兑现的安排承诺。

Node行为测试增加以下目标断言（沿用现有VM/DOM harness实际调用控制台脚本）：
```javascript
assert.equal(apiCalls.filter(call => call.path === '/v1/task-definitions').length, 0);
await clickConfirmSchedule();
assert.equal(apiCalls.filter(call => call.path === '/v1/task-definitions').length, 1);
assert.equal(await displayedTaskState('outcome_unknown'), '结果待核实');
assert.equal(hasUnsafeRetryButton(), false);
```

在新task_definition_behavior.cjs中定义clickConfirmSchedule、displayedTaskState、hasUnsafeRetryButton为真实DOM查询和点击包装，不用字符串包含代替行为。assert和apiCalls沿用现有Node行为测试注入方式。

Run: `uv run pytest packages/xuanpu-console/tests packages/xuanpu-server/tests/task_triggers/test_assistant_definitions.py packages/xuanpu-server/tests/identity/test_reminder_routing.py -q`。
Expected: 未点击确认无副作用，提醒意图不变执行授权，断网重试不重复，旧主对话与通知行为通过。

Commit: `feat: 从对话确认后台执行并展示恢复进展`。

## Task C2：跨层故障矩阵与验收工具

Files:
- Create: `packages/xuanpu-server/tests/work_tasks/test_r2_fault_matrix.py`
- Create: `packages/xuanpu-server/tests/task_triggers/test_r2_trigger_faults.py`
- Create: `scripts/acceptance/r2_durable.py`、`scripts/acceptance/r2_effect_server.py`
- Modify: `scripts/acceptance/r1_office.py`（只抽取可复用输入和独立下载核验，不覆盖原证据格式）
- Create: `docs/guides/releases/2026-10-21-r2-acceptance.md`（实施时写真实结果；未跑项保留not_checked）

- [x] 将下表作为参数化矩阵，每项保存输入、精确故障点、task/attempt、实际阶段、所有业务/对象/下游计数、SHA、版本和失败细节。测试中fake clock只控制时间，不替代PG事务、Linux进程或真实HTTP下游。
- [x] 验收CLI要求显式`--credentials`、`--evidence`、`--mode staging|production`；凭据只读私有文件，不写入成果。每次重试新证据目录，初轮模型失败和未通过案例不覆盖。
- [x] effect_server只绑定隔离测试网络，提供真实持久计数、按稳定键去重、暂停响应、丢弃回执和权威查询；写操作仅合成数据，不发送真实邮件/消息/财务请求。
- [x] 自动矩阵通过后，用真实模型重复CSV/XLSX/会议与无附件工单只读任务。分别核对语义结果、生命周期和外部计数；真实模型轮次成功不能替代业务验收。
- [x] 浏览器通过CUA验证真实待确认/恢复卡并保存桌面与窄屏截图；直接HTTPS API、模拟登录身份、真实企业登录分开标注，不能因页面“飞书”字样宣称真实登录通过。

| 故障点 | 实际验证与预期 |
| --- | --- |
| accepted提交后、派发前SIGKILL | 重启同task受理，只有1次持久实例 |
| Docker创建响应丢失 | 明确名称清理成功前不新建，旧/新任务容器不会并存 |
| 模型/办公脚本运行中SIGKILL | 固定输入、新attempt、新scope，旧进程已退出，预算累计 |
| 成果冻结后SIGKILL | 不新增模型调用，暂存对象完整才继续上传 |
| 4成果第2份提交后SIGKILL | 最终恰好4成果，逐份下载SHA一致 |
| 原生行动项事务提交后断线 | 相同决定回放，todo/event/inbox reminder各1，无重复 |
| write成功但回执未提交 | 下游实际写计数1，任务unknown，无自动重发 |
| 正等待工具批准重启 | 原session/pending/operation保留；无文件依赖的登记HTTP批准只消费一次；scratch依赖或未证实可恢复的pending明确block_resume，不能在空scope继续 |
| 运行中HTTP取消/员工停用/租户暂停 | 执行容器和活动session归零，未知外部结果不伪装撤销 |
| 预算预留后用量未知 | 旧完整上界预留不退；剩余足够新上界才继续，否则block_budget；核实后与真实usage幂等对账 |
| 次数耗尽/模型持续不可用 | 有界停止，原生查询/确认/取消仍可用，无忙循环 |
| 时间触发停机和DST | missed/coalesce/fold/gap与确认策略一致，不无限补跑 |
| outbox写入回滚/投递后崩溃/乱序提交 | 源回滚不触发，重复只1任务，较小晚提交event不丢 |
| definition编辑/撤销/循环触发 | 旧版未开始任务不执行，可信链路限额有效 |
| R1旧记录升级 | 不追溯自动重跑，旧waiting_user/成果补读完整 |
| daemon重启/平台重建/同宿主重启 | socket重新连接、旧资源先核实、持久阶段可恢复，无更高权限 |

验收脚本计划用法：
```bash
uv run python scripts/acceptance/r2_durable.py --help
uv run pytest packages/xuanpu-server/tests/work_tasks/test_r2_fault_matrix.py packages/xuanpu-server/tests/task_triggers/test_r2_trigger_faults.py -q
```

真实脚本参数使用操作者私有路径传入；命令行不得出现API key和企业材料正文。先运行staging，production模式只允许显式隔离验收租户，脚本不得自动改全局模型或暂停正式租户。

Commit: `test: 覆盖持久执行和触发的真实故障窗口`。

## Task C3：构建、灰度、回退与生产验收

Files:
- Modify: `packages/xuanpu-server/pyproject.toml`、`packages/xuanpu-console/pyproject.toml`、`packages/xuanpu-sdk-py/pyproject.toml`、`packages/xuanpu-cli/pyproject.toml`、`uv.lock`（按实际变化升各包版本）
- Modify: `docker/compose.office.yml`、`docker/office_probe.py`（只在新能力确需部署探针时；不放宽隔离）
- Docs: `docs/architecture/work-tasks.md`、`docs/architecture/http-api.md`、`docs/architecture/deployment.md`、`docs/guides/work-tasks.md`、`docs/guides/docker-deployment.md`、`docs/guides/operations.md`、`docs/guides/releases/2026-10-21-r2-acceptance.md`

### 候选构建与预发布

- [x] 所有A/B/C工作包完成并独立复核；按实际发布日期升CalVer，更新锁定与真实模型ledger（若内核有变更）。再次核对迁移连续编号及旧R1对象保留。
- [x] 跑下列完整门禁并保留实际输出；不能沿用R1的1842数当作R2通过数。

```bash
uv sync --all-packages --all-extras
uv run pytest
uv run ruff check --select F,S108,I,TC docker packages
uv run mypy packages/*/src
uv lock --check
git diff --check
OUTPUT_MODE=load docker/deploy.sh
```

- [x] 复用白名单构建脚本，记录镜像digest、lock、全部wheel版本、实际安装源码SHA与Linux/amd64；不把整个源码目录或配置发给Docker构建上下文。
- [x] 在独立预发布库运行0011–0014迁移及实际PG/Linux故障矩阵；分别记录daemon重启和平台重建、源码与成果摘要。
- [x] 用户授权后实际重启共享Linux宿主一次；新boot_id、两个平台自动启动、六处idmapped、原批准续接、固定输入调度和下游计数均通过。预发布和生产在同一宿主，本次同时验证，不能计作两次独立重启。
- [x] 三开关默认false的兼容部署先通过：能读新旧格式，原R1/提醒/主动跟进/工单查询功能不退化；无员工身份的后台调用仍拒绝。

### 正式灰度与验收

- [x] 在有部署授权后进入此段。停新受理/触发、等待执行收尾和lease归零，停写备份PG、全部data、配置、Compose、rootless配置和旧完整镜像，逐项校验摘要及离线归档实际层。
- [x] 发布兼容读取的R2镜像，应用增量迁移，不重初始化企业、密钥和对象存储；保持平台UID10001、独立executor UID10002、Unix socket目录、idmapped共享和既有资源限制。
- [x] 先只给合成验收租户开启durable，再开启triggers/activity，跑真实模型、受控副作用和浏览器链路；原有各租户数量/数据摘要在合成验收写入前对比，验收新增单独记账。
- [x] 已实际验证独立daemon重启和平台重建，两环境隔离探针、真实批准续接和重启后新模型任务通过。
- [x] 生产同宿主整机重启已实际执行；22个原运行容器自动恢复，生产219源码和8版本复核一致，旧记录及117个原对象SHA保持，真实隔离探针通过。
- [x] 正式租户先开放立即交办恢复，再由员工明确确认执行安排；旧R1终态与旧提醒不自动改成可执行工作。保持写入逐项批准和unknown屏障。
- [x] 连续观察两个实际调度周期及一个主动扫描周期，核对队列等待、最老可运行任务、错误、预算、未知效应、资源残留和通知；本轮为单个合成员工，多员工公平性由自动测试覆盖，生产负载公平性not_checked。
- [x] 回收合成定义/未来触发、用户、key和登录会话，取消提醒并暂停验收租户；末位管理员保护不绕过，必要时保留管理员但暂停整个租户。关闭测试侧车与浏览器代理，保留原始证据。

### 前向回退

- [x] 演练关闭triggers和新认领→记录并结清在途资源→保持R2兼容reader运行、查询/取消可用的前向回退；已受理任务清楚显示暂停/等待状态。
- [x] 不删新表、不抹去effect/usage/occurrence账本，不把outcome_unknown改成failed后重发。不能直接把新JSON交给旧R1严格解析器。
- [x] 只有确认无新正式写入、显式维护停写且已核实外部效应时才考虑数据库/文件快照恢复；镜像切回不证明外部业务可回滚。未实际演练快照恢复则如实标注。
- [x] 源码主线、各包/镜像、迁移、真实模型/副作用、浏览器、部署、前向回退及共享宿主整机重启分别留证，R2整体既定强制验收完成。真实企业OAuth、真实外部通知、生产多员工压测和从快照恢复到新库后的独立服务启动继续保持独立未验边界。

Commit: `feat: 发布并验收R2持久后台工作`，随后独立提交脱敏发布证据；只有实际执行过的步骤可以勾选。
