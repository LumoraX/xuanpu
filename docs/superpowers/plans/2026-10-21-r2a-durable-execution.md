# R2-A 持久执行与安全恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use superpowers:subagent-driven-development only when the user explicitly selects delegated execution. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 已受理工作跨进程重建保留执行责任、预算与检查点，仅从可证明安全的阶段继续。

**Architecture:** WorkTask保持业务主记录，新增持久派发/attempt/grant/budget/effect记录；唯一TaskRegistry从数据库认领，TaskRunner按阶段执行。复用原员工事务、单实例保护、会话租约、固定输入和R0审批回执。

**Tech Stack:** Python/Pydantic、PostgreSQL与memory事务、现有ObjectStore、taifeng公开EnginePool/Resume、DockerSandboxProvider。

依据：[R2设计](../specs/2026-10-21-r2-durable-work-design.md)；总顺序见[R2总计划](2026-10-21-r2-delivery.md)。下文保留原实施接口及目标测试示意，实际落点与证据以实施记录和验收记录为准。


## 实施记录（2026-10-21）

A0至A5已实现，当前范围和验收证据见[验收记录](../../guides/releases/2026-10-21-r2a-acceptance.md)。
测试harness按职责拆分：双后端事务/公开冷恢复、受控HTTP、独立PG子进程SIGKILL/SIGTERM与真实模型；
不强制将全部辅助方法集中在同一个fixture。单实例Registry仍为唯一异步执行持有者。
迁移最终为0011/0012，A本期未新增0013。A0-A5因执行、预算、恢复的交叉约束形成一个完整绿色实现提交，
没有按原建议拆出中间红色提交。以下步骤保留设计验收目标，完成状态对应本次已实现的等价机制。

## 文件职责和契约

所有路径相对xuanpu根。新增模块保持单文件≤800行、函数≤80行；拆分按下列责任，不把全部逻辑塞进registry.py。

| 文件 | 责任 |
| --- | --- |
| `packages/xuanpu-server/src/xuanpu_server/work_tasks/execution_models.py` | ExecutionGrant、DispatchRecord、TaskAttempt、Checkpoint、RecoveryFacts/Decision；不含数据库或模型调用 |
| `packages/xuanpu-server/src/xuanpu_server/work_tasks/execution_store.py` | 员工事务内认领、续约、epoch-CAS、阶段写入和持久预算；分解为dispatch_store.py/budget_store.py时保持接口不变 |
| `packages/xuanpu-server/src/xuanpu_server/workspace/postgres_task_execution.py` | PG精确单任务读写，不经现有全员工任务字典重复装载 |
| `packages/xuanpu-server/src/xuanpu_server/work_tasks/recovery.py` | 纯恢复决策、已知资源清理和阶段选择 |
| `packages/xuanpu-server/src/xuanpu_server/work_tasks/worker.py` | 有界公平派发、后台任务所有权与停止；由TaskRegistry持有 |
| `packages/xuanpu-server/src/xuanpu_server/work_tasks/artifact_staging.py` | 私有暂存意图、校验对象、恢复上传及有界回收 |
| `packages/xuanpu-server/src/xuanpu_server/work_tasks/capability_runner.py` | 发布目录中的只读通用能力，明确完成证据，不强制文件成果 |
| `packages/xuanpu-server/src/xuanpu_server/runtime/effect_receipts.py` | 跨传输的效应意图、已验证回执和恢复屏障 |
| `packages/xuanpu-server/src/xuanpu_server/runtime/effect_store.py` | memory/PG效应账本事务实现；不得把参数正文放进管理审计 |
| `packages/xuanpu-server/src/xuanpu_server/runtime/effect_reconciliation.py` | 服务端登记的权威查询适配器；默认无适配器、禁止自动重发 |

公开方法签名（计划）：

```python
from dataclasses import dataclass
from typing import Literal

@dataclass(frozen=True)
class AttemptLease:
    tenant_id: str
    user_id: str
    task_id: str
    attempt_no: int
    owner: str
    epoch: int
    expires_at: float

RecoveryDecision = Literal[
    'keep_terminal', 'cleanup', 'wait_user', 'start', 'recompute',
    'commit_artifacts', 'resume_proven_barrier', 'block_unknown',
    'block_auth', 'block_budget', 'block_resume', 'fail_attempts',
]
```

