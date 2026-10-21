"""交办输入必须固定版本且不能自授身份或执行权限。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from xuanpu_server.work_tasks.models import WorkTaskInput


def request(**changes: object) -> dict[str, object]:
    """生成最小且有效的员工交办。"""
    return {"request_id": "req-1", "text": "整理会议材料",
            "input_files": [{"item_id": "f1", "revision": 2}], **changes}


@pytest.mark.parametrize("field", ["user_id", "tenant_id", "actor", "package", "budget", "scope"])
def test_authority_cannot_be_chosen(field: str) -> None:
    """请求只描述目标与已存在附件，不选择权限。"""
    with pytest.raises(ValidationError):
        WorkTaskInput.model_validate(request(**{field: "someone-else"}))


@pytest.mark.parametrize("changes", [
    {"text": " \n"}, {"text": "a" * 8001}, {"request_id": " "},
    {"input_files": [{"item_id": "f1"}]},
    {"input_files": [{"item_id": "f1", "revision": True}]},
    {"input_files": [{"item_id": "f1", "revision": float("nan")}]},
    {"input_files": [{"item_id": "f1", "revision": 1, "object_key": "secret"}]},
    {"input_files": []}, {"input_files": [{"item_id": "f1", "revision": 1}] * 9},
    {"timezone": "not/a/timezone"},
])
def test_invalid_input_is_rejected(changes: dict[str, object]) -> None:
    """无效文字、版本和时区在执行前明确拒绝。"""
    with pytest.raises(ValidationError):
        WorkTaskInput.model_validate(request(**changes))


def test_task_input_pins_revision_and_is_deeply_immutable() -> None:
    """调用者修改原始列表不能改变已验证的任务输入。"""
    raw = request()
    task = WorkTaskInput.model_validate(raw)
    raw["input_files"][0]["revision"] = 3  # type: ignore[index]
    assert task.input_files[0].revision == 2
    assert isinstance(task.input_files, tuple)
    with pytest.raises(ValidationError):
        task.input_files[0].revision = 4
