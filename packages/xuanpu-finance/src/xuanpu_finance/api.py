"""独立财务 F0 应用与后台工作生命周期。"""

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

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from xuanpu_finance.auth import FinanceAuthenticator
    from xuanpu_finance.invoices.worker import FinanceWorker
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


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """停止后台后，上层才关闭独立数据库与对象资源。"""
    task = asyncio.create_task(_work(app)) if app.state.start_worker and app.state.worker else None
    app.state.worker_task = task
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def create_app(
    *,
    store: FinanceStore,
    auth: FinanceAuthenticator,
    worker: FinanceWorker | None = None,
    mode: str = "enabled",
    start_worker: bool = True,
) -> FastAPI:
    """显式注入独立存储，不在平台进程中导入此应用。"""
    if mode not in MODES:
        raise ValueError("财务模块模式无效")
    app = FastAPI(title="玄圃财务 F0", version="2026.10.21.1", lifespan=_lifespan)
    app.state.store, app.state.auth, app.state.mode = store, auth, mode
    app.state.worker, app.state.start_worker, app.state.worker_error = worker, start_worker, None
    app.add_exception_handler(FinanceError, _finance_error)
    app.add_exception_handler(RequestValidationError, _invalid_request)
    app.add_exception_handler(Exception, _unexpected)
    app.add_middleware(RequestBodyLimit)
    app.include_router(router)

    @app.get("/health")
    async def health() -> JSONResponse:
        """健康状态不表示任何票据已验真或业务已完成。"""
        failed = app.state.worker_error is not None
        return JSONResponse(
            status_code=503 if failed else 200,
            content={"status": "unavailable" if failed else "ok", "mode": app.state.mode},
        )

    return app
