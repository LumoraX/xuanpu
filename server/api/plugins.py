"""微内核前后端插件注册、动态路由分派、不可变 UI 插件静态托管与受控 Bridge RPC 网关（ADR 0027）。

- ``GET /v1/plugins``：查看后端已挂载插件与实时健康状态；
- ``POST /v1/plugins/{plugin_id}/unmount``：管理员运行时干净卸载指定非内核插件；
- ``GET /v1/plugins/ui``：返回本租户可用的 L2 内置前端插件 + L1 匠人同包 ``ui/`` 插件清单；
- ``GET /v1/plugins/ui/{package}/{version}/{asset_path}``：以严格沙盒 CSP 响应头托管不可变 L1 前端资源；
- ``POST /v1/plugins/bridge/rpc``：Web 主壳代沙盒 iframe 转发的白名单 RPC 网关（严守插件权限声明与本人隔离）；
- ``GET/POST/PUT/DELETE /v1/plugins/ext/{plugin_id}/{subpath}``：动态分派至运行时挂载的后端插件路由。
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from server.api.auth import Caller  # noqa: TC001 —— FastAPI 运行时解析
from server.core.errors import InvalidRequestError, NotFoundError, PermissionDeniedError
from server.core.plugins.builtins import BUILTIN_UI_PLUGINS
from server.core.plugins.models import UiPluginDescriptor
from spec import parse_manifest
from spec.validate import is_safe_relative

if TYPE_CHECKING:
    from server.core.tenancy import Principal
    from server.platform import Platform

logger = logging.getLogger(__name__)

_SANDBOX_CSP = (
    "default-src 'none'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src data: blob:; "
    "font-src data:; "
    "connect-src 'none'; "
    "frame-ancestors 'self'"
)


class BridgeRpcBody(BaseModel):
    """Web 主壳代理沙盒 iframe 发起的白名单 RPC 请求体。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    plugin_id: str = Field(min_length=1, max_length=128)
    method: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    package: str | None = Field(default=None, max_length=64)
    version: str | None = Field(default=None, max_length=64)


async def _tenant_ui_plugins(platform: Platform, who: Principal) -> list[UiPluginDescriptor]:
    """聚合 L2 内置前端插件与当前租户所有生效业务包中声明的 L1 前端插件。"""
    tenant = who.require_tenant()
    descriptors: list[UiPluginDescriptor] = list(BUILTIN_UI_PLUGINS)
    versions = await platform.packages.list_versions(tenant)
    seen_packages = sorted({rec.name for rec in versions})
    for pkg_name in seen_packages:
        active_ver = await platform.packages.active_version(tenant, pkg_name)
        if not active_ver:
            continue
        rec = next((r for r in versions if r.name == pkg_name and r.version == active_ver), None)
        if rec is None:
            continue
        try:
            manifest = parse_manifest(rec.manifest_yaml)
        except Exception:
            logger.warning("解析业务包 %s@%s 清单失败，跳过 UI 插件发现", pkg_name, active_ver, exc_info=True)
            continue
        if manifest.spec.ui is None:
            continue
        for ui_plugin in manifest.spec.ui.plugins:
            entry_url = (
                f"/v1/plugins/ui/{pkg_name}/{active_ver}/{ui_plugin.entry.lstrip('/')}"
            )
            descriptors.append(
                UiPluginDescriptor(
                    plugin_id=f"artisan:{pkg_name}:{ui_plugin.id}",
                    title=ui_plugin.title,
                    slot=ui_plugin.slot,
                    mode=ui_plugin.mode,
                    source_layer="L1_artisan",
                    package=pkg_name,
                    version=active_ver,
                    entry_url=entry_url,
                    mime_types=ui_plugin.mime_types,
                    bridge_permissions=ui_plugin.bridge_permissions,
                )
            )
    return descriptors


async def _resolve_bridge_permissions(
    platform: Platform, who: Principal, body: BridgeRpcBody,
) -> set[str]:
    """根据服务端权威清单核验该前端插件实际获批的 Bridge 权限集合（绝不信任前端自报）。"""
    all_plugins = await _tenant_ui_plugins(platform, who)
    for item in all_plugins:
        if item.plugin_id != body.plugin_id:
            continue
        if body.package is not None and item.package != body.package:
            continue
        if body.version is not None and item.version != body.version:
            continue
        return set(item.bridge_permissions)
    raise NotFoundError(f"前端插件未注册或未生效：{body.plugin_id}")


