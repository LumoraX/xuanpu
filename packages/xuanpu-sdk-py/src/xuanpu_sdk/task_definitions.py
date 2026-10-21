"""本人后台安排及权威核实客户端，复用原认证和传输。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .models import TaskDefinition, WorkTask

if TYPE_CHECKING:
    from .client import _Http


def definition_path(identifier: str) -> str:
    """标识只占一个路径段，不允许嵌入查询或其他资源路径。"""
    return '/v1/task-definitions/'+quote(identifier,safe='')


class TaskDefinitionClient:
    """主客户端继承同一传输，不另建身份或自动确认。"""
    _http: _Http

    async def create_task_definition(self, request: dict[str,Any]) -> TaskDefinition:
        """原请求必须包含明确确认及稳定request_id。"""
        return TaskDefinition.from_json(await self._http.request('POST','/v1/task-definitions',json=dict(request)))

    async def get_task_definition(self, definition_id: str) -> TaskDefinition:
        """补读本人已确认安排。"""
        return TaskDefinition.from_json(await self._http.request('GET',definition_path(definition_id)))

    async def list_task_definitions(self, *, after_id: str | None=None,limit: int=100) -> list[TaskDefinition]:
        """只传服务端分页游标，不在客户端扩张身份。"""
        params: dict[str,Any]={'limit':limit}
        if after_id is not None:params['after_id']=after_id
        body=await self._http.request('GET','/v1/task-definitions',params=params)
        return [TaskDefinition.from_json(item) for item in body['items']]

    async def update_task_definition(self, definition_id: str, request: dict[str,Any]) -> TaskDefinition:
        """重新确认完整范围及当前state_version，重复请求不换标识。"""
        return TaskDefinition.from_json(await self._http.request('PUT',definition_path(definition_id),json=dict(request)))

    async def _definition_action(self, definition_id: str, action: str, request_id: str,
                                 expected_version: int, stop_running: bool) -> TaskDefinition:
        """管理操作保持同一请求回执及明确在途策略。"""
        return TaskDefinition.from_json(await self._http.request('POST',definition_path(definition_id)+'/'+action,
            json={'request_id':request_id,'expected_version':expected_version,'stop_running':stop_running}))

    async def pause_task_definition(self, definition_id: str, *, request_id: str,
                                    expected_version: int,stop_running: bool=False) -> TaskDefinition:
        """暂停默认保留正在执行的attempt。"""
        return await self._definition_action(definition_id,'pause',request_id,expected_version,stop_running)

    async def resume_task_definition(self, definition_id: str, *, request_id: str,expected_version: int) -> TaskDefinition:
        """恢复不重新生成授权范围或重置预算。"""
        return await self._definition_action(definition_id,'resume',request_id,expected_version,False)

    async def cancel_task_definition(self, definition_id: str, *, request_id: str,expected_version: int) -> TaskDefinition:
        """取消提出停止要求，不声称外部业务已撤销。"""
        return await self._definition_action(definition_id,'cancel',request_id,expected_version,True)

    async def list_occurrences(self, definition_id: str, *, after_id: str | None=None,limit: int=100) -> list[dict[str,Any]]:
        """稳定分页补读原发生事实及唯一任务引用。"""
        params: dict[str,Any]={'limit':limit}
        if after_id is not None:params['after_id']=after_id
        return list((await self._http.request('GET',definition_path(definition_id)+'/occurrences',params=params))['items'])

    async def create_capability_task(self, request: dict[str,Any]) -> WorkTask:
        """显式选择已发布只读入口，不要求伪造附件。"""
        return WorkTask.from_json(await self._http.request('POST','/v1/work-tasks/from-capability',json=dict(request)))

    async def reconcile_task_effect(self, task_id: str, action_id: str, *, request_id: str,expected_version: int) -> dict[str,Any]:
        """只请求已登记权威查询，没有通用重试或自由文本证明。"""
        path='/v1/work-tasks/'+quote(task_id,safe='')+'/effects/'+quote(action_id,safe='')+'/reconcile'
        return await self._http.request('POST',path,json={'request_id':request_id,'expected_version':expected_version})
