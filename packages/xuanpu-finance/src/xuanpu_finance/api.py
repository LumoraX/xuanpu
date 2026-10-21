"""独立财务 F0/F1/F2 应用与后台工作生命周期。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from xuanpu_finance.errors import FinanceError
from xuanpu_finance.limits import RequestBodyLimit
from xuanpu_finance.routes import router
from xuanpu_finance.routes_callback import router as callback_router
from xuanpu_finance.routes_f1 import router as f1_router
from xuanpu_finance.routes_f2 import router as f2_router

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from xuanpu_finance.auth import FinanceAuthenticator
    from xuanpu_finance.invoices.worker import FinanceWorker
    from xuanpu_finance.settlement.providers import ProviderRegistry
    from xuanpu_finance.settlement.worker import SettlementWorker
    from xuanpu_finance.storage import FinanceStore

logger = logging.getLogger(__name__)
MODES = frozenset({"enabled", "draining", "read_only", "disabled"})


def error_response(status: int, code: str, message: str) -> JSONResponse:
    """错误不回显请求原文、个人数据或凭据。"""
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "message": message,
            "trace_id": uuid.uuid4().hex,
            "retryable": status == 503,
        },
    )


async def _finance_error(request: Request, error: Exception) -> JSONResponse:
    """保留领域约束的稳定错误，不伪装重试成功。"""
    assert isinstance(error, FinanceError)
    return error_response(error.status, error.code, error.message)


async def _invalid_request(request: Request, error: Exception) -> JSONResponse:
    """校验失败不输出原始输入值。"""
    return error_response(422, "invalid_request", "请求字段或版本格式无效")


async def _unexpected(request: Request, error: Exception) -> JSONResponse:
    """未知故障只记录类型，数据库凭据不进入日志。"""
    logger.error("财务请求未处理异常：%s", type(error).__name__)
    return error_response(500, "internal_error", "财务服务内部错误")


async def _work(app: FastAPI) -> None:
    """持久任务由服务持有，不绑定浏览器请求连接。"""
    worker: FinanceWorker = app.state.worker
    try:
        while True:
            if app.state.mode in ("enabled", "draining") and await worker.run_once():
                continue
            await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        app.state.worker_error = type(error).__name__
        logger.error("财务 worker 停止：%s", type(error).__name__)


async def _settlement_work(app: FastAPI) -> None:
    """独立外部队列在只读或停用时继续只读核对，不发新请求。"""
    worker: SettlementWorker = app.state.settlement_worker
    try:
        while True:
            dispatch = app.state.mode == "enabled" and app.state.worker_error is None
            if await worker.run_once(allow_dispatch=dispatch):
                continue
            await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        app.state.settlement_worker_error = type(error).__name__
        logger.error("财务外部核对执行器停止：%s", type(error).__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """停止两个独立后台任务后，上层才关闭连接池与HTTP资源。"""
    active = app.state.start_worker
    task = asyncio.create_task(_work(app)) if active and app.state.worker else None
    settlement = (
        asyncio.create_task(_settlement_work(app))
        if active and app.state.settlement_worker
        else None
    )
    app.state.worker_task, app.state.settlement_task = task, settlement
    try:
        yield
    finally:
        for current in (task, settlement):
            if current is not None:
                current.cancel()
        for current in (task, settlement):
            if current is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await current


def create_app(
    *,
    store: FinanceStore,
    auth: FinanceAuthenticator,
    worker: FinanceWorker | None = None,
    mode: str = "enabled",
    start_worker: bool = True,
    settlement_worker: SettlementWorker | None = None,
    settlement_providers: ProviderRegistry | None = None,
) -> FastAPI:
    """显式注入独立存储，不在平台进程中导入此应用。"""
    if mode not in MODES:
        raise ValueError("财务模块模式无效")
    app = FastAPI(title="玄圃财务 F0/F1/F2", version="2026.10.21.3", lifespan=_lifespan)
    app.state.store, app.state.auth, app.state.mode = store, auth, mode
    app.state.worker, app.state.start_worker, app.state.worker_error = worker, start_worker, None
    app.state.settlement_worker, app.state.settlement_providers = (
        settlement_worker,
        settlement_providers,
    )
    app.state.settlement_worker_error, app.state.instance_lock_lost = None, False
    if settlement_worker is not None:
        settlement_worker.dispatch_allowed = lambda: (
            app.state.mode == "enabled"
            and app.state.worker_error is None
            and app.state.settlement_worker_error is None
            and not app.state.instance_lock_lost
        )
    app.add_exception_handler(FinanceError, _finance_error)
    app.add_exception_handler(RequestValidationError, _invalid_request)
    app.add_exception_handler(Exception, _unexpected)
    app.add_middleware(RequestBodyLimit)
    app.include_router(router)
    app.include_router(f1_router)
    app.include_router(f2_router)
    app.include_router(callback_router)

    @app.get("/health")
    async def health() -> JSONResponse:
        """健康状态不表示任何票据已验真或业务已完成。"""
        failed = app.state.worker_error is not None or app.state.settlement_worker_error is not None
        return JSONResponse(
            status_code=503 if failed else 200,
            content={"status": "unavailable" if failed else "ok", "mode": app.state.mode},
        )

    return app
