# R2-B 时间与事件触发 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use superpowers:subagent-driven-development only when the user explicitly selects delegated execution. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 本人确认执行安排后，每个时间点或可信业务事件产生且只产生一个持久工作实例。

**Architecture:** TaskDefinition保存模板和授权，Occurrence保存一次触发；与WorkTask受理同员工事务提交。日历复用现有提醒算法，事件使用事务outbox逐条结算，最终只唤醒A的唯一持久worker。

**Tech Stack:** PostgreSQL/现有memory事务、zoneinfo、现有reminders.schedule、FastAPI、A的grant/dispatch/budget。

前置：[R2-A](2026-10-21-r2a-durable-execution.md)完成。依据：[R2设计](../specs/2026-10-21-r2-durable-work-design.md)。本文件保留原实施步骤；2026-10-21已按B0–B3实现，源码和本地验收已完成，未部署生产；证据见[R2-B验收](../../guides/releases/2026-10-21-r2b-acceptance.md)。

## 结构与数据契约

新增目录`packages/xuanpu-server/src/xuanpu_server/task_triggers/`：

| 文件 | 责任 |
| --- | --- |
| `models.py` | TaskDefinition、Occurrence、DomainEvent、严格创建/修改请求 |
| `service.py` | 本人确认、版本化修改、暂停/恢复、取消与授权撤销 |
| `store.py` | memory与PG共同接口，员工事务内生成occurrence/WorkTask |
| `postgres.py` | 定义、到期索引、occurrence唯一约束和逐行outbox投递 |
| `calendar.py` | 纯日历计算、错过/重叠策略，复用既有日期规则 |
| `events.py` | 可信事件类型、来源验证和环路限额 |
| `outbox.py` | 同业务事务写出领域事件，不含事件正文或凭据 |
| `worker.py` | 生命周期持有的时间/事件扫描器；只产生持久WorkTask，不调用模型 |

Definition字段：definition_id、tenant/user、version、title、enabled、template（office或capability）、grant_id、trigger、next_fire_at、misfire_policy、overlap_policy、created_at/updated_at。输入策略为`fixed_revision`或本人明确确认的`latest_at_occurrence`，后者仅限授权中枚举的文件ID，不能由事件扩成整文件夹或整个空间。

Occurrence字段：occurrence_id、definition_id/version、occurrence_key、scheduled_at、source_event_id、固定输入引用、status（pending/accepted/missed/skipped_overlap/blocked/cancelled）、task_id、reason_code、next_eligibility_check_at、created_at。每个定义版本与occurrence_key唯一，task_id关联同一员工。

DomainEvent字段：event_id、tenant/user、event_type、subject_id、subject_version、occurred_at、trusted_origin_chain、输入精确revision引用。event_id只由服务器生成；事件payload≤8KiB，不保存材料正文。Delivery按(event_id,definition_id,definition_version)唯一，保留pending/claimed/settled/blocked及租约，不能以全局最大event_id批量跳过其他行。

## Task B0：执行安排、持久授权和版本化管理

Files:
- Create: `packages/xuanpu-server/src/xuanpu_server/task_triggers/__init__.py`、`models.py`、`service.py`、`store.py`、`postgres.py`
- Create: `packages/xuanpu-server/src/xuanpu_server/controlplane/migrations/0013_task_triggers.sql`
- Create: `packages/xuanpu-server/src/xuanpu_server/api/task_definitions.py`
- Modify: `packages/xuanpu-server/src/xuanpu_server/api/app.py`、`platform.py`、`identity/listeners.py`
- Test: `packages/xuanpu-server/tests/task_triggers/conftest.py`、`test_definitions.py`、`test_authorization.py`

