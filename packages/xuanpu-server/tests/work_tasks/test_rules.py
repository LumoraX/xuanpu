"""任务必须有经核验的成果，且终态不能被迟到事件覆盖。"""

from __future__ import annotations

import pytest
from xuanpu_server.errors import ConflictError
from xuanpu_server.work_tasks.models import WorkTaskInput, WorkTaskRecord
from xuanpu_server.work_tasks.rules import advance, input_fingerprint


def record() -> WorkTaskRecord:
    """构造服务器已受理的测试记录。"""
    return WorkTaskRecord(task_id="task1", tenant_id="acme", user_id="alice", actor="user:alice",
        request_id="req1", input_fingerprint="a" * 64, text="汇总", timezone="Asia/Shanghai",
        inputs=(), package="office-assistant", package_version="1.0.0", package_digest="b" * 64,
        process_owner="p1", accepted_at=1.0, updated_at=1.0)


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "outcome_unknown", "interrupted"])
def test_terminal_state_never_reopens(status: str) -> None:
    """迟到完成事件与重启扫描均不能回退终态。"""
    task = record().model_copy(update={"status": status, "finished_at": 2.0})
    with pytest.raises(ConflictError):
        advance(task, "running", expected_version=1, at=3.0)


def test_compare_and_swap_rejects_concurrent_old_version() -> None:
    """同一版本只能有一次状态更新。"""
    running = advance(record(), "running", expected_version=1, at=2.0)
    assert running.state_version == 2
    with pytest.raises(ConflictError):
        advance(running, "cancelled", expected_version=1, at=3.0)


def test_completed_turn_without_artifacts_cannot_succeed() -> None:
    """模型结束不能代替成果保存回执。"""
    running = advance(record(), "running", expected_version=1, at=2.0)
    with pytest.raises(ConflictError):
        advance(running, "succeeded", expected_version=2, at=3.0)


def test_fingerprint_includes_revision_text_and_timezone() -> None:
    """重试指纹覆盖所有输入，不能借同请求替换材料。"""
    raw = {"request_id": "req", "text": "汇总", "input_files": [{"item_id": "f", "revision": 1}]}
    original = WorkTaskInput.model_validate(raw)
    same = WorkTaskInput.model_validate({**raw, "request_id": "another"})
    changed = WorkTaskInput.model_validate({**raw, "input_files": [{"item_id": "f", "revision": 2}]})
    assert input_fingerprint(original) == input_fingerprint(same)
    assert input_fingerprint(original) != input_fingerprint(changed)


@pytest.mark.parametrize("change", [{"text": "changed"}, {"timezone": "UTC"}, {"matter_id": "other"}])
def test_execution_cannot_change_accepted_goal(change: dict[str, object]) -> None:
    """内部状态更新也不能更换员工已交办的目标。"""
    with pytest.raises(ConflictError):
        advance(record(), "running", expected_version=1, at=2.0, **change)
