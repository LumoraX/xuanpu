"""私域精准局部文件编辑（Unified Diff + 一键撤回）与跨节点 Trace Artifact 测试（ADR 0025）。"""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING, Any

import pytest

from server.core.errors import InvalidRequestError
from server.domains.workspace.patch import TextPatchEdit, apply_exact_edits
from server.engine.tracing.models import TraceOwner

from .conftest import employee

if TYPE_CHECKING:
    from pathlib import Path

ALICE = TraceOwner(tenant_id="acme", user_id="usr_alice")


def test_apply_exact_edits_unique_anchor_and_diff() -> None:
    """精确局部编辑：唯一命中生成标准 Unified Diff；未命中或歧义直接拒绝且 0 改动。"""
    src = "# 复盘\n\n- 状态：进行中\n- 负责人：Alice\n"
    res = apply_exact_edits(
        src,
        [TextPatchEdit(target="- 状态：进行中", replacement="- 状态：已完成")],
        filename="review.md",
    )
    assert "- 状态：已完成" in res.new_text
    assert res.applied_count == 1
    assert "--- a/review.md" in res.diff and "+- 状态：已完成" in res.diff

    with pytest.raises(InvalidRequestError, match="未找到精确匹配"):
        apply_exact_edits(src, [TextPatchEdit(target="不存在的内容", replacement="x")])

    dup = "重复行\n中间\n重复行\n"
    with pytest.raises(InvalidRequestError, match="歧义"):
        apply_exact_edits(dup, [TextPatchEdit(target="重复行", replacement="新行")])


async def test_patch_personal_file_with_diff_and_undo(butler: dict[str, Any], step: Any) -> None:
    """管家通过 act_personal(patch_personal_file) 精准改文件、返回 Diff，并可经 /undo 还原旧版本。"""
    platform = butler["world"].platform
    alice = butler["alice"]
    saved = await platform.workspace.upload(
        alice, "weekly.md", "# 周报\n\n本周进度：80%\n".encode(), "text/markdown", "up_w1"
    )
    file_id = saved["id"]

    butler["world"].models.script(
        step(calls=[("c1", "act_personal", {
            "action": "patch_personal_file",
            "item_id": file_id,
            "expected_version": saved["version"],
            "edits": [{"target": "本周进度：80%", "replacement": "本周进度：100%"}],
        })]),
        step(text="已帮你把周报进度更新为 100%。"),
    )
    resp = await butler["http"].post(
        "/v1/assistant/messages", json={"request_id": "pf1", "text": "把周报进度改成 100%"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    actions = body["executed_actions"]
    assert len(actions) == 1
    assert "+本周进度：100%" in actions[0]["diff"]

    cur_bytes, _, _ = await platform.workspace.download(alice, file_id)
    assert cur_bytes.decode("utf-8") == "# 周报\n\n本周进度：100%\n"

    # 一键撤回：恢复到修订前原文
    aid = actions[0]["action_id"]
    undo_resp = await butler["http"].post(f"/v1/assistant/actions/pf1/{aid}/undo")
    assert undo_resp.status_code == 200, undo_resp.text
    restored_bytes, _, _ = await platform.workspace.download(alice, file_id)
    assert restored_bytes.decode("utf-8") == "# 周报\n\n本周进度：80%\n"


async def test_trace_artifact_preview_promote_and_cross_node_readonly_handoff(
    butler: dict[str, Any], tmp_path: Path,
) -> None:
    """过程产物支持按 trace 列表、UTF-8 预览、一键转存正式空间、跨用户隔离及只读接力挂载（传播外部污点）。"""
    platform = butler["world"].platform
    art = await platform.trace_artifacts.publish(
        ALICE,
        trace_id="trc_art_1",
        name="research_notes.md",
        content="# 联网调研摘录\n\n结论 A".encode(),
        mime_type="text/markdown",
        dispatch_id="dsp_1",
        step_index=0,
        contains_external_input=True,
    )

    listed = (await butler["http"].get("/v1/traces/trc_art_1/artifacts")).json()
    assert len(listed["items"]) == 1
    assert listed["items"][0]["artifact_id"] == art.artifact_id
    assert listed["items"][0]["contains_external_input"] is True

    preview = (await butler["http"].get(f"/v1/artifacts/{art.artifact_id}/preview")).json()
    assert "结论 A" in preview["text"]

    # 跨节点只读接力挂载：目标文件权限 0444 且传播 contains_external_input=True
    handoff_dir = tmp_path / "sandbox_inputs"
    paths, tainted = await platform.trace_artifacts.materialize_readonly_inputs(
        ALICE, [art.artifact_id], handoff_dir
    )
    assert len(paths) == 1 and tainted is True
    assert paths[0].read_text(encoding="utf-8") == "# 联网调研摘录\n\n结论 A"
    assert stat.S_IMODE(os.stat(paths[0]).st_mode) == 0o444

    # 一键转存正式个人空间
    promoted = (await butler["http"].post(
        f"/v1/artifacts/{art.artifact_id}/promote",
        json={"request_id": "prom_1"},
    )).json()
    assert promoted["artifact"]["promoted_item_id"] == promoted["file"]["id"]

    # 他人不可见
    butler["as"]["who"] = await employee(butler["world"], "usr_bob")
    assert (await butler["http"].get(f"/v1/artifacts/{art.artifact_id}/preview")).status_code == 404