- [x] 创建严格模型及definition/occurrence/domain_event/delivery表；全部FK包含tenant/user，禁止跨人task、输入及grant关联。
- [x] 新接口前缀`/v1/task-definitions`：POST确认创建，GET本人列表与详情，PUT按version修改，POST `/{id}/pause`、`/{id}/resume`、`/{id}/cancel`，GET `/{id}/occurrences`分页。写请求均带request_id及expected_version（新建无expected_version）；同请求同内容回放，改内容409。
- [x] definition和其独立grant在同员工事务创建，grant不需要伪造task占位；每次occurrence生成的WorkTask引用该定义版本grant，消耗同一总次数/预算并叠加本任务上限，不能每轮复制预算。修改definition创建新确认grant并撤销旧版未开始授权，已有在途按原明确策略收尾。
- [x] 创建前服务端解析真实能力与输入策略，显示并冻结授权范围、时区、错过策略、最大执行次数/预算、有效期；没有明确确认不保存enabled定义。
- [x] 暂停阻止新认领，已运行实例是否停止由`stop_running`明确布尔参数决定，默认false；取消定义则取消所有未开始实例并对在途提出停止，不声称撤销下游操作。
- [x] 修改升version并撤销旧版未开始occurrence；恢复不追赶暂停期间所有历史时刻，按原显式misfire策略处理一次；员工停用、租户暂停、原代员工key吊销均撤销未开始授权。
- [x] fixtures复用A的recovery_world，新增`trigger_world`包装真实定义服务、同一个worker/store、fake clock及现有用户/key；不创建第二任务库。

目标测试：
```python
async def test_definition_change_cannot_run_old_version(trigger_world):
    definition = await trigger_world.confirm_daily(hour=9, minute=0)
    old = await trigger_world.materialize_due(definition)
    changed = await trigger_world.change(definition, hour=10)
    await trigger_world.dispatch(old.occurrence_id)
    assert changed.version == definition.version + 1
    assert (await trigger_world.occurrence(old.occurrence_id)).status == 'cancelled'
    assert (await trigger_world.counts())['model_calls'] == 0
```

Run: `uv run pytest packages/xuanpu-server/tests/task_triggers/test_definitions.py packages/xuanpu-server/tests/task_triggers/test_recovery_authorization.py packages/xuanpu-server/tests/test_controlplane_migrations.py -q`。
Expected: memory/PG一致、跨人404、未确认不受理、版本冲突不产生旧版执行。

Commit: `feat: 保存本人确认的后台执行安排`。

## Task B1：日历、错过与单次持久受理

Files:
- Create: `packages/xuanpu-server/src/xuanpu_server/task_triggers/calendar.py`、`worker.py`
- Modify: `packages/xuanpu-server/src/xuanpu_server/reminders/schedule.py`（仅在确需抽取共用纯函数时）、`task_triggers/store.py`、`postgres.py`、`work_tasks/service.py`
- Test: `packages/xuanpu-server/tests/task_triggers/test_calendar.py`、`test_time_occurrences.py`、`test_misfires.py`

- [x] once/daily/weekly调用经过测试的共用日历函数，不引入自制cron。每个候选先按IANA本地时间计算，再以UTC瞬间形成唯一键。
- [x] 一次性输入必须含明确偏移并核对指定时区；重复规则的gap跳过、fold较早一次，禁止一天执行两次。覆盖上海、纽约春季/秋季DST、跨月年、时钟倒退和重启。
- [x] 先区分资格阻塞与执行提交：任务数/对象/预算配额不足时，只提交blocked occurrence及next_eligibility_check_at（默认60秒），不推进原next_fire_at；额度恢复后用同一occurrence_key受理，超出已确认窗口则明确missed。普通数据库异常仍全回滚，不能通过失败路径跳过时刻。每员工启用定义上限100，沿用WorkTask现有1000上限，超限不删除旧证据腾位置。
- [x] 事务外读取正本，事务内复核definition版本、授权、来源revision/digest、重叠、次数/预算；occurrence占位、WorkTask受理、dispatch和推进next_fire_at同事务。任何一步失败全部回滚，不出现时间游标前进但任务不存在。
- [x] 默认late≤900秒时补1次，超过记missed；明确选择coalesce_latest时只补最近1次，仍检查授权有效期。若同定义正在运行则skipped_overlap，不积压无限历史任务。
- [x] 候选扫描每轮最多100定义，以due索引和稳定分页轮转；不要在员工事务里下载对象、等模型或睡眠。

稳定键的目标纯函数：
```python
from datetime import datetime, timezone

def time_occurrence_key(definition_id: str, version: int, scheduled: datetime) -> str:
    if scheduled.tzinfo is None or scheduled.microsecond:
        raise ValueError('计划时刻必须明确到带时区的整秒')
    stamp = int(scheduled.astimezone(timezone.utc).timestamp())
    return f'time:{definition_id}:{version}:{stamp}'
```

