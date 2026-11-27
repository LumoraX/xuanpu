"""统一 5 级安全评估 ``taifeng.HookRegistry`` 工厂（ADR 0025）。

覆盖所有匠人运行时的 ``pre_tool_use`` 与 ``post_tool_use`` 生命周期：
1. **L1 硬拒绝危险路径与敏感凭据**：阻断路径穿越（``..``）、敏感凭据文件（``.env`` / ``.ssh`` / ``id_rsa``）及破坏性命令；
2. **L2 只读输入保护**：禁止任何文件写入/删除/补丁工具篡改 ``/workspace/inputs/`` 或 ``inputs/`` 下的只读输入与接力产物；
3. **L3 断网沙盒防外泄护栏**：当 ``sandbox.network=False`` 时，拦截 Shell 中的显式外连工具（``curl`` / ``wget`` / ``nc`` / ``ssh``）；
4. **L4 事后轻量体检**：记录大体积输出告警并附加安全检查标记；
5. **L5 失败关闭（Fail-Closed）审计注记**：记录本轮安全拦截与放行轨迹。
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import taifeng

if TYPE_CHECKING:
    from server.engine.packages import ResolvedPackage

_WRITE_TOOLS = frozenset({
    "file_write",
    "write_file",
    "edit_file",
    "apply_patch",
    "delete_file",
    "file_delete",
    "create_file",
})
_SHELL_TOOLS = frozenset({"run_command", "run_in_background", "shell", "bash", "exec_command"})
_SENSITIVE_NAMES = frozenset({".env", ".env.local", ".env.production", "id_rsa", "id_ed25519", "shadow", "passwd"})
_DESTRUCTIVE_CMD_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:rm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+/(?:\s|$)|mkfs\b|dd\s+if=/dev/|:\(\)\s*\{)",
    re.IGNORECASE,
)
_EXFIL_CMD_RE = re.compile(
    r"(?:^|[;&|\s])(?:curl|wget|nc|ncat|netcat|ssh|scp|sftp|telnet)\s+",
    re.IGNORECASE,
)
_LARGE_OUTPUT_CHARS = 80_000


def _extract_paths(arguments: dict[str, Any]) -> list[str]:
    """从各类文件工具参数中提取涉及的目标路径列表。"""
    paths: list[str] = []
    for key in ("path", "file_path", "target_path", "dest", "destination"):
        val = arguments.get(key)
        if isinstance(val, str) and val.strip():
            paths.append(val.strip())
    patches = arguments.get("patches")
    if isinstance(patches, list):
        for item in patches:
            if isinstance(item, dict) and isinstance(item.get("path"), str):
                paths.append(item["path"].strip())
    return paths


def _is_sensitive_path(raw_path: str) -> bool:
    """判定路径是否含越界穿越或敏感凭据文件名。"""
    normalized = raw_path.replace("\\", "/").strip()
    parts = PurePosixPath(normalized).parts
    if ".." in parts:
        return True
    lowered = {p.lower() for p in parts}
    if ".ssh" in lowered or ".aws" in lowered or ".gnupg" in lowered:
        return True
    name = PurePosixPath(normalized).name.lower()
    return name in _SENSITIVE_NAMES


def _is_readonly_input_path(raw_path: str) -> bool:
    """判定路径是否落在只读挂载的输入目录（``/workspace/inputs`` 或 ``inputs/``）内。"""
    normalized = raw_path.replace("\\", "/").strip()
    rel = normalized[2:] if normalized.startswith("./") else normalized
    return (
        rel == "inputs"
        or rel.startswith("inputs/")
        or normalized == "/workspace/inputs"
        or normalized.startswith("/workspace/inputs/")
    )


def build_runtime_hooks(package: ResolvedPackage) -> taifeng.HookRunner:
    """按业务包清单策略构建统一 5 级安全 ``taifeng.HookRunner``。"""
    network_allowed = bool(package.manifest.spec.policy.sandbox.network)
    registry = taifeng.HookRegistry()

    async def _pre_security_chain(
        hook: taifeng.PreToolUseHook, ctx: taifeng.HookContext,
    ) -> taifeng.HookDecision:
        """L1–L3 前置安全评估链：任一红线命中立即硬拒。"""
        tool = hook.tool_name
        args = hook.arguments if isinstance(hook.arguments, dict) else {}
        paths = _extract_paths(args)

        # L1: 敏感凭据与路径穿越硬拒绝
        for p in paths:
            if _is_sensitive_path(p):
                return taifeng.HookDecision.deny(f"安全策略禁止访问敏感或越界路径：{p!r}")

        # L2: 只读输入与跨步接力产物目录禁写保护
        if tool in _WRITE_TOOLS:
            for p in paths:
                if _is_readonly_input_path(p):
                    return taifeng.HookDecision.deny(
                        f"安全策略禁止修改只读输入/接力产物目录：{p!r}"
                    )

        # L1 + L3: Shell 破坏性指令与断网沙盒外连指令拦截
        if tool in _SHELL_TOOLS:
            cmd = str(args.get("command") or args.get("cmd") or "")
            if _DESTRUCTIVE_CMD_RE.search(cmd):
                return taifeng.HookDecision.deny("安全策略拦截高危破坏性系统命令")
            if not network_allowed and _EXFIL_CMD_RE.search(cmd):
                return taifeng.HookDecision.deny("当前匠人处于断网沙盒，禁止调用网络外连命令")

        checks = ctx.extras.get("security_checks")
        if isinstance(checks, list):
            checks.append({"tool": tool, "decision": "allow"})
        return taifeng.HookDecision.ok()

    async def _post_security_chain(
        hook: taifeng.PostToolUseHook, ctx: taifeng.HookContext,
    ) -> taifeng.HookDecision:
        """L4–L5 事后体检：标记超大工具输出与执行摘要。"""
        text = str(hook.output or "")
        if len(text) > _LARGE_OUTPUT_CHARS:
            notices = ctx.extras.get("security_notices")
            if isinstance(notices, list):
                notices.append(f"工具 {hook.tool_name} 输出体积较大（{len(text)} 字符），建议优先落盘为产物")
        return taifeng.HookDecision.ok()

    registry.register("pre_tool_use", _pre_security_chain)
    registry.register("post_tool_use", _post_security_chain)
    return taifeng.HookRunner(registry)
