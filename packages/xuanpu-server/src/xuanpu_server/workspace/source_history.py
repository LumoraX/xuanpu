"""个人历史的来源投影；后台个人跟进始终排除受保护的共享内容。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from xuanpu_server.errors import NotFoundError, SharedAccessRevokedError
from xuanpu_server.shared_matters.source_dependencies import source_ref

if TYPE_CHECKING:
    from xuanpu_server.shared_matters.context_guard import SharedContexts
    from xuanpu_server.tenancy import Principal
    from xuanpu_server.workspace.store import WorkspaceStore


def protected_message(row: dict[str, Any]) -> bool:
    """残缺的来源标记也不能当作普通私人文本使用。"""
    return bool(row.get('source_context_id') or row.get('sources'))


class SourceHistory:
    """只在员工事务外复查共享授权，避免逆序获取租户和员工锁。"""
    def __init__(self, store: WorkspaceStore) -> None:
        """未接共享服务时允许旧个人历史，带来源内容则拒绝。"""
        self.store, self.contexts = store, None

    contexts: SharedContexts | None

    def bind(self, contexts: SharedContexts) -> None:
        """启动时绑定同一共享服务，禁止运行中替换权限来源。"""
        if self.contexts is not None and self.contexts is not contexts:
            raise RuntimeError('消息来源服务已绑定')
        self.contexts = contexts

    async def check(self, who: Principal, row: dict[str, Any]) -> None:
        """引用必须来自本人上下文的完整依赖集合，不能伪造或删减。"""
        if not protected_message(row):
            return
        identifier = row.get('source_context_id')
        if self.contexts is None or not isinstance(identifier, str):
            raise SharedAccessRevokedError('共享来源不可用')
        sources = await self.contexts.sources(who, identifier)
        if row.get('sources') != [source.model_dump(mode='json') for source in sources]:
            raise SharedAccessRevokedError('共享消息来源不一致，请开始新对话')

    async def project(self, who: Principal, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """同一工作单元复核整页，避免检查下一条消息期间前一条来源又失效。"""
        if not any(protected_message(row) for row in rows):
            return rows
        contexts = self.contexts
        if contexts is None:
            return [self.blocked(row) if protected_message(row) else row for row in rows]
        who = await contexts.service.actor(who)
        assert who.user_id is not None
        result = []
        async with contexts.service.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await contexts.service.actor(who)
            for row in rows:
                if not protected_message(row):
                    result.append(row)
                    continue
                try:
                    context = contexts.owned(tx, who, str(row.get('source_context_id', '')))
                    if not contexts.current(tx, who, context):
                        raise SharedAccessRevokedError('消息来源已失效')
                    expected = [source_ref(dep).model_dump(mode='json') for dep in contexts.dependencies(tx, context.id)]
                    if row.get('sources') != expected:
                        raise SharedAccessRevokedError('消息来源不一致')
                except (NotFoundError, SharedAccessRevokedError):
                    result.append(self.blocked(row))
                else:
                    result.append(row)
        return result

    @staticmethod
    def blocked(row: dict[str, Any]) -> dict[str, Any]:
        """保留游标位置，正文、附件、派生草稿及元数据均不可补读。"""
        return {key: row[key] for key in ('id', 'role', 'created_at', 'request_id')} | {
            'text': '共享资料权限已失效，请重新选择来源开始新对话。', 'attachments': [],
            'matter_id': None, 'metadata': {}, 'sources': [], 'source_blocked': True}

    async def personal_messages(self, tenant: str, user: str, after_id: int, limit: int) -> list[dict[str, Any]]:
        """主动扫描和恢复原始证据也排除共享消息，不产生无来源私人衍生物。"""
        rows = await self.store.messages(tenant, user, after_id, limit)
        return [row for row in rows if not protected_message(row)]

    async def protected_session(self, tenant: str, user: str, session_id: str) -> bool:
        """业务扫描排除所有曾绑定来源的会话，包括已失效的旧会话。"""
        if self.contexts is None:
            return False
        return bool(await self.contexts.service.store.runtime_contexts(tenant, user, session_id))