目标测试：
```python
async def test_crash_does_not_advance_time_without_a_task(trigger_world):
    definition = await trigger_world.confirm_once(seconds_from_now=60)
    await trigger_world.advance(60)
    await trigger_world.fail_commit_after('occurrence_created')
    await trigger_world.tick_time()
    assert (await trigger_world.counts())['tasks'] == 0
    await trigger_world.clear_commit_fault()
    await trigger_world.tick_time()
    await trigger_world.tick_time()
    assert (await trigger_world.counts())['tasks'] == 1
```

本Task为harness增加事务故障点，fail_commit_after只在真实事务内抛异常，clear_commit_fault移除故障；tick_time驱动同一真实扫描服务。

Run: `uv run pytest packages/xuanpu-server/tests/task_triggers/test_calendar.py packages/xuanpu-server/tests/task_triggers/test_time_occurrences.py packages/xuanpu-server/tests/task_triggers/test_recovery_authorization.py packages/xuanpu-server/tests/reminders -q`。
Expected: 所有时区实例唯一、无丢单、无无限补跑；原提醒到时/改期/取消语义不变。

Commit: `feat: 按确定日历产生唯一后台任务实例`。

## Task B2：可信业务事件及事务outbox

Files:
- Create: `packages/xuanpu-server/src/xuanpu_server/task_triggers/events.py`、`outbox.py`
- Modify: `packages/xuanpu-server/src/xuanpu_server/task_triggers/postgres.py`、`worker.py`
- Modify: `packages/xuanpu-server/src/xuanpu_server/workspace/models.py`、`memory.py`、`postgres.py`、`service.py`、`creation.py`、`work_tasks/store.py`
- Test: `packages/xuanpu-server/tests/task_triggers/test_outbox_order.py`、`test_event_occurrences.py`、`test_event_privacy.py`

- [x] 只注册file_revision_committed、todo_completed、work_task.succeeded；事件在相应领域写入的同一个数据库事务中产生，回滚不留下事件。模型/客户端无“发布可信事件”公共入口。
- [x] 已结算outbox/delivery只可压缩诊断字段；事件ID、definition/version、关联task及去重墓碑保留，压缩不能使重放再次受理。未结算/未知记录不做TTL清理，并暴露积压/最老未结算时间供C3观察。
- [x] payload只包含本人subject/version、固定输入refs及可信origin_chain；对象正文按员工权限再次读取，其他员工和管理员管理接口看不到文件名、目标及正文。
- [x] 每条outbox有投递结算；扫描只处理已提交未结算行，按delivery租约CAS；先派生唯一occurrence及task再结算投递。重启和重复交付不会重复task。
- [x] 测试两个PG事务：小event_id先分配但延迟提交，大ID先提交并处理，小ID随后提交仍被发现；禁止用“已处理最大ID”过滤它。
- [x] 新definition只订阅确认后产生的事件；编辑版本不会把旧event重放成新实例。源业务事务与definition变更共用员工锁，在事件写出时冻结当时已启用的definition_id/version订阅清单，后来的新定义不接收旧事件；逐行delivery消费此清单，不用MAX(id)或墙钟猜提交顺序。
- [x] 固定事件发生时的精确revision；在派发前文件被更新仍读事件指定版本，删除/权限失效则blocked；不能静默换成当前最新文件。

目标测试：
```python
async def test_late_commit_with_lower_sequence_is_not_lost(trigger_world):
    first = await trigger_world.open_uncommitted_file_event('a.csv')
    second = await trigger_world.commit_file_event('b.csv')
    assert first.event_id < second.event_id
    await trigger_world.tick_events()
    await first.commit()
    await trigger_world.tick_events()
    await trigger_world.tick_events()
    assert (await trigger_world.counts())['event_tasks'] == 2
```

本Task的harness事件方法必须使用不同PG连接和不同员工锁以形成真实提交乱序；先建立分别授权到两名合成员工的定义。不能在同一事务伪造event_id后假称数据库通过。

Run: `uv run pytest packages/xuanpu-server/tests/task_triggers/test_outbox_order.py packages/xuanpu-server/tests/task_triggers/test_event_occurrences.py packages/xuanpu-server/tests/task_triggers/test_api.py -q`。
Expected: 源事务回滚无事件；两次投递一任务；乱序提交不丢失；外部材料伪造event_type/origin不会触发。

