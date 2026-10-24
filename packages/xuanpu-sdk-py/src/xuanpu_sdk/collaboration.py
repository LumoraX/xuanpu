"""交接、固定成果和组织决定的本人客户端，不自动确认或替换审批人。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from .client import _Http

HANDOFF_ACTIONS=frozenset(('accept','return','start','request-information','provide-information','cancel',
    'request-cancel','confirm-cancel','transfer','deliver','sign-off','dispute','reopen'))
APPROVAL_ACTIONS=frozenset(('approve','reject','return','withdraw'))


def handoff_path(identifier: str) -> str:
    """员工提供的标识始终编码为单个路径段。"""
    return '/v1/handoffs/'+quote(identifier,safe='')


class CollaborationClient:
    """保留原确认、CAS与操作号，冲突不自动改版本重新提交。"""
    _http: _Http

    async def list_handoffs(self, *, view: str='received',limit: int=50,cursor: str|None=None) -> dict[str,Any]:
        """只能查询本人视图，不提供覆盖当前员工参数。"""
        return await self._http.request('GET','/v1/handoffs',params={'view':view,'limit':limit,'cursor':cursor})

    async def get_handoff(self,identifier: str) -> dict[str,Any]:
        """读取当前责任、允许动作和已过滤历史。"""
        return await self._http.request('GET',handoff_path(identifier))

    async def submit_handoff(self,body: dict[str,Any]) -> dict[str,Any]:
        """目标与精确输入必须由调用者原生确认。"""
        return await self._http.request('POST','/v1/handoffs',json=body)

    async def handoff_action(self,identifier: str,action: str,body: dict[str,Any]) -> dict[str,Any]:
        """受理、交付、签收各用独立动作，不从任务成功自动推出。"""
        if action not in HANDOFF_ACTIONS:
            raise ValueError('未知交接操作')
        return await self._http.request('POST',handoff_path(identifier)+'/'+action,json=body)

    async def upload_handoff_result(self,identifier: str,data: bytes,*,name: str,mime_type: str,
            request_id: str,expected_version: int,shared_version: int,confirmed: bool) -> dict[str,Any]:
        """仅发布本次新成果，不接受覆盖原材料的material_id。"""
        return await self._http.request('POST',handoff_path(identifier)+'/result-uploads',content=data,
            headers={'Content-Type':mime_type},params={'name':name,'request_id':request_id,'expected_version':expected_version,
                'shared_version':shared_version,'confirmed':str(confirmed).lower()})

    async def publish_handoff_text(self,identifier: str,body: dict[str,Any]) -> dict[str,Any]:
        """正文来自本人固定助手消息，不接受自由替换或去掉原来源。"""
        return await self._http.request('POST',handoff_path(identifier)+'/result-texts',json=body)

    async def handoff_publication(self,identifier: str,publication_id: str) -> dict[str,Any]:
        """补读ready或failed，pending回执不等于成果已可见。"""
        return await self._http.request('GET',handoff_path(identifier)+'/result-publications/'+quote(publication_id,safe=''))

    async def list_approval_policies(self,*,admin: bool=False,limit: int=50,cursor: str|None=None) -> dict[str,Any]:
        """员工只读可选名称；完整规则需要当前企业级管理资格。"""
        return await self._http.request('GET','/v1/'+('admin/' if admin else '')+'approval-policies',params={'limit':limit,'cursor':cursor})

    async def create_approval_policy(self,body: dict[str,Any]) -> dict[str,Any]:
        """管理员明确配置一到三步，不根据可见范围猜职责。"""
        return await self._http.request('POST','/v1/admin/approval-policies',json=body)

    async def change_approval_policy(self,identifier: str,body: dict[str,Any]) -> dict[str,Any]:
        """追加修订，不修改旧请求的已冻结审批人。"""
        return await self._http.request('PATCH','/v1/admin/approval-policies/'+quote(identifier,safe=''),json=body)

    async def list_approval_requests(self,*,limit: int=50,cursor: str|None=None) -> dict[str,Any]:
        """本人申请与冻结步骤元数据按当前身份过滤。"""
        return await self._http.request('GET','/v1/approval-requests',params={'limit':limit,'cursor':cursor})

    async def get_approval_request(self,identifier: str) -> dict[str,Any]:
        """决定理由正文仍按当前成果及完整来源权限过滤。"""
        return await self._http.request('GET','/v1/approval-requests/'+quote(identifier,safe=''))

    async def submit_approval_request(self,body: dict[str,Any]) -> dict[str,Any]:
        """只引用预配策略与固定成果，客户端不能覆盖审批人。"""
        return await self._http.request('POST','/v1/approval-requests',json=body)

    async def approval_decision(self,identifier: str,action: str,body: dict[str,Any]) -> dict[str,Any]:
        """组织决定不授予工具调用或发起人签收权限。"""
        if action not in APPROVAL_ACTIONS:
            raise ValueError('未知组织决定')
        return await self._http.request('POST','/v1/approval-requests/'+quote(identifier,safe='')+'/'+action,json=body)

    async def collaboration_notifications(self,*,after_id: int=0,limit: int=100) -> dict[str,Any]:
        """补读来源自有站内动态，序号独立于个人提醒及共享动态。"""
        return await self._http.request('GET','/v1/collaboration-notifications',params={'after_id':after_id,'limit':limit})

    async def read_collaboration_notification(self,identifier: str,body: dict[str,Any]) -> dict[str,Any]:
        """本人明确标读保留原操作号及版本。"""
        return await self._http.request('POST','/v1/collaboration-notifications/'+quote(identifier,safe='')+'/read',json=body)