`claim(who, task_id, *, owner, now) -> AttemptLease | None`原子检查身份、授权、取消、并发和预算；`checkpoint(lease, phase, evidence, *, now)`必须比较owner/epoch；`renew(lease, *, now)`不能复活已过期租约；`settle_usage(lease, reservation_id, usage)`按稳定request_id结算一次。数据库时间作为持久租约时钟，测试注入时钟。

## Task A0：验证稳定内核及建立故障测试harness

Files:
- Create: `packages/xuanpu-server/tests/work_tasks/test_recovery_contract.py`
- Create: `packages/xuanpu-server/tests/work_tasks/recovery_harness.py`
- Modify: `packages/xuanpu-server/tests/work_tasks/conftest.py`
- Read: `packages/xuanpu-server/src/xuanpu_server/runtime/sessions.py`、`runtime/receipt_store.py`、`work_tasks/runner.py`、`work_tasks/registry.py`
- Read: 实际安装taifeng的公开`EnginePool.get_or_create(..., resume_thread_id=...)`、`Resume`及对应恢复ADR；先确认`uv.lock`和导入路径。

- [x] 固定基线SHA、依赖版本/路径、迁移编号；记录R1旧容器清理、partial decision和冷恢复现有测试结果。
- [x] 新增公开契约测试：同一thread冷恢复等待批准、相同审批ID重复送回、工具派发中断、已完成工具结果恢复、未知写入恢复；另加入“先生成scratch→等待批准→SIGKILL→再次批准”，验证文件依赖无法迁移时安全block_resume、pending完整且外部调用次数0。仅调用公开EnginePool和平台会话接口，不依赖泰逢内部事件类。
- [x] 为`recovery_world`fixture实现统一harness：真实双后端store、现有workspace/identity、可注入时钟、确定模型、受控HTTP计数器及阶段断点。方法契约：`accept_office()`、`accept_capability()`、`run_until(phase)`、`crash()`、`reopen()`、`read_task()`、`counts()`、`effect_server.drop_next_reply()`；crash销毁当前平台进程对象而不清空数据库，对SIGKILL用独立子进程。counter从独立HTTP侧车状态读取。run_until接受真实检查点或注入标记（effect_dispatch_intent、artifact_uploaded:2）；到标记时暂停原执行，后续crash/release辅助方法明确控制恢复，不伪造已完成的数据库状态。
- [x] 先用现有R1复现“executing重启只得到interrupted”，保留失败预期；不得把该用例改成只断开浏览器。

Run: `uv run pytest packages/xuanpu-server/tests/work_tasks/test_restart.py packages/xuanpu-server/tests/test_operation_receipts.py packages/xuanpu-server/tests/work_tasks/test_recovery_contract.py -q`。
Expected: 旧R1保护项绿；新恢复行为尚未实现的测试按真实失败记录。若公开内核不支持原批准续接，明确阻塞该分支并单独立内核任务；不得调私有restore方法凑过。

Commit: `test: 固定R2冷恢复与副作用契约`。提交前遵守仓库门禁；未实现的新失败测试可先与A1实现一起形成首个绿色提交，不能以红色主线交接。

## Task A1：增量模型、持久派发和旧记录兼容

Files:
- Create: `packages/xuanpu-server/src/xuanpu_server/work_tasks/execution_models.py`
- Create: `packages/xuanpu-server/src/xuanpu_server/work_tasks/execution_store.py`
- Create: `packages/xuanpu-server/src/xuanpu_server/workspace/postgres_task_execution.py`
- Create: `packages/xuanpu-server/src/xuanpu_server/controlplane/migrations/0011_task_execution.sql`
- Modify: `packages/xuanpu-server/src/xuanpu_server/work_tasks/models.py`、`work_tasks/rules.py`、`workspace/models.py`、`workspace/memory.py`、`workspace/postgres.py`、`controlplane/postgres.py`
- Test: `packages/xuanpu-server/tests/work_tasks/test_execution_store.py`、`test_execution_compatibility.py`

