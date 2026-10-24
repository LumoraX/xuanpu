"""共享资料客户端只封装当前员工的原生接口，不增加授权或自动确认。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from .client import _Http

BASE = '/v1/shared-matters'


def shared_path(identifier: str) -> str:
    """事项编号始终作为一个路径段，不接受任意外部URL。"""
    return BASE + '/' + quote(identifier, safe='')


class SharedMattersClient:
    """原确认、版本、操作号及固定材料引用均由调用者明确给出。"""
    _http: _Http

    async def create_shared_matter(self, body: dict[str, Any]) -> dict[str, Any]:
        """不自动补confirmed，保持原请求身份。"""
        return await self._http.request('POST', BASE, json=body)

    async def list_shared_matters(self, *, search: str = '', limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        """返回权限过滤后的独立总数与稳定游标。"""
        return await self._http.request('GET', BASE, params={'search': search, 'limit': limit, 'cursor': cursor})

    async def get_shared_matter(self, identifier: str) -> dict[str, Any]:
        """当前可见投影及原生可用动作。"""
        return await self._http.request('GET', shared_path(identifier))

    async def change_shared_matter(self, identifier: str, body: dict[str, Any]) -> dict[str, Any]:
        """冲突不自动提升版本或覆盖。"""
        return await self._http.request('PATCH', shared_path(identifier), json=body)

    async def list_shared_members(self, identifier: str, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        """限定资料读者不能因SDK获得完整参与者列表。"""
        return await self._http.request('GET', shared_path(identifier) + '/members', params={'limit': limit, 'cursor': cursor})

    async def invite_shared_member(self, identifier: str, body: dict[str, Any]) -> dict[str, Any]:
        """明确员工或可选部门及资料范围。"""
        return await self._http.request('POST', shared_path(identifier) + '/members', json=body)

    async def revoke_shared_member(self, identifier: str, member_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """当前授权撤销，不能隐式恢复原grant。"""
        return await self._http.request('POST', shared_path(identifier) + '/members/' + quote(member_id, safe='') + '/revoke', json=body)

    async def transfer_shared_owner(self, identifier: str, body: dict[str, Any]) -> dict[str, Any]:
        """唯一负责人需要独立原生确认。"""
        return await self._http.request('POST', shared_path(identifier) + '/transfer-owner', json=body)

    async def shared_matter_events(self, identifier: str, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        """只补读当前仍可见的正文历史。"""
        return await self._http.request('GET', shared_path(identifier) + '/events', params={'limit': limit, 'cursor': cursor})

    async def list_shared_materials(self, identifier: str, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        """读取精确revision及SHA，不得到对象永久URL。"""
        return await self._http.request('GET', shared_path(identifier) + '/materials', params={'limit': limit, 'cursor': cursor})

    async def publish_shared_material(self, identifier: str, body: dict[str, Any]) -> dict[str, Any]:
        """原样提交本人明确的私人文件版本。"""
        return await self._http.request('POST', shared_path(identifier) + '/materials', json=body)

    async def publish_shared_text(self, identifier: str, body: dict[str, Any]) -> dict[str, Any]:
        """正文和来源取自本人固定助手消息，不接受无来源替换。"""
        return await self._http.request('POST', shared_path(identifier) + '/material-texts', json=body)

    async def upload_shared_material(self, identifier: str, data: bytes, *, name: str, mime_type: str,
                                     request_id: str, expected_version: int, confirmed: bool) -> dict[str, Any]:
        """调用者明确确认后上传固定字节，重试保持同一操作号。"""
        return await self._http.request('POST', shared_path(identifier) + '/material-uploads', content=data,
            headers={'Content-Type': mime_type}, params={'name': name, 'request_id': request_id,
                'expected_version': expected_version, 'confirmed': str(confirmed).lower()})

    async def shared_publication(self, identifier: str, publication_id: str) -> dict[str, Any]:
        """pending回执不冒充ready，必须补读当前状态。"""
        return await self._http.request('GET', shared_path(identifier) + '/publications/' + quote(publication_id, safe=''))

    async def download_shared_material(self, identifier: str, material_id: str, *, revision: int) -> bytes:
        """有界下载验证SHA后返回，不写本地或私人工作台文件。"""
        return await self._http.download(shared_path(identifier) + '/materials/' + quote(material_id, safe='') + '/content',
            params={'revision': revision})

    async def withdraw_shared_material(self, identifier: str, material_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """撤回副本而不删除原件或历史证据。"""
        return await self._http.request('POST', shared_path(identifier) + '/materials/' + quote(material_id, safe='') + '/withdraw', json=body)

    async def create_shared_context(self, body: dict[str, Any]) -> dict[str, Any]:
        """明确选择当前可见正文和精确材料版本。"""
        return await self._http.request('POST', '/v1/shared-contexts', json=body)

    async def get_shared_context(self, identifier: str) -> dict[str, Any]:
        """状态与版本不包含失效正文。"""
        return await self._http.request('GET', '/v1/shared-contexts/' + quote(identifier, safe=''))

    async def list_shared_contexts(self, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        """本人阅读状态目录不包含来源正文。"""
        return await self._http.request('GET', '/v1/shared-contexts', params={'limit': limit, 'cursor': cursor})

    async def read_shared_context(self, identifier: str) -> dict[str, Any]:
        """每次补读重新检查来源和原授权。"""
        return await self._http.request('GET', '/v1/shared-contexts/' + quote(identifier, safe='') + '/read')

    async def close_shared_context(self, identifier: str, body: dict[str, Any]) -> dict[str, Any]:
        """结束旧阅读后需要重新选择，不复用旧上下文。"""
        return await self._http.request('POST', '/v1/shared-contexts/' + quote(identifier, safe='') + '/close', json=body)

    async def shared_source_reply(self, identifier: str, text: str, *, request_id: str) -> dict[str, Any]:
        """共享问答仅走受保护文本入口，不进入普通业务路由。"""
        return await self._http.request('POST', '/v1/assistant/route', json={
            'shared_context_id': identifier, 'text': text, 'request_id': request_id})

    async def shared_notifications(self, *, after_id: int = 0, limit: int = 100) -> dict[str, Any]:
        """当前来源过滤后的动态及独立未读总数。"""
        return await self._http.request('GET', '/v1/shared-notifications', params={'after_id': after_id, 'limit': limit})

    async def read_shared_notification(self, identifier: str, body: dict[str, Any]) -> dict[str, Any]:
        """明确标读，撤权后不通过旧回执读取来源。"""
        return await self._http.request('POST', '/v1/shared-notifications/' + quote(identifier, safe='') + '/read', json=body)
