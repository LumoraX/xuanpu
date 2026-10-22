"""本人连接SDK：可查询、读取和撤销；同意必须由浏览器本人确认。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from .client import _Http


def connection_path(identifier: str) -> str:
    """标识只占单一路径段，不能覆盖主体或查询。"""
    return '/v1/me/connections/'+quote(identifier,safe='')


class ConnectionClient:
    """复用主客户端的固定身份，不提供创建/扩大consent接口。"""
    _http: _Http
    _connection_base: str

    async def list_connections(self) -> dict[str,Any]:
        """只读当前身份的连接元数据与同意状态。"""
        return await self._http.request('GET','/v1/me/connections')

    async def read_connection(self,connection_id: str,*,operation: str='self_profile.read') -> dict[str,Any]:
        """使用已存在的有效委托，不能传userid/token覆盖。"""
        return await self._http.request('POST',connection_path(connection_id)+'/read',
                                        json={'operation':operation,'parameters':{}})

    async def revoke_connection_consent(self,connection_id: str,consent_id: str,*,
                                        request_id: str,expected_version: int) -> dict[str,Any]:
        """撤销当前员工的同意，保留稳定请求与CAS。"""
        return await self._http.request('POST',connection_path(connection_id)+'/consents/'+
            quote(consent_id,safe='')+'/revoke',json={'request_id':request_id,'expected_version':expected_version})

    def connection_confirmation_url(self) -> str:
        """只返回玄圃页面入口；机器身份不能自动批准个人同意。"""
        return self._connection_base.rstrip('/')+'/console/?connections=1'
