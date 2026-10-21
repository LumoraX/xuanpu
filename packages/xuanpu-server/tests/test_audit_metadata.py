"""管理审计不会回传任意事件载荷。"""
from xuanpu_server.api.schemas import audit_metadata_view
from xuanpu_server.controlplane.models import AuditRecord


def test_audit_metadata_omits_payload() -> None:
    """未知事件也不能绕过元数据白名单。"""
    record = AuditRecord("acme", "engine", "unknown_custom_event", 1.0, "engine",
                         data={"text": "PRIVATE-7d83", "arguments": "SECRET-12"})
    result = audit_metadata_view(record)
    assert result["kind"] == "unknown_custom_event"
    assert "data" not in result
    assert "PRIVATE-7d83" not in str(result)
