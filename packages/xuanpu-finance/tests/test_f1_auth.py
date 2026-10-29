"""业务决定的人工来源必须来自签名，不能由模型参数或工具确认升级。"""

from __future__ import annotations

from typing import Any

import pytest
from xuanpu_finance.errors import FinanceError

from . import test_auth
from .test_auth import signed_request


@pytest.fixture
def f1_auth_env() -> Any:
    """复用真实密钥fixture工厂，避免fixture导入与参数重名。"""
    return test_auth.auth_env.__wrapped__()


async def test_signed_human_origin_is_retained(f1_auth_env: Any) -> None:
    """只有受信签名的人工上下文能成为业务决定主体。"""
    auth, key = f1_auth_env
    who = await auth.authenticate(signed_request(key, extra={"origin": "human"}))
    assert getattr(who, "origin", None) == "human"


async def test_missing_origin_is_agent_for_f0_compatibility(f1_auth_env: Any) -> None:
    """旧调用只能得到agent权限，不能默认升级成人工。"""
    auth, key = f1_auth_env
    who = await auth.authenticate(signed_request(key))
    assert getattr(who, "origin", None) == "agent"


@pytest.mark.parametrize("origin", ["model-human", "", None, 1])
async def test_unknown_origin_is_rejected(f1_auth_env: Any, origin: Any) -> None:
    """伪造来源即使有合法签名也不能作为有效身份格式。"""
    auth, key = f1_auth_env
    with pytest.raises(FinanceError, match="invalid_context"):
        await auth.authenticate(signed_request(key, extra={"origin": origin}))


async def test_origin_is_bound_to_persistent_replay(f1_auth_env: Any) -> None:
    """同jti重新签名改变人工来源，不能混用原上下文。"""
    auth, key = f1_auth_env
    await auth.authenticate(signed_request(key, extra={"origin": "agent"}))
    with pytest.raises(FinanceError):
        await auth.authenticate(signed_request(key, extra={"origin": "human"}))


async def test_replay_subject_parts_are_not_ambiguous(f1_auth_env: Any) -> None:
    """带分隔符的租户/员工ID不能拼接成同一重放主体。"""
    auth, key = f1_auth_env
    await auth.authenticate(signed_request(key, subject="c", extra={"tenant_id": "a:b"}))
    with pytest.raises(FinanceError):
        await auth.authenticate(signed_request(key, subject="b:c", extra={"tenant_id": "a"}))
