"""财务 API 共用身份和模块生命周期边界。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.models import FinancePrincipal

if TYPE_CHECKING:
    from xuanpu_finance.auth import FinanceAuthenticator
    from xuanpu_finance.storage import FinanceStore


async def principal(request: Request) -> FinancePrincipal:
    """依赖已认证的请求上下文，不开放身份选择参数。"""
    if request.app.state.mode == "disabled":
        raise FinanceError("module_disabled", 503, "财务模块已停用")
    auth: FinanceAuthenticator = request.app.state.auth
    return await auth.authenticate(request)


FinanceCaller = Annotated[FinancePrincipal, Depends(principal)]


def get_store(request: Request) -> FinanceStore:
    """读取应用注入的独立财务数据库接口。"""
    store: FinanceStore = request.app.state.store
    return store


def require_write(request: Request, *, cancel: bool = False) -> None:
    """排空可取消，执行器故障或只读状态不再受理新写。"""
    mode = request.app.state.mode
    if mode != "enabled" and not (cancel and mode == "draining"):
        raise FinanceError("module_read_only", 409, "当前财务模式不接收该业务操作")
    if any(getattr(request.app.state, name, None) is not None for name in (
        "worker_error", "settlement_worker_error", "compliance_worker_error")):
        raise FinanceError("worker_unavailable", 503, "财务执行器待核对，暂停新受理")
