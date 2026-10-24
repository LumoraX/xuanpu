"""明确选择的材料供共享对话读取，正文有界且不写入私人文件。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import NotFoundError

if TYPE_CHECKING:
    from xuanpu_server.shared_matters.context_guard import SharedContexts
    from xuanpu_server.tenancy import Principal


async def selected_text(contexts: SharedContexts, who: Principal, identifier: str) -> list[dict[str, Any]]:
    """固定材料逐个读前后鉴权，二进制仅提供未读说明，不伪装已理解。"""
    _, dependencies = await contexts.check(who, identifier)
    parts: list[dict[str, Any]] = []
    remaining = 48000
    for dependency in dependencies:
        if dependency.material_revision_id is None:
            continue
        async with contexts.service.store.transaction(who.require_tenant(), participants=(dependency.user_id,)) as tx:
            revision = tx.shared.revisions.get(dependency.material_revision_id)
            if revision is None:
                raise NotFoundError('选定材料版本不存在')
            material = tx.shared.materials[revision.material_id]
        metadata: dict[str, Any] = {'id': revision.id, 'name': material.name, 'revision': revision.revision,
            'sha256': revision.sha256, 'mime_type': revision.mime_type}
        if remaining <= 0 or revision.size > 2 * 1024 * 1024 or not (
                revision.mime_type.startswith('text/') or revision.mime_type == 'application/json'):
            parts.append({**metadata, 'not_read': '本次未读取正文，请在原生资料页面查看'})
            continue
        await contexts.check(who, identifier)
        data, _, _, _ = await contexts.materials.download(who, dependency.matter_id,
            revision.material_id, revision.revision)
        await contexts.check(who, identifier)
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            parts.append({**metadata, 'not_read': '非UTF-8文本，本次未读取正文'})
            continue
        limit = min(16000, remaining)
        parts.append({**metadata, 'text': text[:limit], 'truncated': len(text) > limit})
        remaining -= min(len(text), limit)
    await contexts.check(who, identifier)
    return parts