- [x] 增加严格模型：grant记录原actor/角色上限/能力digest/输入范围/expiry/预算；attempt记录lease、阶段、已知容器名/session、usage reservation、错误与结束时间。WorkTask新增grant_id、execution_kind、entry、completion_policy、recovery_policy_version，旧记录默认为office和R1恢复策略。
- [x] 新增dispatch、attempt、grant、budget_reservation表：任务绑定、attempt及任务预算的主外键含tenant/user/task；grant以(tenant,user,grant_id)独立存在，不要求先有task，直接任务或B的definition版本引用它。unique(task,attempt_no)和(reservation_id)防重；所有同grant任务共同消耗grant次数/总预算，每项任务另有更小的预算。dispatch建立due索引，避免扫描任务正文。迁移不改变旧status CHECK，也不改0010。
- [x] 任务受理、grant、dispatch初始行和首条task event同事务；再崩溃也能派发。相同request_id改内容409，不能产生第二grant或attempt。
- [x] 同步现有test_controlplane_migrations.py的latest_version及错误文本期望，并验证从0003/0010升级后新表/约束和旧数据；随后0012/0013分别继续更新，不把migration测试固定在10。
- [x] 旧记录解析、旧waiting_user补读、旧终态不可重启、新字段不泄露对象键/epoch/宿主路径全部加回归。

目标测试：
```python
async def test_accept_is_atomic_and_replay_keeps_one_dispatch(recovery_world):
    task = await recovery_world.accept_office(request_id='same')
    again = await recovery_world.accept_office(request_id='same')
    assert again.task_id == task.task_id
    assert (await recovery_world.counts())['dispatch'] == 1
    assert (await recovery_world.counts())['grants'] == 1
```

Run: `uv run pytest packages/xuanpu-server/tests/work_tasks/test_execution_store.py packages/xuanpu-server/tests/work_tasks/test_execution_compatibility.py packages/xuanpu-server/tests/test_controlplane_migrations.py -q`。
Expected: memory/PG均通过；模拟事务失败后四种新记录及task均没有半提交。迁移测试复用现有test_controlplane_migrations.py，不另建影子实现。

Commit: `feat: 持久保存任务派发授权与执行次数`。

## Task A2：原子认领、失效隔离和跨重试预算

Files:
- Modify: A1的execution_store.py、postgres_task_execution.py
- Create: `packages/xuanpu-server/src/xuanpu_server/work_tasks/budget_store.py`
- Create: `packages/xuanpu-server/src/xuanpu_server/runtime/task_model_budget.py`
- Modify: `packages/xuanpu-server/src/xuanpu_server/config.py`、`quota.py`、`runtime/models.py`、`runtime/assembler.py`、`runtime/events.py`
- Test: `packages/xuanpu-server/tests/work_tasks/test_execution_claims.py`、`test_execution_budget.py`

- [x] 两个并发认领同一任务只得到一个lease；涉及共享额度时先取租户/用量日额度锁，随后员工→任务→dispatch→attempt/预算；其他事务不在持有员工锁时反向申请额度锁，所有写入比较attempt/owner/epoch和未过期状态。
- [x] 保留总8、每员工1、租户更小值；普通会话仍受原配额，后台不绕过。候选最多100行/轮，以owner轮转防止第一员工1000任务饿死其他人；没有名额时不消耗attempt。
- [x] 在runtime装配时给任务注入公开ModelClient协议包装器task_model_budget.py，覆盖子skill和模型内部重试的每次实际stream请求：持久预留tokens，以服务端request_id核销一次，并限制公开ApiRequest.max_output_tokens；输入采用已验收模型/输入类型的保守上界，无法给出上界或网关不兑现限制时阻塞该模型后台执行。所有attempt累计；崩溃usage未知时完整上界预留继续占用，不退款。账本可读且剩余足够下一请求上界才允许继续，否则block_budget；真实usage后续核实再幂等调整。已有UTC租户用量日不改变，任务上限不因跨日重置。
- [x] 改造runtime/events.py任务分支：逐请求账本作为任务/租户/员工用量的幂等结算来源，根轮次subtree_usage不再重复加账；普通非任务会话保持原逻辑。补测子skill、多请求内部重试、根结束重复事件与结算失败重启，禁止借助当前“usage写失败只记日志、不补记”路径释放任务预算。
- [x] 补测usage丢失且剩余额度充足可在保留旧完整预留后继续、剩余不足则block_budget，以及账本不可读时绝不调用模型；不能通过退还未知预留达到“恢复成功”。
- [x] 注入最大次数3、退避30/120秒、恢复窗口1800秒、单次活动执行600秒的配置默认；等待本人/纯上传不占活动运行槽，也不因模型额度0而禁止原生查询、取消、已冻结文件提交。
- [x] 覆盖租约曾过期后续上、旧worker迟到、取消与认领竞态、授权撤销、跨人/租户、数据库断线后的未知reservation。

