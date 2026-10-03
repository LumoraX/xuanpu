"""控制台静态响应的安全边界：禁止嵌入，防止点击劫持。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.staticfiles import StaticFiles

if TYPE_CHECKING:
    from starlette.responses import Response
    from starlette.types import Scope


class ConsoleStaticFiles(StaticFiles):
    """为控制台文件附上浏览器真正执行的安全响应头。"""

    async def get_response(self, path: str, scope: Scope) -> Response:
        """返回静态文件；响应头覆盖 HTML、脚本及缓存验证响应。"""
        response = await super().get_response(path, scope)
        response.headers["X-Frame-Options"] = "DENY"
        return response
