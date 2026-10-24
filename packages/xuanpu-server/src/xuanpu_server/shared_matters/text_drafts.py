"""原生发布受保护文本草稿，固定本人消息与真实来源集合。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import InvalidRequestError, NotFoundError
from xuanpu_server.shared_matters.models import SharedSourceRef

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.platform import Platform
    from xuanpu_server.shared_matters.schemas import PublishTextDraft
    from xuanpu_server.tenancy import Principal


async def publish_text(platform: Platform, who: Principal, identifier: str, body: PublishTextDraft) -> dict[str, JsonValue]:
    """只发布原生明确选择的助手文本，来源证明在发布前后均重新核对。"""
    await platform.shared_matters.get(who, identifier)
    data,refs=await selected_draft(platform,who,body.source_context_id,body.draft_message_id)
    publications = platform.shared_materials.publications
    result = await publications.prepare(who, identifier, request_id=body.request_id,
        expected=body.expected_version, name=body.name, mime='text/plain', data=data,
        source_refs=refs, draft_message_id=body.draft_message_id)
    await publications.execute(who.require_tenant(), str(result['id']), data=data)
    return result


async def selected_draft(platform: Platform,who: Principal,context_id: str,message_id: int) -> tuple[bytes,tuple[SharedSourceRef,...]]:
    """固定本人已保存消息，复核当前来源后再用于普通发布或受限成果发布。"""
    await platform.shared_contexts.check(who, context_id)
    rows = await platform.workspace.messages(who, message_id - 1, 1)
    if not rows or rows[0]['id'] != message_id:
        raise NotFoundError('本人草稿消息不存在')
    row = rows[0]
    if row['role'] != 'assistant' or row.get('source_context_id') != context_id or not row.get('sources'):
        raise InvalidRequestError('请选择当前共享对话中已生成的助手文本草稿')
    await platform.workspace.source_history.check(who, row)
    refs = tuple(SharedSourceRef.model_validate(value) for value in row['sources'])
    data = row['text'].encode('utf-8')
    return data,refs