目标测试：
```python
async def test_old_epoch_cannot_commit_or_dispatch(recovery_world):
    task = await recovery_world.accept_office()
    old = await recovery_world.claim(task.task_id, owner='old')
    await recovery_world.expire_and_cleanup(old)
    new = await recovery_world.claim(task.task_id, owner='new')
    assert new.epoch > old.epoch
    assert await recovery_world.try_checkpoint(old, 'executing') is False
    assert await recovery_world.try_external_dispatch(old) is False
    assert (await recovery_world.counts())['external_writes'] == 0
```

harness新增方法在本Task实现：claim代理真实execution_store；expire_and_cleanup推进注入时钟并通过实际已知资源清理；try_*捕获明确stale_epoch错误而不吞其他异常。

Run: `uv run pytest packages/xuanpu-server/tests/work_tasks/test_execution_claims.py packages/xuanpu-server/tests/work_tasks/test_execution_budget.py packages/xuanpu-server/tests/test_concurrency_and_drain.py -q`。
Expected: 首次与恢复累计预算不超上限、同请求不重复扣费、账本不可读或预留提交结果未核实时阻止新调用；响应usage未知但旧保守预留仍占用时，只在剩余额度足够完整上界的条件下恢复。

Commit: `feat: 以执行租约和持久预算约束后台重试`。

## Task A3：派发前效应意图与权威回执

Files:
- Create: `packages/xuanpu-server/src/xuanpu_server/runtime/effect_receipts.py`、`effect_store.py`、`effect_reconciliation.py`
- Create: `packages/xuanpu-server/src/xuanpu_server/controlplane/migrations/0012_task_effects.sql`
- Modify: `packages/xuanpu-server/src/xuanpu_server/runtime/connectors.py`、`gate_audit.py`、`receipt_watch.py`、`resolutions.py`、`script_executors.py`、`work_tasks/event_mapping.py`
- Test: `packages/xuanpu-server/tests/work_tasks/test_effect_intents.py`、`test_effect_reconciliation.py`

- [x] 效应意图包含稳定logical_action_id、首次call_id/submission、参数摘要、授权/批准引用和下游回执。复用R0批准operation_id，不把它等同下游成功。
- [x] 外部write/external的intent提交成功才调用下游；读调用不被升成写。shell/script默认不能声明外部重试安全，办公隔离脚本仅可重算本attempt文件。
- [x] confirmed_success只来自确定下游回执；confirmed_not_applied须服务端登记适配器权威查询证明。传输异常、用户说明、模型文字、HTTP 5xx不构成未执行证据。
- [x] 故障点放在intent提交前、提交后发送前、发送后回执前、回执提交后，另测intent提交后lease过期而旧网络请求迟到成功：新owner因既有intent不能派发第二次，旧owner不能提交下一步；未知均不能自动重发。稳定ID不能因换session/attempt而改变；无法稳定映射则阻塞整段模型重算。
- [x] 增加本人核实接口的服务层，绑定原task/effect和expected_version，服务端查询验证；不允许修改既有终态，保存核实结论后明确确认新后续任务。原unknown任务及证据保留，后续任务不能重发原逻辑动作。

目标测试：
```python
async def test_remote_success_lost_reply_never_writes_twice(recovery_world):
    recovery_world.effect_server.drop_next_reply()
    task = await recovery_world.accept_capability(effect='external')
    await recovery_world.approve_pending(task.task_id)
    await recovery_world.run_until('effect_dispatch_intent')
    await recovery_world.crash_after_remote_success()
    await recovery_world.reopen()
    await recovery_world.drain()
    assert recovery_world.effect_server.applied_count == 1
    assert (await recovery_world.read_task(task.task_id)).status == 'outcome_unknown'
```

harness在本Task补充approve_pending（经原真实批准API）、crash_after_remote_success（侧车已计数、响应尚未交付）、drain（有界等待），必须读独立侧车计数，不能只数平台日志。

Run: `uv run pytest packages/xuanpu-server/tests/work_tasks/test_effect_intents.py packages/xuanpu-server/tests/work_tasks/test_effect_reconciliation.py packages/xuanpu-server/tests/test_operation_receipts.py -q`。
Expected: 所有崩溃窗口最多一次已确认写；原R0重复批准保护仍绿。没有真实幂等/查询契约的连接器保持人工核实，不补“默认安全”配置。

