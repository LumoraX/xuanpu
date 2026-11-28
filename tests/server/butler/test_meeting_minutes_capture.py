"""会议录音转写与纪要全量处理回归测试：长转写不早截断、原件自动关联与事项绑定、业务归类与一键撤回。"""

from __future__ import annotations

from typing import Any

from server.core.tenancy import Principal
from server.domains.identity.models import UserRecord
from server.domains.work_tasks.models import InputFile
from server.domains.workspace.taxonomy import classify_document_sample
from server.engine.butler.personal_actions import execute_personal_action, undo_executed_action
from server.engine.butler.personal_reads import read_extended_personal
from server.engine.butler.turn import ButlerTurn


def _make_asr_transcript(*, turns: int = 280) -> str:
    """构造一份带说话人时间戳、约 560+ 行的中文会议录音转写 Markdown。"""
    lines = ["---", "## 录音原文"]
    for idx in range(turns):
        speaker = (idx % 3) + 1
        mm, ss = divmod(idx * 7, 60)
        lines.append(f"**说话人{speaker} | 00:{mm:02d}:{ss:02d}**")
        lines.append(f"第 {idx + 1} 段会议交流内容：对齐第一阶段交付物、周四下午技术方案评审与后续跟进安排。")
    lines.append("**说话人2 | 00:33:40**")
    lines.append("结尾锚点：后面这个会我整理一份正式会议纪要发到项目工作群。")
    return "\n".join(lines) + "\n"


def test_classify_raw_meeting_asr_transcript_as_meeting_minutes() -> None:
    """原始文件名不含“会议”关键词的语音转写稿，应准确识别为 03-会议纪要而非综合文档。"""
    transcript = _make_asr_transcript(turns=60)
    cls = classify_document_sample(
        filename="周五 14点45分.md",
        mime_type="text/markdown",
        text=transcript,
    )
    assert cls.category_id == "meeting_minutes"
    assert cls.folder_name == "03-会议纪要"
    assert "会议纪要" in cls.suggested_tags
    assert cls.confidence >= 0.5


async def test_meeting_transcript_full_read_attach_bind_classify_and_undo(world: Any) -> None:
    """验证 560+ 行转写稿一次读全、capture_bundle 自动挂载原件并绑定事项、classify_file 显式事项与撤回。"""
    platform = world.platform
    user_id = "usr_meeting_eval"
    await platform.identity.store.create_user(
        UserRecord(
            user_id=user_id,
            tenant_id="acme",
            role="tenant_member",
            status="active",
            display_name="MeetingTester",
            created_at=1000.0,
        )
    )
    who = Principal(role="tenant_member", tenant_id="acme", user_id=user_id, key_id=None)

    matter = await platform.workspace.create(
        who, "matter", {"title": "星河算力项目", "description": "算力交付与方案交流项目"}, "m_meet_1"
    )
    matter_id = matter["id"]

    transcript = _make_asr_transcript(turns=280)
    uploaded = await platform.workspace.upload(
        who,
        "周五 14点45分.md",
        transcript.encode("utf-8"),
        "text/markdown",
        "up_meet_1",
    )
    file_id = uploaded["id"]

    turn = ButlerTurn(
        request_id="req_meet_1",
        trace_id="trc_meet_1",
        who=who,
        timezone="Asia/Shanghai",
        materials=(InputFile(item_id=file_id, revision=1),),
    )

    # 1. 560+ 行（约 15K 字符）的短行语音转写稿，无论是否传 start_line=1，都不应在 250 行处被过早截断
    for read_args in (
        {"item_id": file_id, "revision": 1},
        {"item_id": file_id, "revision": 1, "start_line": 1, "max_chars": 24000},
    ):
        read_res = await read_extended_personal(platform, turn, "file_content", read_args, now=2000.0)
        assert read_res is not None
        assert read_res["has_more"] is False
        assert "结尾锚点：后面这个会我整理一份正式会议纪要" in read_res["content"]

    # 2. 若超长文档仍需分页，必须附带 next_read_hint 指导继续读取
    partial = await read_extended_personal(
        platform, turn, "file_content", {"item_id": file_id, "max_chars": 2000}, now=2000.0
    )
    assert partial is not None
    assert partial["has_more"] is True
    assert "next_read_hint" in partial and "start_line=" in partial["next_read_hint"]

    # 3. 模型调用 capture_bundle 即使漏填 attachment_file_ids，也自动挂载本轮 materials 并回写文件 matter_id
    bundle = {
        "record": {
            "record_kind": "meeting",
            "title": "星河算力项目当面交流",
            "occurred_at": "2026-11-27T14:45:00+08:00",
            "location": "星河科技",
            "content": "对齐第一阶段交付与周四方案交流。",
            "summary": "对齐第一阶段交付与周四方案交流。",
            "decisions": ["12月3日完成第一阶段交付"],
            "matter_refs": [matter_id],
        },
        "matters": [
            {"action": "link", "temp_ref": "m_ref", "matter_id": matter_id, "title": "星河算力项目"}
        ],
    }
    cap = await execute_personal_action(
        platform, turn, {"action": "capture_bundle", "bundle": bundle}, now=2001.0
    )
    assert cap["ok"] is True
    assert file_id in cap["result"]["record"]["attachment_file_ids"]
    file_after_bundle = await platform.workspace.get(who, file_id)
    assert file_after_bundle["data"]["matter_id"] == matter_id

    # 4. 一键撤回 capture_bundle 应同步还原文件 matter_id 为 None
    bundle_action = turn.executed_actions[-1]
    undone = await undo_executed_action(platform, who, bundle_action)
    assert undone["undone"] >= 2
    file_after_undo = await platform.workspace.get(who, file_id)
    assert file_after_undo["data"]["matter_id"] is None

    # 5. classify_file 传入显式 matter_id 时，把原始语音转写归入 03-会议纪要并绑定事项
    cls_res = await execute_personal_action(
        platform,
        turn,
        {"action": "classify_file", "item_id": file_id, "matter_id": matter_id},
        now=2002.0,
    )
    assert cls_res["ok"] is True
    assert cls_res["classification"]["category_id"] == "meeting_minutes"
    assert cls_res["classification"]["folder_name"] == "03-会议纪要"
    file_after_classify = await platform.workspace.get(who, file_id)
    assert file_after_classify["data"]["matter_id"] == matter_id
    assert file_after_classify["data"]["parent_id"] == cls_res["folder_id"]
    assert "会议纪要" in file_after_classify["data"]["tags"]
