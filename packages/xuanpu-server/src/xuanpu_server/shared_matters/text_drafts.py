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
    await platform.shared_contexts.check(who, body.source_context_id)
    rows = await platform.workspace.messages(who, body.draft_message_id - 1, 1)
    if not rows or rows[0]['id'] != body.draft_message_id:
        raise NotFoundError('本人草稿消息不存在')
    row = rows[0]
    if row['role'] != 'assistant' or row.get('source_context_id') != body.source_context_id or not row.get('sources'):
        raise InvalidRequestError('请选择当前共享对话中已生成的助手文本草稿')
    await platform.workspace.source_history.check(who, row)
    refs = tuple(SharedSourceRef.model_validate(value) for value in row['sources'])
    data = row['text'].encode('utf-8')
    publications = platform.shared_materials.publications
    result = await publications.prepare(who, identifier, request_id=body.request_id,
        expected=body.expected_version, name=body.name, mime='text/plain', data=data,
        source_refs=refs, draft_message_id=body.draft_message_id)
    await publications.execute(who.require_tenant(), str(result['id']), data=data)
    return result