Commit: `feat: 从可信领域事务投递后台任务触发`。

## Task B3：环路控制、主动业务衔接和后台生命周期

Files:
- Modify: `packages/xuanpu-server/src/xuanpu_server/task_triggers/events.py`、`service.py`、`worker.py`、`platform.py`、`config.py`
- Modify: `packages/xuanpu-server/src/xuanpu_server/proactive/service.py`、`proactive/models.py`（只在增加明确“执行安排草稿”引用时）
- Test: `packages/xuanpu-server/tests/task_triggers/test_cycles.py`、`test_worker_lifecycle.py`、`test_proactive_bridge.py`
- Docs: `docs/architecture/work-tasks.md`、`docs/architecture/proactive-business.md`、`docs/guides/proactive-assistant.md`

- [x] 触发链来源只能来自服务端task/definition关联。忽略同定义自身派生的文件/任务事件；深度>3、单定义每小时>20次则暂停并产生一条有意义通知，计数持久化不因重启清零。
- [x] 控制“任务成功→生成文件→文件变化→任务”循环；结果文件默认不作为同链输入。写入客户端metadata不能伪造干净来源绕过检测。
- [x] 原主动业务识别只提出带证据的执行安排草稿；必须本人确认后交给definition服务。现有承诺识别、定时提醒、完成后停催完全保留，不能把旧“提醒安排”升级成执行授权。
- [x] Platform持有唯一trigger worker；开启在instance guard和持久task worker就绪之后，关闭先停触发再停派发/执行，再关闭模型、数据库和guard。
- [x] feature flag关闭仍可查询/暂停/取消既有定义，停止创建新occurrence；暂存事件逐行保留或按撤销授权明确结算，不能丢弃后声称已执行。
- [x] 真PG下模拟停机3天、恢复、重复事件、版本编辑与取消同时发生；调度器不会堆出3天全部任务或恢复已撤销授权。

目标测试：
```python
async def test_generated_output_cannot_retrigger_same_chain(trigger_world):
    definition = await trigger_world.confirm_file_trigger()
    task = await trigger_world.commit_source_and_dispatch(definition)
    await trigger_world.finish_task_with_output(task.task_id)
    await trigger_world.tick_events()
    assert (await trigger_world.counts())['tasks'] == 1
    assert (await trigger_world.counts())['loop_skips'] == 1
```

harness方法调用A的真实任务完成事务并写出带服务器provenance的文件，不直接构造事件来替代集成链路。

Run: `uv run pytest packages/xuanpu-server/tests/task_triggers packages/xuanpu-server/tests/proactive packages/xuanpu-server/tests/reminders -q`，随后完整仓库门禁。
Expected: 提醒与执行授权分离、乱序与重复不丢不重、环路有界、停用后不执行。

Commit: `feat: 衔接主动业务并限制触发循环`。

## B独立验收

- [x] 上海时区安排一次CSV汇总，员工离线期间发生，唯一task有真实成果。
- [x] 暂停超过宽限再恢复，显示missed或显式coalesced，实际实例数量符合确认策略。
- [x] 上传新文件版本触发恰好一次，素材之后再变也不替换已受理版本；乱序提交和重启后事件不丢。
- [x] 生成的成果不反向无限触发；关闭开关/取消定义/停用员工后不再受理新执行。
- [x] B完成仍需C的员工入口、统一动态和生产故障验收；不提前宣称R2整体完成。

## 实施核对（2026-10-21）

B0–B3作为一个完整绿色变更提交。实际新增15个触发模块，将准备、资格、受理、日历、事件投递及双后端辅助拆分，避免超长函数。
测试覆盖按职责合并到test_recovery_authorization、test_review_regressions、test_outbox_order和test_management_race；上方执行路径已更新为实际文件。
最终全量2012 passed、7 skipped；专项80 passed、2 skipped；真实模型time/event两项通过；没有发布服务镜像、部署或开启生产开关。
此时R2-C员工入口、统一动态和生产故障验收仍待实施。

后续交付：2026-10-22已完成R2-C及用户授权的整机重启补验，既定R2强制验收全部签收。
最终生产版本和分层证据见[R2完整验收](../../guides/releases/2026-10-21-r2-acceptance.md)，本节2012项结果仍保留为B独立实施时的历史门禁。
