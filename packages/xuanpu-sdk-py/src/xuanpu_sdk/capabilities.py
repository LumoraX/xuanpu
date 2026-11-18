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

    async def assign_capability(self, identifier: str, body: dict[str, Any]) -> dict[str, Any]:
        """把能力的明确修订分配给员工或岗位/部门（R4-B）；只授权使用能力，带原 request_id。"""
        return await self._http.request('POST', f'/v1/capabilities/{quote(identifier, safe="")}/assignments', json=body)

    async def list_capability_assignments(self, identifier: str) -> dict[str, Any]:
        """某能力的全部分配（含已撤销），仅管理员。"""
        return await self._http.request('GET', f'/v1/capabilities/{quote(identifier, safe="")}/assignments')

    async def revoke_capability_assignment(self, identifier: str, body: dict[str, Any]) -> dict[str, Any]:
        """按版本撤销分配（body 含 request_id 与 expected_version），下一次调用即被拒。"""
        return await self._http.request('POST', f'/v1/capability-assignments/{quote(identifier, safe="")}/revoke', json=body)

    async def evaluate_capability(self, body: dict[str, Any]) -> dict[str, Any]:
        """发起一次隔离评测（R4-C）；body 含 request_id、capability_id、installation_id、suite。"""
        return await self._http.request('POST', '/v1/capability-evaluations', json=body)

    async def get_capability_evaluation(self, identifier: str) -> dict[str, Any]:
        """评测状态与逐用例判定。"""
        return await self._http.request('GET', f'/v1/capability-evaluations/{quote(identifier, safe="")}')

    async def preview_capability_release(self, identifier: str, body: dict[str, Any]) -> dict[str, Any]:
        """只读发布预览：证据核对、名单变化与确认摘要。"""
        return await self._http.request('POST', f'/v1/capabilities/{quote(identifier, safe="")}/release-previews', json=body)

    async def release_capability(self, identifier: str, body: dict[str, Any]) -> dict[str, Any]:
        """按预览摘要确认发布给固定员工名单（body 含 preview_digest、supersedes、confirmed=true）。"""
        return await self._http.request('POST', f'/v1/capabilities/{quote(identifier, safe="")}/releases', json=body)

    async def list_capability_releases(self, identifier: str) -> dict[str, Any]:
        """某能力的全部发布。"""
        return await self._http.request('GET', f'/v1/capabilities/{quote(identifier, safe="")}/releases')

    async def change_capability_release(self, identifier: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
        """停止（retire）或撤销（revoke）一次发布，body 含 request_id 与 expected_version。"""
        if action not in ('retire', 'revoke'):
            raise ValueError('action 只能是 retire 或 revoke')
        return await self._http.request('POST', f'/v1/capability-releases/{quote(identifier, safe="")}/{action}', json=body)
