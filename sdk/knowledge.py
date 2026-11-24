"""本人知识、受保护问答、工作记忆（R4-D）与能力结果、验收核实（R4-F）SDK。

每个方法只对应一个公开服务端动作：不替员工自动选依据、确认记忆或补核实结论；
标识只占一个路径段（斜杠被转义）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from .client import _Http

MEMORY_ACTIONS = ('confirm', 'reject', 'delete')


def _seg(identifier: str) -> str:
    """路径段转义。"""
    return quote(identifier, safe='')


class KnowledgeClient:
    """本人知识来源、问答上下文与工作记忆。"""

    _http: _Http

    async def register_knowledge_source(self, item_id: str, revision: int) -> dict[str, Any]:
        """登记本人一份工作区文件的明确版本。"""
        return await self._http.request('POST', '/v1/me/knowledge/sources',
                                        json={'item_id': item_id, 'revision': revision})

    async def register_shared_knowledge_source(self, matter_id: str, material_id: str,
                                               revision: int) -> dict[str, Any]:
        """登记本人此刻有权读的共享事项材料的明确版本（每次使用按当前共享授权复核）。"""
        return await self._http.request('POST', '/v1/me/knowledge/sources', json={
            'kind': 'shared_material', 'matter_id': matter_id, 'material_id': material_id, 'revision': revision})

    async def list_knowledge_sources(self) -> dict[str, Any]:
        """本人来源（含 stale 与 has_update）。"""
        return await self._http.request('GET', '/v1/me/knowledge/sources')

    async def search_knowledge(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        """只在本人此刻可读来源里字面检索。"""
        return await self._http.request('POST', '/v1/me/knowledge/search', json={'query': query, 'limit': limit})

    async def change_knowledge_source(self, identifier: str, action: str, expected_version: int) -> dict[str, Any]:
        """刷新（refresh）或删除（delete）来源。"""
        if action not in ('refresh', 'delete'):
            raise ValueError('action 只能是 refresh 或 delete')
        return await self._http.request('POST', f'/v1/me/knowledge/sources/{_seg(identifier)}/{action}',
                                        json={'expected_version': expected_version})

    async def create_knowledge_context(self, request_id: str, pins: list[dict[str, Any]]) -> dict[str, Any]:
        """以明确选定的依据（source_id、ordinal）建受保护问答上下文。"""
        return await self._http.request('POST', '/v1/me/knowledge/contexts',
                                        json={'request_id': request_id, 'pins': pins})

    async def list_knowledge_contexts(self) -> dict[str, Any]:
        """本人问答上下文元数据。"""
        return await self._http.request('GET', '/v1/me/knowledge/contexts')

    async def read_knowledge_context(self, identifier: str) -> dict[str, Any]:
        """依据正文与已有回答；来源失效时服务端返回 knowledge_source_revoked。"""
        return await self._http.request('GET', f'/v1/me/knowledge/contexts/{_seg(identifier)}')

    async def ask_knowledge(self, identifier: str, request_id: str, question: str) -> dict[str, Any]:
        """只依据固定依据回答，带引用与 grounded 标记。"""
        return await self._http.request('POST', f'/v1/me/knowledge/contexts/{_seg(identifier)}/ask',
                                        json={'request_id': request_id, 'question': question})

    async def close_knowledge_context(self, identifier: str, expected_version: int) -> dict[str, Any]:
        """结束问答（回答正文清掉）。"""
        return await self._http.request('POST', f'/v1/me/knowledge/contexts/{_seg(identifier)}/close',
                                        json={'expected_version': expected_version})

    async def list_work_memory(self) -> dict[str, Any]:
        """本人生效中与待确认的记忆。"""
        return await self._http.request('GET', '/v1/me/work-memory')

    async def propose_work_memory(self, body: dict[str, Any]) -> dict[str, Any]:
        """提出候选（须带 citation；本人确认后才生效）。"""
        return await self._http.request('POST', '/v1/me/work-memory/proposals', json=body)

    async def assert_work_memory(self, body: dict[str, Any]) -> dict[str, Any]:
        """本人手工录入的陈述（标明非已核实）。"""
        return await self._http.request('POST', '/v1/me/work-memory/assertions', json=body)

    async def decide_work_memory(self, identifier: str, action: str, expected_version: int) -> dict[str, Any]:
        """确认（confirm）、拒绝（reject）或删除（delete）一条记忆。"""
        if action not in MEMORY_ACTIONS:
            raise ValueError('action 只能是 confirm、reject 或 delete')
        return await self._http.request('POST', f'/v1/me/work-memory/{_seg(identifier)}/{action}',
                                        json={'expected_version': expected_version})

    async def correct_work_memory(self, identifier: str, expected_version: int, text: str) -> dict[str, Any]:
        """更正（生成新修订）。"""
        return await self._http.request('POST', f'/v1/me/work-memory/{_seg(identifier)}/correct',
                                        json={'expected_version': expected_version, 'text': text})


class OutcomeClient:
    """能力运行结果统计与验收核实。"""

    _http: _Http

    async def my_capability_outcomes(self, *, since: float | None = None, until: float | None = None,
                                     limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """本人能力任务的状态、完成率与用量（总数独立于当前页）。"""
        params: dict[str, Any] = {'limit': limit, 'offset': offset}
        params.update({k: v for k, v in (('since', since), ('until', until)) if v is not None})
        return await self._http.request('GET', '/v1/me/capability-outcomes', params=params)

    async def admin_capability_outcomes(self, *, since: float | None = None,
                                        until: float | None = None) -> dict[str, Any]:
        """按能力与修订的计数与用量（仅管理员，不含正文）。"""
        params = {k: v for k, v in (('since', since), ('until', until)) if v is not None}
        return await self._http.request('GET', '/v1/admin/capability-outcomes', params=params)

    async def create_acceptance_batch(self, body: dict[str, Any]) -> dict[str, Any]:
        """冻结一个验收批次（仅管理员）。"""
        return await self._http.request('POST', '/v1/admin/acceptance-batches', json=body)

    async def get_acceptance_batch(self, identifier: str) -> dict[str, Any]:
        """批次详情与核实通过率。"""
        return await self._http.request('GET', f'/v1/acceptance-batches/{_seg(identifier)}')

    async def observe_acceptance(self, identifier: str, body: dict[str, Any]) -> dict[str, Any]:
        """追加一条核实结论（不改任务、交接或审批状态）。"""
        return await self._http.request('POST', f'/v1/acceptance-batches/{_seg(identifier)}/observations', json=body)
