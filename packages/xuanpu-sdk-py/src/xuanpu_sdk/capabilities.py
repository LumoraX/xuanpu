"""能力登记与安装SDK，不自动补确认、分配或激活。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from .client import _Http


class CapabilityClient:
    """每个方法只对应一个公开服务端动作，身份固定于客户端。"""
    _http: _Http

    async def my_capabilities(self) -> dict[str, Any]:
        """本人已可运行集合；A阶段明确为空。"""
        return await self._http.request('GET', '/v1/me/capabilities')

    async def list_capabilities(self, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        """管理员目录与当前安装状态，不自动遍历所有页。"""
        params: dict[str, Any] = {'limit': limit}
        if cursor is not None:
            params['cursor'] = cursor
        return await self._http.request('GET', '/v1/capabilities', params=params)

    async def register_capability(self, body: dict[str, Any]) -> dict[str, Any]:
        """登记不可变描述，调用者提供原request_id。"""
        return await self._http.request('POST', '/v1/capabilities', json=body)

    async def preview_capability_install(self, identifier: str, body: dict[str, Any]) -> dict[str, Any]:
        """只读预检后保存固定依据，不执行能力。"""
        return await self._http.request('POST', f'/v1/capabilities/{quote(identifier, safe="")}/install-previews', json=body)

    async def install_capability(self, identifier: str, body: dict[str, Any]) -> dict[str, Any]:
        """仅提交调用者明确确认的精确预检。"""
        return await self._http.request('POST', f'/v1/capabilities/{quote(identifier, safe="")}/installations', json=body)

    async def change_capability_installation(self, identifier: str, body: dict[str, Any]) -> dict[str, Any]:
        """停用保留历史；ready仍受服务端发布门禁。"""
        return await self._http.request('POST', f'/v1/installations/{quote(identifier, safe="")}/state', json=body)
