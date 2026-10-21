"""执行空间标识避免员工、任务、恶意路径与拼接碰撞。"""

from __future__ import annotations

import pytest
from xuanpu_server.runtime.execution_scope import ExecutionScope


def test_employee_and_execution_have_distinct_stable_storage_keys():
    """同包、同名任务也不能使不同员工共享目录。"""
    scopes = [ExecutionScope("acme", "alice", "task:one"),
              ExecutionScope("acme", "bob", "task:one"),
              ExecutionScope("acme", "alice", "task:two"),
              ExecutionScope("other", "alice", "task:one")]
    assert len({scope.storage_key() for scope in scopes}) == 4
    assert scopes[0].storage_key() == ExecutionScope("acme", "alice", "task:one").storage_key()
    assert all(len(scope.storage_key()) == 64 and "/" not in scope.storage_key() for scope in scopes)


def test_malicious_components_cannot_escape_or_collide():
    """完整JSON编码后摘要，路径字符和分隔符不会变成目录。"""
    a = ExecutionScope("acme", "a/b", "../../other")
    b = ExecutionScope("acme/a", "b", "../../other")
    assert a.storage_key() != b.storage_key()
    assert "." not in a.storage_key()


@pytest.mark.parametrize("values", [("", "a", "s"), ("t", "", "s"), ("t", "a", ""), ("t", "a", "s" * 513)])
def test_empty_or_unbounded_scope_is_rejected(values):
    """内部调用也必须提供有界身份。"""
    with pytest.raises(ValueError):
        ExecutionScope(*values)
