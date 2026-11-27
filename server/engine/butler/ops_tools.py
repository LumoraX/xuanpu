"""管家内置「跨节点自我排查（diagnose_trace）」与「受控造匠/发布回退（forge_artisan）」工具处理函数。

- ``diagnose_trace``（只读）：员工仅能诊断自己的 trace/transcript，租户管理员可诊断本租户全部记录；
  经三态 ``TranscriptReader`` 透明回读本地/跨节点/对象存储脱敏日志，并提供确定性根因摘要；
- ``forge_artisan``（管理员强权限）：把 ``ArtisanForgeService`` 与 ``ReleaseLedgerService`` 暴露为
  对话内可控工具，强制走 L-Control 强类型代码修改、L-Content Hook 护栏、L1/L2/L3 门禁与审核发版回退。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from server.core.errors import InvalidRequestError, NotFoundError, PermissionDeniedError

if TYPE_CHECKING:
    from server.engine.butler.turn import ButlerTurn
    from server.engine.forge.models import ReleaseLayer
    from server.platform import Platform


def _require_str(args: dict[str, Any], key: str) -> str:
    """读取必填非空字符串参数。"""
    val = args.get(key)
    if not isinstance(val, str) or not val.strip():
        raise InvalidRequestError(f"缺少必填字符串参数：{key}")
    return val.strip()


def _is_admin(turn: ButlerTurn) -> bool:
    """判断当前调用方是否具备租户管理员或平台管理员权限。"""
    return turn.who.role in ("tenant_admin", "platform_admin")


async def run_diagnose_trace(
    platform: Platform, turn: ButlerTurn, args: dict[str, Any],
) -> dict[str, Any]:
    """执行跨节点 Transcript 自排查工具（只读、严守租户与本人边界）。"""
    tenant = turn.who.require_tenant()
    op = _require_str(args, "op")
    is_admin = _is_admin(turn)

    if op == "list_failed":
        limit = int(args.get("limit") or 20)
        limit = max(1, min(limit, 50))
        rows = list(await platform.store.transcript_store.list_failed(tenant, limit=limit))
        if not is_admin:
            rows = [r for r in rows if r.user_id == turn.who.user_id]
        return {
            "items": [
                {
                    "trace_id": r.trace_id,
                    "span_id": r.span_id,
                    "kind": r.kind,
                    "node_id": r.node_id,
                    "status": r.status,
                    "error_code": r.error_code,
                    "line_count": r.line_count,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ],
        }

    if op == "list_spans":
        trace_id = _require_str(args, "trace_id")
        rows = list(await platform.store.transcript_store.list_for_trace(tenant, trace_id))
        if not is_admin:
            rows = [r for r in rows if r.user_id == turn.who.user_id]
        return {
            "trace_id": trace_id,
            "spans": [
                {
                    "span_id": r.span_id,
                    "parent_span_id": r.parent_span_id,
                    "kind": r.kind,
                    "node_id": r.node_id,
                    "status": r.status,
                    "error_code": r.error_code,
                    "line_count": r.line_count,
                    "archived": r.archived_at is not None,
                }
                for r in rows
            ],
        }

    if op == "read_transcript":
        span_id = _require_str(args, "span_id")
        rec = await platform.store.transcript_store.get(tenant, span_id)
        if rec is None or (not is_admin and rec.user_id != turn.who.user_id):
            raise NotFoundError(f"未找到可访问的 Transcript：{span_id}")
        mode: Literal["compact", "full"] = "full" if str(args.get("mode") or "compact") == "full" else "compact"
        start_line = max(1, int(args.get("start_line") or 1))
        end_raw = args.get("end_line")
        end_line = int(end_raw) if end_raw is not None else start_line + 79
        page = await platform.transcripts.read(
            tenant, span_id, mode=mode, start_line=start_line, end_line=end_line,
        )
        return {
            "span_id": span_id,
            "trace_id": rec.trace_id,
            "mode": mode,
            "source": page.source,
            "total_lines": page.total_lines,
            "lines": list(page.lines),
        }

    if op == "summarize_root_cause":
        trace_id = _require_str(args, "trace_id")
        rows = list(await platform.store.transcript_store.list_for_trace(tenant, trace_id))
        if not is_admin:
            rows = [r for r in rows if r.user_id == turn.who.user_id]
        if not rows:
            raise NotFoundError(f"未找到 trace {trace_id} 的 Transcript 记录")
        findings: list[dict[str, Any]] = []
        for rec in rows:
            page = await platform.transcripts.read(
                tenant, rec.span_id, mode="compact", start_line=1, end_line=200,
            )
            for idx, entry in enumerate(page.lines, start=1):
                if entry.get("status") in ("error", "failed", "cancelled") or entry.get("kind") in (
                    "error", "turn_failed", "turn_suspended"
                ):
                    findings.append({
                        "span_id": rec.span_id,
                        "kind": rec.kind,
                        "node_id": rec.node_id,
                        "line": idx,
                        "event_kind": entry.get("kind"),
                        "status": entry.get("status"),
                        "payload": entry.get("payload"),
                        "truncated_fields": entry.get("truncated_fields", []),
                    })
        return {
            "trace_id": trace_id,
            "span_count": len(rows),
            "finding_count": len(findings),
            "findings": findings[:25],
            "next_step_hint": (
                "如需查看某条异常完整入参或堆栈，请用 diagnose_trace(op='read_transcript', mode='full') 按行号回读"
                if findings else "未发现明显失败/错误步骤"
            ),
        }

    raise InvalidRequestError(f"不支持的自排查操作：{op}")


async def run_forge_artisan(
    platform: Platform, turn: ButlerTurn, args: dict[str, Any],
) -> dict[str, Any]:
    """在对话内受控调用造匠工作坊（L-Control / L-Content / L-Release）与发布节点回退。"""
    who = turn.who
    if not _is_admin(turn):
        raise PermissionDeniedError("造匠与发布节点管理仅限租户管理员操作")
    op = _require_str(args, "op")

    if op == "create_draft":
        rec = await platform.forge.create_draft(
            who,
            package_name=_require_str(args, "package_name"),
            description=_require_str(args, "description"),
            entry=str(args.get("entry") or "main").strip(),
            display_name=str(args["display_name"]).strip() if args.get("display_name") else None,
        )
        return rec.model_dump(mode="json")

    if op == "checkout_draft":
        rec = await platform.forge.checkout_draft(
            who,
            package_name=_require_str(args, "package_name"),
            version=str(args["version"]).strip() if args.get("version") else None,
        )
        return rec.model_dump(mode="json")

    if op == "control_mutate":
        draft_id = _require_str(args, "draft_id")
        action = _require_str(args, "action").removeprefix("forge_")
        raw_params = args.get("params")
        params: dict[str, Any] = dict(raw_params) if isinstance(raw_params, dict) else {}
        res = await platform.forge.apply_control(who, draft_id, action, params)
        return {"summary": res.summary, "version": res.manifest.metadata.version, "diff": res.diff}

    if op == "content_patch":
        draft_id = _require_str(args, "draft_id")
        patches = args.get("patches")
        if not isinstance(patches, list) or not patches:
            raise InvalidRequestError("content_patch 需要非空 patches 列表")
        content_res = await platform.forge.apply_content_patches(who, draft_id, patches)
        return {
            "ok": content_res.ok,
            "message": content_res.message,
            "diffs": content_res.diffs,
            "post_checks": list(content_res.post_checks),
        }

    if op == "diff":
        draft_id = _require_str(args, "draft_id")
        return {"draft_id": draft_id, "diffs": await platform.forge.list_diff(who, draft_id)}

    if op == "run_gates":
        draft_id = _require_str(args, "draft_id")
        l1 = await platform.forge.run_l1_validate(who, draft_id)
        l2 = await platform.forge.run_l2_unit_tests(who, draft_id)
        l3 = await platform.forge.run_l3_eval_check(who, draft_id)
        return {
            "passed": l1.passed and l2.passed and l3.passed,
            "l1": l1.model_dump(mode="json"),
            "l2": l2.model_dump(mode="json"),
            "l3": l3.model_dump(mode="json"),
        }

    if op == "review":
        draft_id = _require_str(args, "draft_id")
        reviewed = await platform.forge.review_draft(
            who,
            draft_id,
            summary=_require_str(args, "summary"),
            approved=bool(args.get("approved")),
        )
        return reviewed.model_dump(mode="json")

    if op == "publish":
        draft_id = _require_str(args, "draft_id")
        draft, node = await platform.forge.publish_draft(who, draft_id)
        return {"draft": draft.model_dump(mode="json"), "release": node.model_dump(mode="json")}

    if op == "list_releases":
        layer_str = str(args.get("layer") or "artisan")
        if layer_str not in ("artisan", "forge", "kernel"):
            raise InvalidRequestError(f"无效发布层级：{layer_str}")
        layer: ReleaseLayer = layer_str  # type: ignore[assignment]
        target = _require_str(args, "target_name")
        tenant = who.require_tenant()
        nodes = await platform.releases.history(tenant, layer, target)
        return {"items": [n.model_dump(mode="json") for n in nodes]}

    if op == "rollback":
        pkg = _require_str(args, "package_name")
        to_release_id = _require_str(args, "to_release_id")
        rb_mode: Literal["retired", "revoked"] = (
            "revoked" if str(args.get("mode") or "retired") == "revoked" else "retired"
        )
        node = await platform.forge.rollback_artisan(who, pkg, to_release_id=to_release_id, mode=rb_mode)
        return node.model_dump(mode="json")

    if op == "disable":
        layer_str = str(args.get("layer") or "artisan")
        if layer_str not in ("artisan", "forge", "kernel"):
            raise InvalidRequestError(f"无效发布层级：{layer_str}")
        layer_d: ReleaseLayer = layer_str  # type: ignore[assignment]
        target = _require_str(args, "target_name")
        revoke = str(args.get("mode") or "revoked") == "revoked"
        tenant = who.require_tenant()
        disabled = await platform.releases.disable(tenant, layer=layer_d, target_name=target, revoke=revoke)
        return {"disabled": disabled.model_dump(mode="json")}

    raise InvalidRequestError(f"不支持的造匠操作：{op}")
