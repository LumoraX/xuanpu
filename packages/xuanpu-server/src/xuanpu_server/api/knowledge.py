"""本人知识来源接口（R4-D0）：登记、列表、检索、刷新、删除；只给员工本人。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter
from pydantic import JsonValue  # noqa: TC002 —— FastAPI 解析响应类型

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI 解析认证依赖
from xuanpu_server.knowledge.memory import (  # noqa: TC001 —— FastAPI 解析请求模型
    AssertInput,
    CorrectInput,
    DecideInput,
    ProposeInput,
)
from xuanpu_server.knowledge.service import (  # noqa: TC001 —— FastAPI 解析请求模型
    RegisterSource,
    SearchInput,
    SourceChange,
)

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


def knowledge_router(platform: Platform) -> APIRouter:
    """检索只在本人此刻可读的来源里进行。"""
    router = APIRouter(prefix='/v1/me/knowledge')

    @router.post('/sources', status_code=201)
    async def register(body: RegisterSource, who: Caller) -> dict[str, JsonValue]:
        """登记本人一份文件的明确版本。"""
        return await platform.knowledge.register(who, body)

    @router.get('/sources')
    async def sources(who: Caller) -> dict[str, JsonValue]:
        """本人来源列表。"""
        return await platform.knowledge.sources(who)

    @router.post('/search')
    async def search(body: SearchInput, who: Caller) -> dict[str, JsonValue]:
        """字面关键词检索，命中带来源、版本与位置。"""
        return await platform.knowledge.search(who, body)

    @router.post('/sources/{source_id}/refresh')
    async def refresh(source_id: str, body: SourceChange, who: Caller) -> dict[str, JsonValue]:
        """把来源换成文件的当前版本（旧来源删除）。"""
        return await platform.knowledge.refresh(who, source_id, body)

    @router.post('/sources/{source_id}/delete')
    async def delete(source_id: str, body: SourceChange, who: Caller) -> dict[str, JsonValue]:
        """删除来源并清掉文本块。"""
        return await platform.knowledge.delete(who, source_id, body)

    memory = APIRouter(prefix='/v1/me/work-memory')

    @memory.get('')
    async def memories(who: Caller) -> dict[str, JsonValue]:
        """本人生效中与待确认的记忆。"""
        return await platform.work_memory.listing(who)

    @memory.post('/proposals', status_code=201)
    async def propose(body: ProposeInput, who: Caller) -> dict[str, JsonValue]:
        """提出候选（须引用本人可读的来源；本人确认后才生效）。"""
        return await platform.work_memory.propose(who, body)

    @memory.post('/assertions', status_code=201)
    async def assert_fact(body: AssertInput, who: Caller) -> dict[str, JsonValue]:
        """本人手工录入的陈述（标明非已核实）。"""
        return await platform.work_memory.assert_fact(who, body)

    @memory.post('/{memory_id}/confirm')
    async def confirm(memory_id: str, body: DecideInput, who: Caller) -> dict[str, JsonValue]:
        """确认候选。"""
        return await platform.work_memory.confirm(who, memory_id, body)

    @memory.post('/{memory_id}/reject')
    async def reject(memory_id: str, body: DecideInput, who: Caller) -> dict[str, JsonValue]:
        """拒绝候选。"""
        return await platform.work_memory.reject(who, memory_id, body)

    @memory.post('/{memory_id}/correct')
    async def correct(memory_id: str, body: CorrectInput, who: Caller) -> dict[str, JsonValue]:
        """更正（生成新修订）。"""
        return await platform.work_memory.correct(who, memory_id, body)

    @memory.post('/{memory_id}/delete')
    async def delete_memory(memory_id: str, body: DecideInput, who: Caller) -> dict[str, JsonValue]:
        """删除。"""
        return await platform.work_memory.delete(who, memory_id, body)

    outer = APIRouter()
    outer.include_router(router)
    outer.include_router(memory)
    return outer
