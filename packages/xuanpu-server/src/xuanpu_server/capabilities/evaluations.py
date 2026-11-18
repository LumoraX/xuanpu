"""隔离能力评测（R4-C0）：固定能力修订、安装配置摘要与套件摘要，逐用例保存结果与独立判定。

- 套件是严格 JSON（最多 50 个用例），只允许预定义检查（包含/不包含/token 上限），没有插件或脚本；
  套件摘要须等于交付描述声明的 ``evaluation_suite_sha256``，否则 409（换了用例就是另一份证据）；
- 声明了企业连接或写入/外部工具的能力，本版本不在隔离环境里替换生产目标：直接 ``blocked``
  （``external_implementation_not_verified``），不用替身补 pass；
- 用例逐个串行运行，总 token 超预算即 ``blocked``，不截断成通过；判定看实际轮次结局与回复，
  不听模型自报；评测会话由评测记录列名、凭评测绑定运行，评测结束后这些会话不能继续；
- 进程中断时仍在进行的评测在启动时标 ``interrupted``，不自动重放；可用新操作号重新评测。
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import ConflictError, NotFoundError, PlatformError
from xuanpu_server.ids import new_id

from .delivery import fingerprint
from .models import CapabilityEvaluation, CaseOutcome

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.runtime.sessions import SessionManager
    from xuanpu_server.tenancy import Principal

    from .models import EvaluationCase
    from .schemas import EvaluateInput
    from .service import CapabilityService

logger = logging.getLogger(__name__)
_EXCERPT = 2000


def evaluation_view(record: CapabilityEvaluation) -> dict[str, JsonValue]:
    """管理视图：状态、摘要与逐用例判定；不含测试输入正文与原始回复全文。"""
    return {'id': record.id, 'capability_id': record.capability_id,
            'capability_revision': record.capability_revision, 'installation_id': record.installation_id,
            'config_digest': record.config_digest, 'suite_digest': record.suite_digest,
            'token_budget': record.token_budget, 'status': record.status, 'reason': record.reason,
            'version': record.version, 'created_at': record.created_at, 'finished_at': record.finished_at,
            'cases': [{'name': c.name, 'passed': c.passed, 'reasons': list(c.reasons), 'outcome': c.outcome,
                       'total_tokens': c.total_tokens} for c in record.cases]}


def judge(case: EvaluationCase, reply: str, outcome: str, tokens: int) -> list[str]:
    """预定义检查；轮次没有正常完成本身就是失败。"""
    reasons = [] if outcome == 'completed' else [f'轮次结局为 {outcome}']
    reasons += [f'回复缺少「{text}」' for text in case.contains if text not in reply]
    reasons += [f'回复不应出现「{text}」' for text in case.not_contains if text in reply]
    if case.max_tokens is not None and tokens > case.max_tokens:
        reasons.append(f'用量 {tokens} 超过 {case.max_tokens}')
    return reasons


class CapabilityEvaluations:
    """评测的受理、后台执行、查询与中断恢复。"""

    def __init__(self, service: CapabilityService) -> None:
        """会话管理由平台装配后注入（评测经正常会话路径运行）。"""
        self.service = service
        self.sessions: SessionManager | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self, who: Principal, body: EvaluateInput) -> dict[str, JsonValue]:
        """受理一次评测：核对安装、配置与套件摘要；能力声明了外部通道则直接 blocked。"""
        s = self.service
        who = await s.admin(who, mutation=True)
        tenant = who.require_tenant()
        payload = {'kind': 'capability.evaluate', **body.model_dump(mode='json')}
        async with s.store.transaction(tenant, participants=s.participants(who)) as tx:
            await s.admin(who, mutation=True)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            state = tx.state.capabilities
            installation = state.installations.get(body.installation_id)
            if installation is None or installation.capability_id != body.capability_id:
                raise NotFoundError('安装不存在')
            config = state.configurations[installation.config_id]
            revision = next(r for r in state.revisions.values()
                            if r.capability_id == body.capability_id and r.revision == config.revision)
            digest = fingerprint(body.suite.model_dump(mode='json'))
            if digest != revision.delivery.evaluation_suite_sha256:
                raise ConflictError('评测套件与交付描述声明的摘要不一致')
            entries = {revision.delivery.package.entry}
            if any(case.entry not in entries for case in body.suite.cases):
                raise ConflictError('评测用例的入口不属于这个能力')
            permissions = revision.delivery.permissions
            external = bool(permissions.connections) or any(t.effect != 'read' for t in permissions.tools)
            record = CapabilityEvaluation(
                tenant_id=tenant, id=new_id('cev'), created_at=s.now(), capability_id=body.capability_id,
                capability_revision=revision.revision, installation_id=installation.id,
                config_id=config.id, config_digest=config.digest, suite_digest=digest, suite=body.suite,
                token_budget=body.token_budget,
                status='blocked' if external else 'running',
                reason='external_implementation_not_verified' if external else None,
                finished_at=s.now() if external else None)
            state.evaluations[record.id] = record
            tx.emit('capability.evaluation_started', record.id, record.version, actor=who.actor, at=s.now())
            result = evaluation_view(record)
            tx.record_receipt(who.actor, body.request_id, payload, result)
        if record.status == 'running':
            task = asyncio.create_task(self._run(who, record.id, revision.delivery.package.name,
                                                 revision.delivery.package.version))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return result

    async def get(self, who: Principal, identifier: str) -> dict[str, JsonValue]:
        """查一次评测（仅管理员）。"""
        s = self.service
        who = await s.admin(who)
        state, _ = await s.store.capability_snapshot(who.require_tenant())
        record = state.evaluations.get(identifier)
        if record is None:
            raise NotFoundError('评测不存在')
        return evaluation_view(record)

    async def _update(self, tenant: str, identifier: str, **changes: Any) -> CapabilityEvaluation:
        """改写进行中的评测记录（版本加一）。"""
        async with self.service.store.transaction(tenant) as tx:
            record = tx.state.capabilities.evaluations[identifier]
            if record.status != 'running':
                return record
            changed = record.model_copy(update={**changes, 'version': record.version + 1})
            tx.state.capabilities.evaluations[identifier] = changed
            return changed

    async def _run(self, who: Principal, identifier: str, package: str, version: str) -> None:
        """逐个用例串行运行；任何意外都以失败收尾，不留「进行中」。"""
        tenant = who.require_tenant()
        try:
            await self._cases(who, identifier, package, version)
        except asyncio.CancelledError:
            await self._update(tenant, identifier, status='interrupted', reason='platform_closed',
                               finished_at=self.service.now())
            raise
        except Exception as exc:
            logger.exception('能力评测失败')
            await self._update(tenant, identifier, status='failed', reason=type(exc).__name__,
                               finished_at=self.service.now())

    async def _cases(self, who: Principal, identifier: str, package: str, version: str) -> None:
        """每个用例开评测会话（先登记到评测记录再说话）、收回复、关会话、独立判定。"""
        assert self.sessions is not None
        tenant = who.require_tenant()
        state, _ = await self.service.store.capability_snapshot(tenant)
        record = state.evaluations[identifier]
        outcomes: list[CaseOutcome] = []
        spent = 0
        for case in record.suite.cases:
            session = await self.sessions.create(who, package=package, version=version, entry=case.entry,
                                                 evaluation_id=identifier)
            record = await self._update(tenant, identifier, sessions=(*record.sessions, session.session_id))
            reply, outcome, tokens = await self._turn(who, session.session_id, case.input)
            spent += tokens
            reasons = judge(case, reply, outcome, tokens)
            outcomes.append(CaseOutcome(name=case.name, passed=not reasons, reasons=tuple(reasons),
                                        outcome=outcome, total_tokens=tokens, reply_excerpt=reply[:_EXCERPT]))
            record = await self._update(tenant, identifier, cases=tuple(outcomes))
            if spent > record.token_budget:
                await self._update(tenant, identifier, status='blocked', reason='budget_exceeded',
                                   finished_at=self.service.now())
                return
        passed = all(c.passed for c in outcomes)
        await self._update(tenant, identifier, status='passed' if passed else 'failed',
                           finished_at=self.service.now())

    async def _turn(self, who: Principal, session_id: str, text: str) -> tuple[str, str, int]:
        """说一句话、读完这一轮；平台错误记为这一用例的结局。"""
        assert self.sessions is not None
        parts: list[str] = []
        outcome, tokens = 'error', 0
        try:
            turn = await self.sessions.send_message(who, session_id, text)
            async for event in turn.stream.events():
                if event.msg.kind == 'assistant_text':
                    parts.append(str(event.msg.data.get('delta', '')))
                elif event.msg.kind in ('turn_completed', 'turn_failed', 'turn_suspended'):
                    outcome = event.msg.kind.removeprefix('turn_')
                    tokens = _usage_total(event.msg.data)
        except PlatformError as exc:
            outcome = 'error:' + exc.code
        finally:
            await self.sessions.close(who, session_id)
        return ''.join(parts), outcome, tokens

    async def recover(self, tenants: list[str]) -> None:
        """启动时：上次进程里还在进行的评测标 interrupted（不自动重放）。"""
        for tenant in tenants:
            state, _ = await self.service.store.capability_snapshot(tenant)
            for record in state.evaluations.values():
                if record.status == 'running':
                    await self._update(tenant, record.id, status='interrupted', reason='process_restarted',
                                       finished_at=self.service.now())

    async def close(self) -> None:
        """平台关闭：取消在途评测（各自标 interrupted）。"""
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


def _usage_total(data: dict[str, Any]) -> int:
    """终结事件里本轮（含子执行）的 token 总量，口径与包评测一致。"""
    usage = data.get('subtree_usage') or data.get('usage') or {}
    if not isinstance(usage, dict):
        return 0
    total = int(usage.get('total_tokens') or 0)
    return total or int(usage.get('input_tokens') or 0) + int(usage.get('output_tokens') or 0)
