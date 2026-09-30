"""审批：把业务包声明的审批强度换算成内核的权限策略。

业务方只选强度，不写规则（ADR 0001 后果）。规则由平台按强度与副作用分类生成：

| 副作用               | strict | standard | relaxed |
| -------------------- | ------ | -------- | ------- |
| 调用子 skill         | 放行   | 放行     | 放行    |
| 沙盒内跑脚本         | 审批   | 放行     | 放行    |
| 沙盒内执行命令       | 审批   | 审批     | 放行    |
| 连接器：只读         | 审批   | 放行     | 放行    |
| 连接器：写           | 审批   | 审批     | 放行    |
| 连接器：对外         | 审批   | 审批     | 审批    |
| 其余一切             | 审批   | 审批     | 审批    |

需要审批时不阻塞等人：本轮对话挂起，实例可以释放；审批结果经会话的 resume 接口送回后
续跑。没有列在表里的副作用一律走审批，新增的工具不会因为漏配规则而被默认放行。
"""

from __future__ import annotations

import secrets
from typing import TYPE_CHECKING, Any

import taifeng

if TYPE_CHECKING:
    from xuanpu_spec.manifest import ApprovalLevel, Effect

Mode = str

# 审批结果的形状，前端据此渲染审批界面
DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "granted": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["granted"],
}

_SCRIPT_MODE: dict[str, Mode] = {"strict": "ask", "standard": "allow", "relaxed": "allow"}
_SHELL_MODE: dict[str, Mode] = {"strict": "ask", "standard": "ask", "relaxed": "allow"}
_CONNECTOR_MODE: dict[str, dict[str, Mode]] = {
    "strict": {"read": "ask", "write": "ask", "external": "ask"},
    "standard": {"read": "allow", "write": "ask", "external": "ask"},
    "relaxed": {"read": "allow", "write": "allow", "external": "ask"},
}


class SuspendingApprovalPrompter:
    """需要审批时挂起本轮对话。

    只用内核稳定层的类型实现（内核自带的同类实现不在稳定层）。
    """

    async def prompt(self, request: taifeng.PermissionRequest) -> taifeng.PermissionDecision:
        """从不返回：抛出挂起信号，让本轮对话在审批点停下。"""
        detail: dict[str, Any] = {
            "scope": request.scope,
            "target": request.target,
            "reason": request.reason,
            "call_chain": list(request.call_chain),
            **request.metadata,
        }
        raise taifeng.SuspendSignal(
            taifeng.PendingRequest(
                request_id=f"req_{secrets.token_hex(8)}",
                reason=taifeng.SuspendReason.PERMISSION,
                payload_schema=DECISION_SCHEMA,
                related_call_id=request.metadata.get("call_id"),
                detail=detail,
            )
        )


def connector_mode(level: ApprovalLevel, effect: Effect) -> Mode:
    """某个审批强度下，某种副作用的连接器工具是放行还是审批。"""
    return _CONNECTOR_MODE[level][effect]


def _rule(scope: str, pattern: str, mode: Mode, reason: str) -> taifeng.PermissionRule:
    """构造一条规则。"""
    return taifeng.PermissionRule(
        scope=scope,  # type: ignore[arg-type]
        target_pattern=pattern,
        mode=mode,  # type: ignore[arg-type]
        reason=reason,
    )


def build_policy(
    level: ApprovalLevel, connector_tools: dict[str, Effect] | None = None
) -> taifeng.PermissionPolicy:
    """按审批强度生成权限策略。

    Args:
        level: 业务包声明的审批强度。
        connector_tools: 连接器工具的完整名字 → 副作用分类。
    """
    rules = [
        _rule("skill_dispatch", "glob:*", "allow", "调用子 skill 不产生外部副作用"),
        _rule("script_exec", "glob:*", _SCRIPT_MODE[level], f"审批强度 {level}：沙盒内脚本"),
        _rule("shell_exec", "glob:*", _SHELL_MODE[level], f"审批强度 {level}：沙盒内命令"),
    ]
    for name, effect in sorted((connector_tools or {}).items()):
        rules.append(
            _rule(
                "tool_use",
                name,
                connector_mode(level, effect),
                f"审批强度 {level}：连接器工具（{effect}）",
            )
        )
    return taifeng.PermissionPolicy(
        rules=rules,
        # 没有被规则覆盖的一律审批
        default_mode="ask",
        prompter=SuspendingApprovalPrompter(),
    )


__all__ = [
    "DECISION_SCHEMA",
    "SuspendingApprovalPrompter",
    "build_policy",
    "connector_mode",
]
