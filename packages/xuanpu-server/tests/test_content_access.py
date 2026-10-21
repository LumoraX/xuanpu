"""管理角色不能隐式获取个人正文。"""
from dataclasses import replace

import pytest
from xuanpu_server.controlplane.models import SessionRecord
from xuanpu_server.runtime.content_access import can_read_content
from xuanpu_server.tenancy import Principal


@pytest.mark.parametrize("role", ["tenant_admin", "tenant_member"])
def test_only_employee_owner_reads_personal_content(role: str) -> None:
    """跨员工、跨租户与无代人密钥均不能读取正文。"""
    record = SessionRecord("s", "acme", "office", "1", "main", 0, 0,
                           "user:alice", user_id="alice")
    owner = Principal(role, "acme", None, user_id="alice")
    assert can_read_content(owner, record)
    assert not can_read_content(replace(owner, user_id="bob"), record)
    assert not can_read_content(replace(owner, tenant_id="other"), record)
    assert not can_read_content(Principal("tenant_admin", "acme", "key"), record)


def test_service_session_content_belongs_to_creator() -> None:
    """无主人的系统会话只允许创建者，代人身份不能继承密钥的其他会话。"""
    record = SessionRecord("s", "acme", "office", "1", "main", 0, 0, "key:one")
    assert can_read_content(Principal("tenant_member", "acme", "one"), record)
    assert not can_read_content(Principal("tenant_admin", "acme", "two"), record)
    assert not can_read_content(Principal("tenant_admin", "acme", "one", user_id="alice"), record)
