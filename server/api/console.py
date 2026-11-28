"""前端单页路由与静态资源安全边界：无 .html 干净路径、旧 /console/* 重定向兼容与防点击劫持。"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI
    from starlette.responses import Response
    from starlette.types import Scope

WORKSPACE_TABS: frozenset[str] = frozenset({
    "sessions",
    "functions",
    "marketplace",
    "packages",
    "secrets",
    "capabilities",
    "audit",
    "usage",
    "users",
})
"""完整工作台与管理后台支持的子路径页签。"""

_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-cache",
}


class ConsoleStaticFiles(StaticFiles):
    """静态资源挂载：仅提供 JS/CSS 等资源文件，附带安全头与指纹缓存策略。"""

    async def get_response(self, path: str, scope: Scope) -> Response:
        """返回静态资源；拒绝直接暴露 HTML 入口文件。"""
        clean = path.strip("/")
        if not clean or clean.lower().endswith(".html"):
            raise StarletteHTTPException(status_code=404)
        response = await super().get_response(path, scope)
        response.headers["X-Frame-Options"] = "DENY"
        query_bytes = scope.get("query_string", b"")
        if b"v=" in query_bytes:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


def _redirect(
    target: str,
    request: Request,
    *,
    drop_params: frozenset[str] = frozenset(),
) -> RedirectResponse:
    """保留除 drop_params 之外的查询参数并执行 307 跳转。"""
    raw_query = request.url.query
    if raw_query:
        pairs = [
            (k, v)
            for k, v in parse_qsl(raw_query, keep_blank_values=True)
            if k not in drop_params
        ]
        if pairs:
            target = f"{target}?{urlencode(pairs)}"
    return RedirectResponse(url=target, status_code=307, headers=dict(_SECURITY_HEADERS))


def _html_response(path: Path) -> FileResponse:
    """返回 HTML 页面并附加禁嵌入与协商缓存头。"""
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(
        path,
        media_type="text/html; charset=utf-8",
        headers=dict(_SECURITY_HEADERS),
    )


def register_console_routes(app: FastAPI, console_dir: Path) -> None:
    """注册前端干净路由（/、/workspace/*、/admin/*、/assets/*）与旧 /console/* 兼容跳转。"""
    resolved = console_dir.resolve()
    entry_html = resolved / "entry.html"
    index_html = resolved / "index.html"
    asset_files = ConsoleStaticFiles(directory=str(resolved), html=False)

    def serve_entry() -> FileResponse:
        return _html_response(entry_html if entry_html.is_file() else index_html)

    def serve_workspace() -> FileResponse:
        return _html_response(index_html)

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def _root_entry(request: Request) -> Response:
        """根路径直出小院极简入口；带 admin=1 / classic=1 时跳转到对应干净路径。"""
        if request.query_params.get("admin") == "1":
            return _redirect("/admin", request, drop_params=frozenset({"admin", "classic"}))
        if request.query_params.get("classic") == "1":
            return _redirect("/workspace", request, drop_params=frozenset({"classic"}))
        return serve_entry()

    @app.api_route("/courtyard", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/courtyard/", methods=["GET", "HEAD"], include_in_schema=False)
    async def _courtyard_alias(request: Request) -> RedirectResponse:
        """小院别名跳转至根路径。"""
        return _redirect("/", request)

    @app.api_route("/workspace", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/workspace/", methods=["GET", "HEAD"], include_in_schema=False)
    async def _workspace_root(request: Request) -> Response:
        """完整工作台入口。"""
        if request.query_params.get("admin") == "1":
            return _redirect("/admin", request, drop_params=frozenset({"admin", "classic"}))
        return serve_workspace()

    @app.api_route("/workspace/{tab}", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/workspace/{tab}/", methods=["GET", "HEAD"], include_in_schema=False)
    async def _workspace_tab(tab: str) -> Response:
        """完整工作台各功能页签路径。"""
        if tab not in WORKSPACE_TABS:
            raise HTTPException(status_code=404)
        return serve_workspace()

    @app.api_route("/admin", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/admin/", methods=["GET", "HEAD"], include_in_schema=False)
    async def _admin_root() -> Response:
        """管理员后台入口。"""
        return serve_workspace()

    @app.api_route("/admin/{tab}", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/admin/{tab}/", methods=["GET", "HEAD"], include_in_schema=False)
    async def _admin_tab(tab: str) -> Response:
        """管理员后台各功能页签路径。"""
        if tab not in WORKSPACE_TABS:
            raise HTTPException(status_code=404)
        return serve_workspace()

    # ---------- 旧路径与 *.html 兼容跳转 ----------

    @app.api_route("/entry.html", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/console/entry.html", methods=["GET", "HEAD"], include_in_schema=False)
    async def _legacy_entry_html(request: Request) -> RedirectResponse:
        """兼容旧版 entry.html 链接。"""
        return _redirect("/", request)

    @app.api_route("/index.html", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/console/index.html", methods=["GET", "HEAD"], include_in_schema=False)
    async def _legacy_index_html(request: Request) -> RedirectResponse:
        """兼容旧版 index.html 链接。"""
        if request.query_params.get("admin") == "1":
            return _redirect("/admin", request, drop_params=frozenset({"admin", "classic"}))
        return _redirect("/workspace", request, drop_params=frozenset({"classic"}))

    @app.api_route("/console", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/console/", methods=["GET", "HEAD"], include_in_schema=False)
    async def _legacy_console_root(request: Request) -> RedirectResponse:
        """兼容旧版 /console/ 入口，按参数导向新路由。"""
        if request.query_params.get("admin") == "1":
            return _redirect("/admin", request, drop_params=frozenset({"admin", "classic"}))
        if request.query_params.get("classic") == "1":
            return _redirect("/workspace", request, drop_params=frozenset({"classic"}))
        return _redirect("/", request)

    @app.api_route("/console/{subpath:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def _legacy_console_subpath(subpath: str, request: Request) -> Response:
        """兼容旧版 /console/<页签> 重定向与 /console/<静态资源> 读取。"""
        clean = subpath.rstrip("/")
        if clean == "courtyard":
            return _redirect("/", request)
        if clean in WORKSPACE_TABS:
            return _redirect(f"/workspace/{clean}", request)
        if clean in ("workspace", "admin"):
            return _redirect(f"/{clean}", request)
        if clean.startswith(("workspace/", "admin/")):
            head, tail = clean.split("/", 1)
            if tail in WORKSPACE_TABS:
                return _redirect(f"/{head}/{tail}", request)
        return await asset_files.get_response(subpath, request.scope)

    app.mount("/assets", asset_files, name="assets")


__all__ = ["WORKSPACE_TABS", "ConsoleStaticFiles", "register_console_routes"]
