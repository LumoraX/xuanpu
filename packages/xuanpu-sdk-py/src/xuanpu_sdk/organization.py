"""组织SDK只封装公开接口，身份固定于客户端，不替调用者自动确认。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import quote

if TYPE_CHECKING:
    from .client import _Http

Collection = Literal['units', 'positions', 'memberships', 'roles']
BASE = '/v1/admin/organization'


def organization_path(kind: Collection, identifier: str | None = None) -> str:
    """固定集合名及单路径段编码，不接受任意URL。"""
    if kind not in ('units', 'positions', 'memberships', 'roles'):
        raise ValueError('未知组织集合')
    return BASE + '/' + kind + (('/' + quote(identifier, safe='')) if identifier is not None else '')


class OrganizationClient:
    """部门配置、来源预览与选中批次共享服务端权限和回执。"""
    _http: _Http

    async def organization_me(self) -> dict[str, Any]:
        """本人当前任职与业务职责。"""
        return await self._http.request('GET', '/v1/organization/me')

    async def organization_directory(self, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        """权限过滤后的最小目录及独立总数。"""
        return await self._http.request('GET', '/v1/organization/directory', params={'limit': limit, 'cursor': cursor})

    async def list_organization(self, kind: Collection, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        """管理范围内的分页组织实体。"""
        return await self._http.request('GET', organization_path(kind), params={'limit': limit, 'cursor': cursor})

    async def get_organization(self, kind: Collection, identifier: str) -> dict[str, Any]:
        """跨范围对象和不存在对象同样由服务端拒绝。"""
        return await self._http.request('GET', organization_path(kind, identifier))

    async def create_organization(self, kind: Collection, body: dict[str, Any]) -> dict[str, Any]:
        """原样发送固定操作号，不自动授予任何职责。"""
        return await self._http.request('POST', organization_path(kind), json=body)

    async def change_organization(self, kind: Collection, identifier: str, body: dict[str, Any]) -> dict[str, Any]:
        """原样发送CAS版本；冲突交由调用者刷新处理。"""
        return await self._http.request('PATCH', organization_path(kind, identifier), json=body)

    async def organization_authority(self) -> dict[str, Any]:
        """查询显式字段来源规则。"""
        return await self._http.request('GET', BASE + '/authority')

    async def preview_organization_authority(self, body: dict[str, Any]) -> dict[str, Any]:
        """先预览来源切换影响，不自动确认。"""
        return await self._http.request('POST', BASE + '/authority/previews', json=body)

    async def apply_organization_authority(self, identifier: str, body: dict[str, Any]) -> dict[str, Any]:
        """明确确认指定预览版本。"""
        return await self._http.request('POST', BASE + '/authority/previews/' + quote(identifier, safe='') + '/apply', json=body)

    async def preview_organization_import(self, body: dict[str, Any]) -> dict[str, Any]:
        """经已配置的安装采集目录并冻结差异。"""
        return await self._http.request('POST', BASE + '/import-previews', json=body)

    async def get_organization_import(self, identifier: str) -> dict[str, Any]:
        """查询范围、完整性、逐页证据及字段冲突。"""
        return await self._http.request('GET', BASE + '/import-previews/' + quote(identifier, safe=''))

    async def apply_organization_import(self, identifier: str, body: dict[str, Any]) -> dict[str, Any]:
        """只提交调用者明确选中的批次，不补全遗漏变更。"""
        return await self._http.request('POST', BASE + '/import-previews/' + quote(identifier, safe='') + '/apply', json=body)