Commit: `feat: 记录后台任务效应意图并阻止未知写入重发`。

## Task A4：检查点、私有成果暂存和安全恢复

Files:
- Create: `packages/xuanpu-server/src/xuanpu_server/work_tasks/recovery.py`、`artifact_staging.py`
- Modify: `packages/xuanpu-server/src/xuanpu_server/work_tasks/runner.py`、`artifacts.py`、`inputs.py`、`proposals.py`、`runtime/execution_scope.py`、`runtime/sessions.py`、`runtime/assembler.py`
- Test: `packages/xuanpu-server/tests/work_tasks/test_phase_recovery.py`、`test_staged_artifacts.py`、`test_restart.py`

- [x] 每个attempt的新scope包含task_id/attempt_no；重算时session创建request_id也使用task_id/attempt_no，不能沿用当前固定task_id键返回旧session。预登记确定容器名称再启动。新attempt前确认旧容器、迟到启动和旧lease清理结束；不扫描其他应用容器。
- [x] 将runner拆成固定输入、执行、冻结、暂存、上传、候选确认阶段；阶段和task event同事务。执行中安全重算创建新session，明确追加attempt事件，不重发旧UserMessage到旧历史。
- [x] 私有暂存key由服务器生成，放在不共享给executor的对象存储前缀；先持久暂存意图再put，再核对长度/digest。冻结完整检查点包含角色、来源摘要、验证器版本与全部已验对象；未形成完整检查点不能从半份manifest宣告成功。
- [x] 已冻结恢复只读暂存对象，沿用`artifact:<task_id>:<index>`幂等上传；缺失的继续上传，已提交的核对回执/对象，最后一次事务提交全部成果及候选。旧R1artifact intent仍只补已有索引，不能获得未提供的字节。
- [x] 暂存回收只依据服务器意图账本中的明确key，终态且无恢复/读取租约、超过24小时后才删；Local和S3都不靠全桶扫描猜归属，未知put结果保留核查责任。
- [x] pending批准仅走A0证明的公开冷恢复和原request_id；原挂起续接保留session/thread/call_id作为例外，以新attempt/epoch显式重新绑定执行所有权，旧owner事件无法提交。只允许服务端登记且经测试无文件依赖/无进程内状态的HTTP工具；文件、脚本、stdio/MCP及scratch相关pending本期block_resume，保留原pending和证据。不得空scope继续，不复用未冻结旧workspace；有派发屏障时不整轮重算。未知写入、资源未核实、失效授权各有独立block_reason。

目标测试：
```python
async def test_crash_after_second_upload_uses_no_new_model(recovery_world):
    task = await recovery_world.accept_office(output_kind='table_summary')
    await recovery_world.run_until('artifact_uploaded:2')
    before = recovery_world.model_calls
    await recovery_world.crash()
    await recovery_world.reopen()
    await recovery_world.drain()
    restored = await recovery_world.read_task(task.task_id)
    assert restored.status == 'succeeded'
    assert len(restored.artifacts) == 4
    assert recovery_world.model_calls == before
    assert (await recovery_world.counts())['artifact_files'] == 4
```

再覆盖：纯参数HTTP pending冷恢复沿用原session和批准ID、仅调用1次；scratch依赖pending恢复安全阻塞且SHA证据仍保留供核实；冻结前崩溃→新attempt；暂存对象缺失/篡改→拒绝成功；待办事务提交后崩溃→回放同一回执；模型不可用时仍能恢复冻结成果并确认原生记录。

Run: `uv run pytest packages/xuanpu-server/tests/work_tasks/test_phase_recovery.py packages/xuanpu-server/tests/work_tasks/test_staged_artifacts.py packages/xuanpu-server/tests/work_tasks/test_artifact_recovery.py packages/xuanpu-server/tests/work_tasks/test_restart.py -q`。
Expected: 对计划支持的阶段实际推进；不支持的原内核续接明确阻塞；原R1保守旧记录用例通过。

Commit: `feat: 从已核验阶段恢复任务与成果写回`。

## Task A5：唯一调度生命周期及通用只读能力

