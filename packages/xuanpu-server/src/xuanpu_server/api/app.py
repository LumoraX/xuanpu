"""HTTP 接入层。

接入层只做三件事：认证、把请求翻译成对 ``Platform`` 的调用、把结果与异常翻译成响应。
业务规则（权限、租户隔离、配额）都在平台里，不在这里。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from xuanpu_server import __version__
from xuanpu_server.api import schemas
from xuanpu_server.api.sse import stream_turn
from xuanpu_server.controlplane.models import AuditQuery
from xuanpu_server.errors import (
    AuthenticationError,
    InvalidRequestError,
    NotFoundError,
    PackageInvalidError,
    PlatformError,
    SessionBusyError,
)
from xuanpu_server.tenancy import Principal

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from xuanpu_server.platform import Platform
    from xuanpu_server.runtime.sessions import Turn

logger = logging.getLogger(__name__)

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_BEARER = "bearer "


async def caller(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> Principal:
    """从 ``Authorization: Bearer <凭据>`` 得到调用者。"""
    if authorization is None or not authorization.lower().startswith(_BEARER):
        raise AuthenticationError("凭据无效")
    platform: Platform = request.app.state.platform
    return await platform.authenticate(authorization[len(_BEARER) :].strip())


# 路由函数的参数标注要在模块作用域里解析得到，所以依赖项定义在模块层
Caller = Annotated[Principal, Depends(caller)]


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    """统一的错误响应。"""
    return JSONResponse(
        status_code=status, content={"error": {"code": code, "message": message, **extra}}
    )


def _turn_response(turn: Turn) -> StreamingResponse:
    """一轮对话的事件流响应。"""
    return StreamingResponse(
        stream_turn(turn),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Submission-Id": turn.submission_id,
        },
    )


def _install_error_handlers(app: FastAPI) -> None:
    """把平台异常翻译成响应。"""

    @app.exception_handler(PlatformError)
    async def _platform_error(_: Request, exc: PlatformError) -> JSONResponse:
        extra: dict[str, Any] = {}
        if isinstance(exc, PackageInvalidError):
            extra["problems"] = list(exc.problems)
        if isinstance(exc, SessionBusyError):
            extra["owner"] = exc.owner
        if exc.status >= 500:
            logger.error("请求失败：%s（%s）", exc.message, exc.code)
        return _error(exc.status, exc.code, exc.message, **extra)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = [
            f"{'.'.join(str(part) for part in item['loc'])}：{item['msg']}" for item in exc.errors()
        ]
        return _error(400, "invalid_request", "请求参数不合法", problems=problems)

    @app.exception_handler(ValueError)
    async def _value_error(_: Request, exc: ValueError) -> JSONResponse:
        return _error(400, "invalid_request", str(exc))

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        # 未预料的异常：细节只进日志，不回给调用方
        logger.exception("未处理的异常", exc_info=exc)
        return _error(500, "internal_error", "服务内部错误")


def create_app(  # noqa: PLR0915, C901 —— 路由登记的平铺清单
    platform: Platform, *, console_dir: Path | None = None
) -> FastAPI:
    """用一个装配好的平台创建 HTTP 应用。

    Args:
        platform: 装配好的平台。
        console_dir: 控制台静态页面的目录；给了就挂在 ``/console/``。
    """

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await platform.start()
        try:
            yield
        finally:
            await platform.close()

    # 自动生成的接口描述与交互式文档页面都不开放：它们不需要凭据，接口参考以 http-api.md 为准
    app = FastAPI(
        title="xuanpu",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.platform = platform
    _install_error_handlers(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        """存活探测。不需要凭据，也不透露租户信息。"""
        return {"status": "ok", "version": __version__, "instance": platform.instance_id}

    # --- 平台管理 ---

    @app.post("/v1/admin/tenants", status_code=201)
    async def create_tenant(body: schemas.CreateTenantBody, who: Caller) -> dict[str, Any]:
        """新建租户。"""
        tenant = await platform.create_tenant(
            who, tenant_id=body.tenant_id, name=body.name, quota=body.quota.limits()
        )
        return schemas.tenant_view(tenant)

    @app.get("/v1/admin/tenants")
    async def list_tenants(who: Caller) -> dict[str, Any]:
        """列出租户。"""
        who.require_platform_admin()
        tenants = await platform.store.list_tenants()
        return {"tenants": [schemas.tenant_view(tenant) for tenant in tenants]}

    @app.put("/v1/admin/tenants/{tenant_id}/status")
    async def set_tenant_status(
        tenant_id: str, body: schemas.TenantStatusBody, who: Caller
    ) -> dict[str, Any]:
        """启用或停用租户。"""
        return schemas.tenant_view(await platform.set_tenant_status(who, tenant_id, body.status))

    @app.put("/v1/admin/tenants/{tenant_id}/quota")
    async def set_tenant_quota(
        tenant_id: str, body: schemas.QuotaBody, who: Caller
    ) -> dict[str, Any]:
        """整体替换租户的配额：请求体里没给的项恢复为不限。"""
        return schemas.tenant_view(await platform.set_tenant_quota(who, tenant_id, body.limits()))

    @app.post("/v1/admin/tenants/{tenant_id}/keys", status_code=201)
    async def issue_tenant_key(
        tenant_id: str, body: schemas.IssueKeyBody, who: Caller
    ) -> dict[str, Any]:
        """给某个租户签发 API key。完整的 key 只在这次响应里出现。"""
        who.require_platform_admin()
        issued = await platform.issue_key(who, tenant_id=tenant_id, name=body.name, role=body.role)
        return {"key": issued.key, **schemas.key_view(issued.record)}

    # --- 凭据 ---

    @app.post("/v1/keys", status_code=201)
    async def issue_key(body: schemas.IssueKeyBody, who: Caller) -> dict[str, Any]:
        """给本租户签发 API key。"""
        issued = await platform.issue_key(who, tenant_id=None, name=body.name, role=body.role)
        return {"key": issued.key, **schemas.key_view(issued.record)}

    @app.get("/v1/keys")
    async def list_keys(who: Caller) -> dict[str, Any]:
        """列出本租户的 API key。"""
        records = await platform.store.list_api_keys(who.require_tenant_admin())
        return {"keys": [schemas.key_view(record) for record in records]}

    @app.delete("/v1/keys/{key_id}")
    async def revoke_key(key_id: str, who: Caller) -> dict[str, Any]:
        """吊销本租户的一把 API key。"""
        if not await platform.revoke_key(who, key_id):
            raise NotFoundError("API key 不存在或已吊销")
        return {"revoked": True}

    # --- 密钥 ---

    @app.put("/v1/secrets/{name}")
    async def put_secret(name: str, body: schemas.SecretBody, who: Caller) -> dict[str, Any]:
        """写入或覆盖一条密钥。"""
        await platform.put_secret(who, name, body.value)
        return {"name": name}

    @app.get("/v1/secrets")
    async def list_secrets(who: Caller) -> dict[str, Any]:
        """列出密钥名。取值写入之后不可再读出。"""
        return {"names": await platform.vault.names(who.require_tenant_admin())}

    @app.delete("/v1/secrets/{name}")
    async def delete_secret(name: str, who: Caller) -> dict[str, Any]:
        """删除一条密钥。"""
        if not await platform.delete_secret(who, name):
            raise NotFoundError(f"密钥 {name!r} 不存在")
        return {"deleted": True}

    # --- 业务包 ---

    @app.post("/v1/packages", status_code=201)
    async def publish_package(
        request: Request, who: Caller, activate: Annotated[bool, Query()] = False
    ) -> dict[str, Any]:
        """发布业务包。请求体是归档本身（``application/zip``）。"""
        who.require_tenant_admin()
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_ARCHIVE_BYTES:
                raise InvalidRequestError(f"归档超过 {MAX_ARCHIVE_BYTES} 字节")
            chunks.append(chunk)
        record = await platform.packages.publish(who, b"".join(chunks), activate=activate)
        active = await platform.packages.active_version(record.tenant_id, record.name)
        return schemas.package_view(record, active=active)

    @app.get("/v1/packages")
    async def list_packages(
        who: Caller, name: Annotated[str | None, Query()] = None
    ) -> dict[str, Any]:
        """列出本租户已发布的业务包版本。"""
        tenant_id = who.require_tenant()
        records = await platform.packages.list_versions(tenant_id, name)
        active = {
            item: await platform.packages.active_version(tenant_id, item)
            for item in {record.name for record in records}
        }
        return {
            "packages": [
                schemas.package_view(record, active=active[record.name]) for record in records
            ]
        }

    @app.post("/v1/packages/{name}/activate")
    async def activate_package(
        name: str, body: schemas.ActivateBody, who: Caller
    ) -> dict[str, Any]:
        """切换生效版本（前进或回滚）。"""
        await platform.packages.activate(who, name, body.version)
        return {"name": name, "active": body.version}

    @app.post("/v1/packages/{name}/evals")
    async def run_evals(name: str, body: schemas.EvalBody, who: Caller) -> dict[str, Any]:
        """运行业务包的评测用例，返回报告。用例较多时这个请求会比较久。"""
        report = await platform.evals.run(who, name, version=body.version, names=body.names)
        return report.as_dict()

    # --- 会话 ---

    @app.post("/v1/sessions", status_code=201)
    async def create_session(body: schemas.CreateSessionBody, who: Caller) -> dict[str, Any]:
        """新建会话。"""
        record = await platform.sessions.create(
            who,
            package=body.package,
            version=body.version,
            entry=body.entry,
            metadata=body.metadata,
        )
        return schemas.session_view(record)

    @app.get("/v1/sessions")
    async def list_sessions(
        who: Caller,
        status: Annotated[str | None, Query(pattern="^(active|closed)$")] = None,
        mine: Annotated[bool, Query()] = False,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> dict[str, Any]:
        """列出本租户的会话；``mine=true`` 只看本凭据创建的。"""
        records = await platform.sessions.list_sessions(who, status=status, mine=mine, limit=limit)
        return {"sessions": [schemas.session_view(record) for record in records]}

    @app.get("/v1/sessions/{session_id}")
    async def get_session(session_id: str, who: Caller) -> dict[str, Any]:
        """取会话。"""
        return schemas.session_view(await platform.sessions.get(who, session_id))

    @app.delete("/v1/sessions/{session_id}")
    async def close_session(session_id: str, who: Caller) -> dict[str, Any]:
        """关闭会话。进行中的对话会被取消。"""
        return schemas.session_view(await platform.sessions.close(who, session_id))

    @app.get("/v1/sessions/{session_id}/pending")
    async def pending_requests(session_id: str, who: Caller) -> dict[str, Any]:
        """会话当前等待处理的审批或表单请求。"""
        return {"pending": await platform.sessions.pending(who, session_id)}

    @app.get("/v1/sessions/{session_id}/events")
    async def replay_events(
        session_id: str,
        who: Caller,
        submission_id: Annotated[str, Query()],
        after_seq: Annotated[int, Query(ge=-1)] = -1,
    ) -> dict[str, Any]:
        """回放一轮对话已记入审计的事件：断线之后用它补齐，而不是重发消息。"""
        events = await platform.sessions.replay(who, session_id, submission_id, after_seq=after_seq)
        return {"submission_id": submission_id, "events": events}

    @app.post("/v1/sessions/{session_id}/messages")
    async def send_message(
        session_id: str, body: schemas.MessageBody, who: Caller
    ) -> StreamingResponse:
        """发一条消息，响应是这一轮对话的事件流。"""
        return _turn_response(await platform.sessions.send_message(who, session_id, body.text))

    @app.post("/v1/sessions/{session_id}/resume")
    async def resume_session(
        session_id: str, body: schemas.ResumeBody, who: Caller
    ) -> StreamingResponse:
        """送回审批结果或表单答案，响应是续跑部分的事件流。"""
        return _turn_response(await platform.sessions.resume(who, session_id, body.resolutions))

    @app.post("/v1/sessions/{session_id}/cancel")
    async def cancel_turns(
        session_id: str, who: Caller, body: schemas.CancelBody | None = None
    ) -> dict[str, Any]:
        """停止生成：取消会话里在途的对话（请求体可省略），返回停了哪些 submission。"""
        submission_id = body.submission_id if body is not None else None
        return {"cancelled": await platform.sessions.cancel(who, session_id, submission_id)}

    # --- 审计与用量 ---

    @app.get("/v1/audit")
    async def query_audit(  # noqa: PLR0913 —— 查询条件逐项对应查询参数
        who: Caller,
        session_id: Annotated[str | None, Query()] = None,
        kind: Annotated[str | None, Query()] = None,
        source: Annotated[str | None, Query(pattern="^(engine|gate|control)$")] = None,
        since: Annotated[float | None, Query()] = None,
        until: Annotated[float | None, Query()] = None,
        after_id: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> dict[str, Any]:
        """查询本租户的审计记录。用响应里的 ``next_after_id`` 翻页。"""
        records = await platform.store.query_audit(
            who.require_tenant_admin(),
            AuditQuery(
                session_id=session_id,
                kind=kind,
                source=source,  # type: ignore[arg-type]
                since=since,
                until=until,
                after_id=after_id,
                limit=limit,
            ),
        )
        return {
            "records": [schemas.audit_view(record) for record in records],
            "next_after_id": records[-1].audit_id if len(records) == limit else None,
        }

    @app.get("/v1/usage")
    async def query_usage(
        who: Caller,
        since: Annotated[str, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")],
        until: Annotated[str, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")],
    ) -> dict[str, Any]:
        """查询本租户一段日期内的用量。"""
        records = await platform.store.list_usage(
            who.require_tenant_admin(), since=since, until=until
        )
        return {"usage": [schemas.usage_view(record) for record in records]}

    if console_dir is not None:
        app.mount("/console", StaticFiles(directory=str(console_dir), html=True), name="console")

    return app


__all__ = ["MAX_ARCHIVE_BYTES", "create_app"]
