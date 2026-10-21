"""单实例进程持有办公执行；请求和网页订阅不拥有执行生命周期。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Protocol

from xuanpu_server.errors import (
    ConflictError,
    PermissionDeniedError,
    RuntimeUnavailableError,
)
from xuanpu_server.tenancy import Principal

from .config import DurableExecutionConfig
from .event_mapping import TaskExecutionError, cleanup_uncertain
from .models import TERMINAL, WorkTaskRecord
from .worker import DurableWorker

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from xuanpu_server.workspace.models import WorkspaceState

    from .decisions import TaskDecision, ToolApprovalDecision
    from .execution_models import AttemptLease
    from .proposals import ActionProposalService
    from .store import WorkTaskStore

logger = logging.getLogger(__name__)
TaskKey = tuple[str, str, str]


class Runner(Protocol):
    """执行器返回已验证的阶段结果，不能仅靠内核完成事件宣告成功。"""

    async def run(self, who: Principal, task: WorkTaskRecord, decision: TaskDecision | None = None) -> dict[str, Any]:
        """处理新交办或一组经过认领的本人决定，退出前完成执行清理。"""
        ...


class TaskRegistry:
    """保存强任务引用和本人决定锁；持久状态是唯一查询来源。"""

    def __init__(self, store: WorkTaskStore, runner: Runner, *, process_owner: str,
                 now: Callable[[], float] = time.time,
                 cleanup: Callable[[WorkTaskRecord], Awaitable[None]] | None = None,
                 close_resources: Callable[[], Awaitable[None]] | None = None,
                 recover: Callable[[WorkTaskRecord], Awaitable[None]] | None = None,
                 actions: ActionProposalService | None = None,
                 durable: DurableExecutionConfig | None = None,
                 ordinary_turns: Callable[[str],int] | None = None) -> None:
        """注入任务存储、处理器和当前独占进程身份。"""
        self.store, self.runner, self.process_owner, self.now = store, runner, process_owner, now
        self.cleanup, self.close_resources, self.recover, self.actions = cleanup, close_resources, recover, actions
        self._active: dict[TaskKey, asyncio.Task[None]] = {}
        self._controls: dict[TaskKey, asyncio.Task[None]] = {}
        self._failures: list[BaseException] = []
        self._locks: dict[TaskKey, asyncio.Lock] = {}
        self._scan: asyncio.Task[None] | None = None
        self._accepting = False
        self._closing = False
        self._admission = asyncio.Lock()
        self._ordinary_turns = ordinary_turns or (lambda tenant: 0)
        self._lifecycle = asyncio.Lock()
        self._executing: set[TaskKey] = set()
        self._stop_reasons: dict[TaskKey, tuple[str, str]] = {}
        self.durable = DurableWorker(self, durable or DurableExecutionConfig())

    async def open(self, *, scan: bool = True) -> None:
        """独占持有权获得后结算旧进程，再补领本进程已提交的accepted。"""
        await self.reconcile(restart=True)
        self._accepting = True
        if scan:
            self._scan = asyncio.create_task(self._scan_loop())

    def start(self, who: Principal, task: WorkTaskRecord) -> None:
        """同步保留执行引用，不等待模型，不依赖HTTP响应保持连接。"""
        if not self._accepting:
            raise RuntimeUnavailableError('任务执行器正在关闭或尚未启动')
        key = (task.tenant_id, task.user_id, task.task_id)
        if len(self._active) >= 16 or key in self._active or (task.status != 'accepted' and task.recovery_policy_version != 2) or any(
                active[:2] == key[:2] for active in self._active):
            return
        self._launch(key, who, task)

    def _launch(self, key: TaskKey, who: Principal, task: WorkTaskRecord,
                decision: TaskDecision | None = None) -> None:
        """唯一持有后台任务，完成回调取走异常并移除强引用。"""
        work = asyncio.create_task(self._execute(who, task, decision))
        self._active[key] = work

        def finished(done: asyncio.Task[None]) -> None:
            """只清理本次持有者，避免迟到回调删除后继工作。"""
            if self._active.get(key) is done:
                del self._active[key]
                self._executing.discard(key)
            if not done.cancelled():
                error = done.exception()
                if error is not None:
                    self._failures.append(error)
                    logger.error('任务收尾失败 task=%s', task.task_id, exc_info=error)

        work.add_done_callback(finished)

    async def _execute(self, who: Principal, task: WorkTaskRecord, decision: TaskDecision | None) -> None:
        """CAS认领后执行；异常和取消都先完成执行器清理，再持久结算。"""
        if task.recovery_policy_version == 2:
            await self.durable.execute(who, task, decision)
            return
        await self._execute_legacy(who, task, decision)

    async def _execute_legacy(self, who: Principal, task: WorkTaskRecord, decision: TaskDecision | None) -> None:
        """R1执行保持既有取消和保守中断行为，不进入R2租约恢复。"""
        key = task.tenant_id, task.user_id, task.task_id
        if not await self._admit(key):
            return
        try:
            if decision is None:
                task = await self.store.transition(who, task.task_id, 'running',
                    expected_version=task.state_version, at=self.now(), event_kind='started', resources_pending=True)
            result = dict(await self.runner.run(who, task, decision))
            await self._finish_result(who, task, result)
        except asyncio.CancelledError:
            status, code = self._stop_reasons.get(key, (
                'interrupted' if self._closing else 'cancelled',
                'service_shutdown' if self._closing else 'cancel_requested'))
            await self._settle(task, status, code)
            raise
        except ConflictError:
            # CAS输给取消或其他合法状态，读取结果，不能覆盖获胜者。
            try:
                current = await self.store.get(who, task.task_id)
            except PermissionDeniedError:
                await self._settle(task, 'interrupted', 'employee_unavailable')
                return
            if current.status not in TERMINAL:
                await self._settle(task, 'failed', 'task_state_conflict')
        except PermissionDeniedError:
            await self._settle(task, 'interrupted', 'employee_unavailable')
        except TaskExecutionError as error:
            current = await self.store.get(who, task.task_id)
            await self.store.transition(who, task.task_id, 'failed', expected_version=current.state_version,
                at=self.now(), error_code=error.code, error_message=error.message)

        except Exception as error:
            logger.exception('办公任务处理失败 task=%s', task.task_id)
            pending_cleanup = cleanup_uncertain(error)
            await self._settle(task, 'outcome_unknown' if pending_cleanup else 'failed',
                               'container_cleanup_unverified' if pending_cleanup else 'execution_failed')
        finally:
            self._executing.discard(key)

    async def _finish_result(self, who: Principal, task: WorkTaskRecord, result: dict[str, Any]) -> None:
        """真实结果、取消意图及待核实写回执共同决定唯一持久结论。"""
        key = task.tenant_id, task.user_id, task.task_id
        current = await self.store.get(who, task.task_id)
        if current.status in TERMINAL:
            return
        status = result.pop('status')
        if task.recovery_policy_version == 2:
            result['pending_decision'] = {}
        if status in {'succeeded', 'failed', 'cancelled'} and current.unverified_write:
            status = 'outcome_unknown'
            result['error_code'] = 'write_outcome_unverified'
        if current.cancel_requested:
            status, code = self._stop_reasons.get(key, ('cancelled','cancel_requested'))
            await self._settle(task, status, code)
        else:
            await self.store.transition(who, task.task_id, status,
                expected_version=current.state_version, at=self.now(), **result)

    async def _admit(self, key: TaskKey) -> bool:
        """准备容器前限制总活动任务和租户配额，普通会话仍走已有准入复核。"""
        async with self._admission:
            if key in self._executing:
                return True
            if len(self._executing) >= 8 or any(active[:2] == key[:2] for active in self._executing):
                return False
            tenant = await self.store.workspace._control.get_tenant(key[0])
            if tenant is None or tenant.status != 'active':
                return False
            maximum = min(8, tenant.quota.max_concurrent_turns or 8)
            ordinary = self._ordinary_turns(key[0]) if tenant.quota.max_concurrent_turns is not None else 0
            if sum(active[0] == key[0] for active in self._executing) + ordinary >= maximum:
                return False
            self._executing.add(key)
            return True

    async def _settle(self, task: WorkTaskRecord, status: str, error_code: str, *,
                      lease: AttemptLease | None = None) -> None:
        """内部收尾不要求被停用员工重新授权，也不会执行任何业务写入。"""
        from .execution_context import current_execution

        binding = current_execution.get()
        owned = lease or (binding.lease if binding is not None else None)
        await self.store.settle_lifecycle(task.tenant_id, task.user_id, task.task_id,
            status, at=self.now(), error_code=error_code,
            expected_owner=owned.owner if owned else (task.process_owner if task.recovery_policy_version == 2 else None),
            expected_attempt=owned.attempt_no if owned else (task.attempt_no if task.recovery_policy_version == 2 else None),
            expected_epoch=owned.epoch if owned else None)

    async def cancel(self, who: Principal, task: WorkTaskRecord) -> None:
        """取消准备与关闭协调；控制任务继续独立持有清理所有权。"""
        async with self._lifecycle:
            if self._closing:
                raise RuntimeUnavailableError('任务执行器正在关闭')
            control = self._cancel(who, task)
        await asyncio.shield(control)

    def _cancel(self, who: Principal, task: WorkTaskRecord) -> asyncio.Task[None]:
        """同步登记取消持有者，请求断开不撤销已登记的清理。"""
        key = task.tenant_id, task.user_id, task.task_id
        control = self._controls.get(key)
        if control is None:
            control = asyncio.create_task(self._cancel_owned(key, task))
            self._controls[key] = control

            def finished(done: asyncio.Task[None]) -> None:
                """请求断开不取消收尾，异常仍由平台持有并阻止错误退出声明。"""
                self._controls.pop(key, None)
                if not done.cancelled():
                    error = done.exception()
                    if error is not None:
                        self._failures.append(error)

            control.add_done_callback(finished)
        return control

    async def _cancel_owned(self, key: TaskKey, task: WorkTaskRecord) -> None:
        """执行者及等待批准的资源均退出后，才可以结算停止。"""
        work = self._active.get(key)
        if work is not None:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        if self.cleanup is not None:
            await self.cleanup(task)
        status, code = self._stop_reasons.get(key, ('cancelled','cancel_requested'))
        await self._settle(task, status, code)

    async def decide(self, who: Principal, task: WorkTaskRecord, decision: TaskDecision) -> WorkTaskRecord:
        """决定提交及移交与平台关闭互斥；关闭不能漏掉尚在事务中的认领。"""
        async with self._lifecycle:
            if not self._accepting:
                raise RuntimeUnavailableError('任务执行器正在关闭或尚未启动')
            if decision.kind == 'office_actions':
                if self.actions is None:
                    raise ConflictError('办公确认服务尚未准备好')
                return await self.actions.confirm(who, task.task_id, decision)
            return await self._decide(who, task, decision)

    async def _decide(self, who: Principal, task: WorkTaskRecord, decision: TaskDecision) -> WorkTaskRecord:
        """同请求回放先于状态检查，认领与状态切换在同一员工事务。"""
        from .rules import advance
        from .store import require_task, save_record, validate_sources

        key = task.tenant_id, task.user_id, task.task_id
        fingerprint = hashlib.sha256(json.dumps(decision.model_dump(mode='json'), sort_keys=True,
                                                separators=(',', ':')).encode()).hexdigest()
        async with self._locks.setdefault(key, asyncio.Lock()):
            # 准入先于员工事务，既不消费无法恢复的决定，也不反向等待数据库连接。
            current = await self.store.get(who, task.task_id)
            old = current.decision_fingerprints.get(decision.request_id)
            if old is not None:
                if old != fingerprint:
                    raise ConflictError('同一决定请求的内容不同')
                return current
            if len(self._active) >= 8 or key in self._active or not self._accepting:
                raise ConflictError('任务仍在收尾或执行器不可用，请稍后重试')
            if not await self._admit(key):
                raise ConflictError('当前执行额度已满，决定尚未消费，请稍后重试')
            launched = False
            try:
                async with self.store.workspace.transaction(who, 'work-task:decision') as state:
                    current = require_task(state, task.task_id)
                    if current.status != 'waiting_user' or current.state_version != decision.state_version:
                        raise ConflictError('任务当前不等待该决定，请重新读取')
                    if decision.kind != 'tool_approval':
                        raise ConflictError('当前任务尚无可确认的办公行动项')
                    validate_sources(state, current)
                    calls = self._prepare_decision(state, current, decision)
                    updated = advance(current, 'running', expected_version=current.state_version,
                        at=self.now(), process_owner=self.process_owner,
                        resources_pending=current.resources_pending if current.recovery_policy_version == 2 else True,
                        unverified_write=bool(calls), unverified_calls=tuple(sorted(calls)),
                        decision_fingerprints={**current.decision_fingerprints, decision.request_id: fingerprint},
                        **({'pending_decision': decision.model_dump(mode='json')} if current.recovery_policy_version == 2 else {}))
                    save_record(state, updated, 'decision_accepted')
                self._launch(key, who, updated, decision)
                launched = True
                return updated
            finally:
                if not launched:
                    self._executing.discard(key)

    def _prepare_decision(self, state: WorkspaceState, current: WorkTaskRecord, decision: ToolApprovalDecision) -> set[str]:
        """同员工事务验证续接入口，并保留R1批准即标记可能派发的兼容行为。"""
        if current.recovery_policy_version == 2:
            from .execution_store import dispatch_of

            dispatch = dispatch_of(state, current.task_id)
            if dispatch.state == 'blocked' or current.unsafe_resume:
                raise ConflictError('block_resume：原挂起依赖的执行上下文不能安全恢复')
            allowed = self.durable.config.stateless_http_tools
            if not current.pending or any(entry.get('detail', {}).get('target') not in allowed
                                          for entry in current.pending):
                raise ConflictError('block_resume：该工具尚未登记为无状态HTTP续接入口')
            state.task_dispatch[current.task_id] = dispatch.model_copy(update={
                'state': 'ready', 'due_at': self.now()}).model_dump(mode='json')
        calls = set(current.unverified_calls)
        for pending in current.pending:
            value = decision.decisions.get(pending.get('request_id', ''))
            if (current.recovery_policy_version == 1 and value is not None and value.granted
                and pending.get('detail', {}).get('scope') == 'tool_use'):
                calls.add(pending.get('related_call_id') or pending['request_id'])
        return calls

    async def reconcile(self, *, restart: bool = False) -> None:
        """旧进程只中断；本进程accepted遗漏受理时可以安全补领。"""
        if not restart:
            await self.durable.scan()
        cursor = None
        while True:
            owners = await self.store.workspace.store.task_owners(after=cursor, limit=100, legacy_only=not restart)
            for tenant, user in owners:
                for task in await self.store.lifecycle_records(tenant, user, legacy_only=not restart):
                    await self._reconcile_task(task, restart=restart)
            if len(owners) < 100:
                return
            cursor = owners[-1]

    async def _reconcile_task(self, task: WorkTaskRecord, *, restart: bool) -> None:
        """逐项结算旧所有权；终态仅补资源清理和已上传文件索引。"""
        if task.recovery_policy_version == 2:
            if restart:
                await self.durable.reconcile(task, restart=True)
            return
        await self._reconcile_legacy(task, restart=restart)

    async def _reconcile_legacy(self, task: WorkTaskRecord, *, restart: bool) -> None:
        """旧版本资源仍按原所有权清理，旧运行终态绝不重放。"""
        if task.status in TERMINAL:
            if restart and self.recover is not None and (task.artifact_recovery_pending or task.resources_pending or (
                    task.error_code == 'container_cleanup_unverified' and not task.resources_recovered)):
                await self._recover(task)
            return
        if restart and task.process_owner != self.process_owner and self.recover is not None:
            await self._recover(task)
        if task.status == 'waiting_user':
            if restart and task.unverified_write:
                await self._settle(task, 'outcome_unknown', 'write_outcome_unverified')
            elif task.cancel_requested:
                await self.cancel(_owner(task), task)
            return
        if restart and task.process_owner != self.process_owner:
            await self._settle(task, 'interrupted', 'process_restarted')
        elif self._accepting and task.status == 'accepted' and task.process_owner == self.process_owner:
            self.start(_owner(task), task)
        elif task.cancel_requested:
            await self.cancel(_owner(task), task)

    async def _recover(self, task: WorkTaskRecord) -> None:
        """包括异常终态在内，按持久资源所有权清理后登记回执。"""
        assert self.recover is not None
        try:
            await self.recover(task)
            await self.store.resources_closed(task, at=self.now())
        except BaseException as exc:
            self._failures.append(exc)
            raise

    async def _scan_loop(self) -> None:
        """有界间隔补领已提交但请求断开未登记的受理。"""
        while True:
            await asyncio.sleep(1)
            try:
                await self.reconcile()
            except Exception:
                logger.exception('工作任务补领失败')

    async def on_user_disabled(self, tenant_id: str, user_id: str) -> None:
        """停用后先等待本员工执行退出，再结算全部非终态。"""
        await self.durable.execution.revoke_employee(tenant_id, user_id)
        for task in await self.store.lifecycle_records(tenant_id, user_id):
            who = Principal(role='tenant_member', tenant_id=tenant_id, user_id=user_id, key_id=None)
            key = task.tenant_id, task.user_id, task.task_id
            self._stop_reasons[key] = ('interrupted','employee_unavailable')
            try:
                await self.cancel(who, task)
            finally:
                self._stop_reasons.pop(key,None)

    async def join(self) -> None:
        """验证与关闭时等待当前全部托管执行。"""
        while self._active:
            await asyncio.gather(*tuple(self._active.values()))

    async def close(self) -> None:
        """停接单，停止补领，取消并等待全部执行者，失败向平台收尾传播。"""
        self._accepting, self._closing = False, True
        async with self._lifecycle:
            await self._close_owned()

    async def _close_owned(self) -> None:
        """准备中的决定已退出或移交，现在强引用完整覆盖全部执行者。"""
        if self._scan is not None:
            self._scan.cancel()
            await asyncio.gather(self._scan, return_exceptions=True)
        for work in tuple(self._active.values()):
            work.cancel()
        results = await asyncio.gather(*tuple(self._active.values()), return_exceptions=True)
        if self._controls:
            await asyncio.gather(*tuple(self._controls.values()))
        if self.close_resources is not None:
            await self.close_resources()
        failures = self._failures + [result for result in results if isinstance(result, BaseException)
                    and not isinstance(result, asyncio.CancelledError)]
        if failures:
            raise BaseExceptionGroup('办公执行尚未全部退出', failures)


def _owner(task: WorkTaskRecord) -> Principal:
    """后台补领保留初始actor，权限只限持久任务员工，不提升原始角色。"""
    suffix = '/user:' + task.user_id
    key_id = None
    if task.actor.startswith('key:') and task.actor.endswith(suffix):
        key_id = task.actor[4:-len(suffix)]
    elif task.actor != 'user:' + task.user_id:
        raise ConflictError('任务执行身份存档不合法')
    return Principal(role='tenant_member', tenant_id=task.tenant_id, user_id=task.user_id, key_id=key_id)