Files:
- Create: `packages/xuanpu-server/src/xuanpu_server/work_tasks/worker.py`、`capability_runner.py`
- Modify: `packages/xuanpu-server/src/xuanpu_server/work_tasks/registry.py`、`service.py`、`routing.py`、`rules.py`、`models.py`、`platform.py`、`api/work_tasks.py`、`identity/listeners.py`
- Test: `packages/xuanpu-server/tests/work_tasks/test_durable_worker.py`、`test_capability_tasks.py`、`test_recovery_identity.py`
- Docs: `docs/architecture/work-tasks.md`、`docs/architecture/http-api.md`、`docs/guides/work-tasks.md`

- [x] Registry持有唯一worker；start只唤醒持久派发，不另建竞争的asyncio执行者。open先持有单实例guard、清理旧资源、分类恢复，再开始认领；close停新受理/触发、结清执行并保留可恢复检查点。
- [x] 正常关闭时，R2可恢复任务完成取消/资源清理后持久dispatch=paused_shutdown，保留非终态业务status和检查点，启动后由worker分类恢复；不得沿用当前_close_owned把它先写成interrupted，再破坏终态不变式重跑。用户cancel/真实失败仍终态；R1策略任务仍保守interrupted。补优雅SIGTERM重建与SIGKILL两组，不把关页当关闭。
- [x] 开关默认false；新记录recovery_policy_version=2才自动恢复。旧R1旧owner运行按旧策略清理并interrupted，旧终态不重放。
- [x] 新增`POST /v1/work-tasks/from-capability`，严格字段request_id、服务端目录项引用、text、可选明确input_files/matter/timezone。仅选择租户真实发布且员工可用的入口；服务端冻结package/version/digest/entry与grant，不接受宿主路径或任意脚本。
- [x] 通用只读任务可无附件，output_kind增加capability_result；WorkTaskRecord增加本人result_summary（最多16000字符）和result_receipt（原submission及最多256个工具回执引用）。advance按服务端冻结的completion_policy校验：office仍要求文件与来源，capability_receipt要求已保存并验证的轮次/工具回执、无未知副作用且未取消，不用伪造空文件满足旧成功规则。不注册动态进程内业务代码，不把模型总结当作业务已执行。
- [x] 再次核对代员工key吊销、员工停用、租户暂停、角色降低、连接授权撤销、文件删除和能力版本不可用。登录cookie到期本身不撤销已经明确交办的授权。
- [x] 停用回调撤销grant、停止派发、取消在途任务并清理；不能对停用身份开放内容读取。失败收尾继续保留原资源责任。

目标测试：
```python
async def test_revoked_origin_key_stops_recovery(recovery_world):
    task = await recovery_world.accept_office(origin='act_as_key')
    await recovery_world.run_until('inputs_pinned')
    await recovery_world.revoke_origin_key()
    await recovery_world.crash()
    await recovery_world.reopen()
    assert (await recovery_world.read_dispatch(task.task_id)).block_reason == 'grant_revoked'
    assert (await recovery_world.counts())['live_containers'] == 0
```

本Task为harness补充origin、revoke_origin_key和read_dispatch，分别调用真实key服务和执行存储，禁止改内存Principal伪造吊销。

Run: `uv run pytest packages/xuanpu-server/tests/work_tasks packages/xuanpu-server/tests/identity -q`，再跑`uv run pytest`、`uv run ruff check --select F,S108,I,TC packages`、`uv run mypy packages/*/src`。
Expected: 双后端安全恢复、旧接口与R0/R1回归全部绿；只读工单实际工具调用与无附件业务任务不被office校验器误拒。

Commit: `feat: 统一后台派发与已授权能力执行`。

## A独立验收

- [x] 真实PG持久化；Linux Docker中在executing SIGKILL，确认旧容器退出后新attempt执行，输入digest不变。
- [x] CSV/XLSX固定输入恢复得到独立核对的15.50；纪要待确认跨重启后仅创建本人所选记录。
- [x] 已冻结四成果部分上传后重建，不增加模型调用，文件数和digest正确。
- [x] 受控HTTP侧车“写成功、回执丢失”计数仍为1，unknown不重发；实际运行中HTTP取消后无遗留容器。
- [x] 每个新attempt实际扣入同任务预算；测试用量未知时不退款重跑，身份失效时不执行。
- [x] A验收记录明确只有立即交办的持久恢复；时间、事件和完整工作动态尚未完成，不标记整体R2完成。
