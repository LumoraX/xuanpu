"""评测运行器：把业务包清单里的评测用例跑一遍，给业务方发布前的效果反馈。

模型的成本、延迟、效果不像 SQL 那样可以事先确定（ADR 0001 后果），所以平台要给业务方
业务层面的反馈工具。第一版做最直接的一种：每个用例开一个会话、发一句输入、按期望检查回复。
评测走的是正常的会话路径——同样过闸门、同样计量、同样审计。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import InvalidRequestError, PlatformError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from xuanpu_spec.manifest import EvalCase

    from xuanpu_server.audit import AuditLog
    from xuanpu_server.packages import PackageRegistry
    from xuanpu_server.runtime.sessions import SessionManager
    from xuanpu_server.tenancy import Principal

DEFAULT_CONCURRENCY = 4


@dataclass(frozen=True)
class CaseResult:
    """一个用例的结果。

    Attributes:
        name: 用例名。
        passed: 是否通过。
        reasons: 没通过的原因，逐条列出。
        reply: 模型的回复文本（挂起时是挂起前的部分）。
        outcome: 本轮的结局：completed / failed / suspended / error。
        total_tokens: 本轮消耗的 token。
        duration_ms: 耗时。
        session_id: 用到的会话，便于去审计里查细节。
    """

    name: str
    passed: bool
    reasons: tuple[str, ...]
    reply: str
    outcome: str
    total_tokens: int
    duration_ms: int
    session_id: str | None

    def as_dict(self) -> dict[str, Any]:
        """对外表示。"""
        return {
            "name": self.name,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "reply": self.reply,
            "outcome": self.outcome,
            "total_tokens": self.total_tokens,
            "duration_ms": self.duration_ms,
            "session_id": self.session_id,
        }


@dataclass(frozen=True)
class EvalReport:
    """一次评测的报告。"""

    package: str
    version: str
    cases: tuple[CaseResult, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        """是否全部通过。"""
        return all(case.passed for case in self.cases)

    def as_dict(self) -> dict[str, Any]:
        """对外表示。"""
        return {
            "package": self.package,
            "version": self.version,
            "passed": self.passed,
            "total": len(self.cases),
            "failed": sum(1 for case in self.cases if not case.passed),
            "total_tokens": sum(case.total_tokens for case in self.cases),
            "cases": [case.as_dict() for case in self.cases],
        }


def check_expectations(case: EvalCase, reply: str, outcome: str, total_tokens: int) -> list[str]:
    """按用例的期望检查，返回没通过的原因。"""
    reasons: list[str] = []
    if outcome != "completed":
        reasons.append(f"本轮没有正常完成：{outcome}")
    for fragment in case.expect.contains:
        if fragment not in reply:
            reasons.append(f"回复里没有 {fragment!r}")
    for fragment in case.expect.not_contains:
        if fragment in reply:
            reasons.append(f"回复里不该出现 {fragment!r}")
    limit = case.expect.max_tokens
    if limit is not None and total_tokens > limit:
        reasons.append(f"消耗 {total_tokens} token，超过上限 {limit}")
    return reasons


def _usage_total(data: dict[str, Any]) -> int:
    """从终结事件里取本轮的 token 总量。"""
    usage = data.get("subtree_usage") or data.get("usage") or {}
    if not isinstance(usage, dict):
        return 0
    total = int(usage.get("total_tokens") or 0)
    return total or int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)


class EvalRunner:
    """评测运行器。"""

    def __init__(
        self,
        sessions: SessionManager,
        packages: PackageRegistry,
        audit: AuditLog,
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        """
        Args:
            sessions: 会话管理。
            packages: 业务包登记处。
            audit: 审计写入口。
            concurrency: 同时运行的用例数。
        """
        self._sessions = sessions
        self._packages = packages
        self._audit = audit
        self._concurrency = concurrency

    async def _run_case(
        self, principal: Principal, package: str, version: str, case: EvalCase
    ) -> CaseResult:
        """跑一个用例：开会话、发输入、收事件、关会话。"""
        started = time.monotonic()
        session_id: str | None = None
        parts: list[str] = []
        outcome = "error"
        total_tokens = 0
        try:
            record = await self._sessions.create(
                principal, package=package, version=version, entry=case.entry
            )
            session_id = record.session_id
            turn = await self._sessions.send_message(principal, session_id, case.input)
            async for event in turn.stream.events():
                if event.msg.kind == "assistant_text":
                    parts.append(str(event.msg.data.get("delta", "")))
                elif event.msg.kind in ("turn_completed", "turn_failed", "turn_suspended"):
                    outcome = event.msg.kind.removeprefix("turn_")
                    total_tokens = _usage_total(event.msg.data)
        except PlatformError as exc:
            reasons = [f"{exc.code}：{exc.message}"]
            return CaseResult(
                case.name, False, tuple(reasons), "".join(parts), "error", 0,
                int((time.monotonic() - started) * 1000), session_id,
            )  # fmt: skip
        finally:
            if session_id is not None:
                await self._sessions.close(principal, session_id)
        reply = "".join(parts)
        reasons = check_expectations(case, reply, outcome, total_tokens)
        return CaseResult(
            name=case.name,
            passed=not reasons,
            reasons=tuple(reasons),
            reply=reply,
            outcome=outcome,
            total_tokens=total_tokens,
            duration_ms=int((time.monotonic() - started) * 1000),
            session_id=session_id,
        )

    async def run(
        self,
        principal: Principal,
        package: str,
        *,
        version: str | None = None,
        names: Sequence[str] | None = None,
    ) -> EvalReport:
        """跑一个业务包版本的评测用例。

        Args:
            principal: 调用者，须是租户管理员。
            package: 业务包名。
            version: 版本；None 用生效版本。
            names: 只跑这些用例；None 跑全部。

        Raises:
            InvalidRequestError: 清单里没有评测用例，或指定的用例名不存在。
        """
        tenant_id = principal.require_tenant_admin()
        resolved = await self._packages.resolve(tenant_id, package, version)
        cases = list(resolved.manifest.spec.evals)
        if names is not None:
            known = {case.name for case in cases}
            unknown = sorted(set(names) - known)
            if unknown:
                raise InvalidRequestError(f"清单里没有这些评测用例：{', '.join(unknown)}")
            cases = [case for case in cases if case.name in set(names)]
        if not cases:
            raise InvalidRequestError(f"业务包 {package!r} 没有评测用例")

        gate = asyncio.Semaphore(self._concurrency)
        chosen_version = resolved.record.version

        async def limited(case: EvalCase) -> CaseResult:
            async with gate:
                return await self._run_case(principal, package, chosen_version, case)

        results = await asyncio.gather(*(limited(case) for case in cases))
        report = EvalReport(package=package, version=chosen_version, cases=tuple(results))
        await self._audit.control(
            principal,
            tenant_id,
            "eval_completed",
            {
                "package": package,
                "version": chosen_version,
                "passed": report.passed,
                "failed": [case.name for case in results if not case.passed],
                "total_tokens": sum(case.total_tokens for case in results),
            },
        )
        return report


__all__ = ["CaseResult", "EvalReport", "EvalRunner", "check_expectations"]