def plugins_router(platform: Platform) -> APIRouter:
    """注册微内核插件管理、动态路由分派、UI 静态托管与安全 Bridge RPC 路由。"""
    router = APIRouter()

    @router.get("/v1/plugins")
    async def list_backend_plugins(who: Caller) -> dict[str, Any]:
        """列出全部后端插件运行态及实时健康状态。"""
        who.require_tenant()
        health = await platform.plugins.health_report()
        items = []
        for rec in platform.plugins.list_plugins():
            h = health.get(rec.plugin_id)
            items.append({
                **rec.model_dump(mode="json"),
                "health": h.model_dump(mode="json") if h is not None else None,
            })
        return {"items": items}

    @router.post("/v1/plugins/{plugin_id}/unmount")
    async def unmount_backend_plugin(plugin_id: str, who: Caller) -> dict[str, Any]:
        """租户或平台管理员运行时干净卸载指定非内核后端插件。"""
        who.require_tenant_admin()
        rec = await platform.plugins.unmount(plugin_id)
        return rec.model_dump(mode="json")

    @router.get("/v1/plugins/ui")
    async def list_ui_plugins(who: Caller) -> dict[str, Any]:
        """返回本租户当前生效的全部 L2 内置前端插件与 L1 匠人同包 UI 插件清单。"""
        await platform.workspace.require_employee(who)
        items = await _tenant_ui_plugins(platform, who)
        return {"items": [item.model_dump(mode="json") for item in items]}

    @router.get("/v1/plugins/ui/{package}/{version}/{asset_path:path}")
    async def serve_ui_plugin_asset(
        package: str, version: str, asset_path: str, who: Caller,
    ) -> Response:
        """从本租户已校验不可变业务包缓存安全托管 L1 前端插件资源（强制沙盒 CSP 与防路径穿越）。"""
        tenant, _ = await platform.workspace.require_employee(who)
        norm = asset_path.strip().lstrip("/")
        if not norm or not is_safe_relative(norm) or "\\" in norm:
            raise InvalidRequestError("非法前端插件资源路径")
        resolved = await platform.packages.resolve(tenant, package, version)
        if resolved.manifest.spec.ui is None:
            raise NotFoundError("该业务包未声明前端插件目录")
        ui_root = (resolved.root / resolved.manifest.spec.ui.dir).resolve()
        target = (ui_root / PurePosixPath(norm)).resolve()
        try:
            target.relative_to(ui_root)
        except ValueError as exc:
            raise NotFoundError("资源路径越界") from exc
        if not target.is_file():
            raise NotFoundError("前端插件资源不存在")
        data = await asyncio.to_thread(target.read_bytes)
        media_type, _ = mimetypes.guess_type(target.name)
        return Response(
            content=data,
            media_type=media_type or "application/octet-stream",
            headers={
                "Content-Security-Policy": _SANDBOX_CSP,
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=31536000, immutable",
            },
        )

    @router.post("/v1/plugins/bridge/rpc")
    async def bridge_rpc(body: BridgeRpcBody, who: Caller) -> dict[str, Any]:
        """Web 主壳代理沙盒 iframe 发起的白名单 RPC 网关：强制按服务端权限清单鉴权与隔离。"""
        tenant, _ = await platform.workspace.require_employee(who)
        perms = await _resolve_bridge_permissions(platform, who, body)
        method = body.method.strip()
        params = body.params

        if method == "artifact.read":
            if "artifact.read" not in perms:
                raise PermissionDeniedError("该前端插件未获授 artifact.read 权限")
            artifact_id = str(params.get("artifact_id") or "").strip()
            if not artifact_id:
                raise InvalidRequestError("缺少 artifact_id")
            preview = await platform.trace_artifacts.preview(who, artifact_id)
            return {"ok": True, "result": preview}

        if method == "collection.read":
            if "collection.read" not in perms:
                raise PermissionDeniedError("该前端插件未获授 collection.read 权限")
            collection = str(params.get("collection") or "default").strip()
            limit = int(params.get("limit") or 50)
            rows = await platform.plugins.collections.list(who, body.plugin_id, collection, limit=limit)
            return {"ok": True, "result": {"items": [r.model_dump(mode="json") for r in rows]}}

        if method == "collection.write":
            if "collection.write" not in perms:
                raise PermissionDeniedError("该前端插件未获授 collection.write 权限")
            collection = str(params.get("collection") or "default").strip()
            data = params.get("data")
            if not isinstance(data, dict):
                raise InvalidRequestError("collection.write 需要提供 data 对象")
            rec_id = params.get("record_id")
            exp_ver = params.get("expected_version")
            saved = await platform.plugins.collections.put(
                who,
                body.plugin_id,
                collection,
                data,
                record_id=str(rec_id) if isinstance(rec_id, str) and rec_id.strip() else None,
                expected_version=int(exp_ver) if isinstance(exp_ver, int) else None,
            )
            await platform.audit.control(
                who,
                tenant,
                "plugin_bridge_write",
                {"plugin_id": body.plugin_id, "collection": collection, "record_id": saved.record_id},
            )
            return {"ok": True, "result": saved.model_dump(mode="json")}

        if method == "workspace.propose":
            if "workspace.propose" not in perms:
                raise PermissionDeniedError("该前端插件未获授 workspace.propose 权限")
            summary = str(params.get("summary") or "").strip()
            if not summary:
                raise InvalidRequestError("workspace.propose 必须提供非空 summary")
            # 写操作只能以“建议草稿”返回给主壳弹确认卡，插件绝不能绕过员工直接执行外部写操作
            await platform.audit.control(
                who,
                tenant,
                "plugin_bridge_propose",
                {"plugin_id": body.plugin_id, "summary": summary[:200]},
            )
            return {
                "ok": True,
                "result": {
                    "state": "proposed",
                    "plugin_id": body.plugin_id,
                    "summary": summary[:500],
                    "payload": params.get("payload") if isinstance(params.get("payload"), dict) else {},
                },
            }

        raise InvalidRequestError(f"不支持的 Bridge RPC 方法：{method}")

    @router.api_route(
        "/v1/plugins/ext/{plugin_id}/{subpath:path}",
        methods=["GET", "POST", "PUT", "DELETE"],
    )
    async def dispatch_plugin_ext(
        plugin_id: str, subpath: str, request: Request, who: Caller,
    ) -> dict[str, Any]:
        """动态路由表分派至已挂载后端插件（支持热插拔与卸载后即时 404）。"""
        await platform.workspace.require_employee(who)
        payload: dict[str, Any] = dict(request.query_params)
        if request.method in ("POST", "PUT", "DELETE"):
            raw = await request.body()
            if raw:
                body_obj = await request.json()
                if not isinstance(body_obj, dict):
                    raise InvalidRequestError("插件请求体必须是 JSON 对象")
                payload.update(body_obj)
        return await platform.plugins.dispatch_http(plugin_id, request.method, subpath, who, payload)

    return router
