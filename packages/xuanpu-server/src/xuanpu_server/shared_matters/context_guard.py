"""原生来源选择和当前上下文检查；失效标记持久提交后才返回拒绝。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from xuanpu_server.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
    SharedAccessRevokedError,
)
from xuanpu_server.ids import new_id
from xuanpu_server.shared_matters.models import SharedContext, SourceDependency
from xuanpu_server.shared_matters.permissions import access
from xuanpu_server.shared_matters.source_dependencies import (
    authority_digest,
    check_dependency,
    permission_digest,
    source_ref,
)
from xuanpu_server.shared_matters.views import page, selection_version

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.shared_matters.materials import SharedMaterials
    from xuanpu_server.shared_matters.models import SharedAccessGrant, SharedMatter, SharedSourceRef
    from xuanpu_server.shared_matters.schemas import CreateSharedContext, SharedChange
    from xuanpu_server.tenancy import Principal


class SharedContexts:
    """每个来源上下文只属于一个员工；再授权不能复活旧会话。"""
    def __init__(self, materials: SharedMaterials) -> None:
        """共用协调事务、材料ACL及平台时钟，不维护授权缓存。"""
        self.materials, self.service = materials, materials.service

    @staticmethod
    def owned(tx: CoordinationTransaction, who: Principal, identifier: str) -> SharedContext:
        """管理员与其他员工无法区分不属于本人的上下文。"""
        context = tx.shared.contexts.get(identifier)
        if context is None or context.user_id != who.user_id:
            raise NotFoundError('共享上下文不存在')
        return context

    @staticmethod
    def dependencies(tx: CoordinationTransaction, context_id: str) -> list[SourceDependency]:
        """依赖按稳定编号返回，禁止通过客户端改写来源集合。"""
        return sorted((d for d in tx.shared.dependencies.values() if d.context_id == context_id), key=lambda d: d.id)

    async def create(self, who: Principal, body: CreateSharedContext) -> dict[str, JsonValue]:
        """明确选择内容和固定材料版本，依赖先于正文输出持久保存。"""
        who = await self.service.actor(who)
        assert who.user_id is not None
        payload: dict[str, JsonValue] = {'action': 'shared.context', 'body': body.model_dump(mode='json')}
        async with self.service.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await self.service.actor(who)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                self.owned(tx, who, str(old['id']))
                return old
            self.service.enabled(tx.tenant_id)
            matter, grants = access(tx, who, body.shared_id, self.service.now())
            ordered = sorted(grants, key=lambda g: (g.scope != 'full', g.created_at, g.id))
            primary = ordered[0]
            selected_version = selection_version(matter, grants)
            if selected_version != body.expected_version:
                raise ConflictError('共享事项已改变，请重新选择来源')
            if sum(c.user_id == who.user_id and c.status == 'active' for c in tx.shared.contexts.values()) >= 200:
                raise ConflictError('本人活动共享对话达到上限，请先结束不再使用的阅读')
            await self._runtime(tx, who, body)
            now, identifier = self.service.now(), new_id('scx')
            context = SharedContext(tenant_id=tx.tenant_id, id=identifier, user_id=who.user_id,
                user_epoch=tx.participant_epochs[who.user_id], kind=body.kind,
                runtime_session_id=body.runtime_session_id, created_at=now, updated_at=now)
            for selected in ([primary] if primary.scope == 'full' else ordered):
                title = matter.title if selected.scope == 'full' else selected.packet_title
                content = matter.content if selected.scope == 'full' else selected.content_snapshot
                self._dependency(tx, context, matter, selected, title=title,
                    content=content if body.include_content and (selected.scope == 'full' or selected.include_content) else None)
            for revision_id in dict.fromkeys(body.material_revision_ids):
                revision = tx.shared.revisions.get(revision_id)
                if revision is None or revision.matter_id != matter.id:
                    raise InvalidRequestError('选择的材料版本不可读')
                try:
                    self.materials.readable(tx, who, matter.id, revision.material_id, revision.revision)
                except NotFoundError as exc:
                    raise InvalidRequestError('选择的材料版本不可读') from exc
                grant = next(g for g in ordered if g.scope == 'full' or revision.id in g.material_revisions)
                self._dependency(tx, context, matter, grant, revision_id=revision.id)
            tx.shared.contexts[identifier] = context
            result: dict[str, JsonValue] = {'id': identifier, 'version': 1, 'status': 'active'}
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result

    async def _runtime(self, tx: CoordinationTransaction, who: Principal, body: CreateSharedContext) -> None:
        """只绑定本人的普通会话；R2沙盒不接受共享正文或共享输入。"""
        if body.kind == 'assistant_conversation':
            if body.runtime_session_id is not None:
                raise InvalidRequestError('主对话上下文不能另传业务会话')
            return
        if body.runtime_session_id is None:
            raise InvalidRequestError('业务上下文需要明确本人会话')
        record = await self.service.organization.control.get_session(tx.tenant_id, body.runtime_session_id)
        if record is None or record.user_id != who.user_id:
            raise NotFoundError('业务会话不存在')
        if record.work_task_id is not None or record.status != 'active':
            raise InvalidRequestError('共享内容只绑定启用的普通会话，不进入后台沙盒')
        if record.thread_id is not None or record.owner is not None:
            raise ConflictError('请为共享资料开始一个尚未使用的新会话')
        if any(c.runtime_session_id == record.session_id for c in tx.shared.contexts.values()):
            raise ConflictError('原会话已有来源记录，请开始新会话')

    def _dependency(self, tx: CoordinationTransaction, context: SharedContext, matter: SharedMatter,
                    grant: SharedAccessGrant, *, title: str = '', content: str | None = None,
                    revision_id: str | None = None) -> None:
        """来源正文只存受当前来源权限保护的冻结快照，不复制成无来源私人材料。"""
        ordinal = sum(d.context_id == context.id for d in tx.shared.dependencies.values())
        now, identifier = self.service.now(), f'sdp_{context.id}_{ordinal:06d}'
        tx.shared.dependencies[identifier] = SourceDependency(tenant_id=tx.tenant_id, id=identifier,
            user_id=context.user_id, context_id=context.id, matter_id=matter.id,
            material_revision_id=revision_id, grant_id=grant.id, grant_epoch=grant.epoch,
            access_epoch=matter.access_epoch, matter_version=matter.version if grant.scope == 'full' else grant.version, source_scope=grant.scope,
            permission_digest=permission_digest(grant),
            authority_digest=authority_digest(tx, context.user_id, grant, now), title_snapshot=title, content_snapshot=content,
            created_at=now, updated_at=now)

    def current(self, tx: CoordinationTransaction, who: Principal, context: SharedContext) -> bool:
        """此方法仅校验工作单元；调用者必须在退出事务后才抛失效错误。"""
        valid = context.status == 'active' and context.user_epoch == tx.participant_epochs.get(context.user_id)
        dependencies = self.dependencies(tx, context.id)
        try:
            if not valid or not dependencies:
                raise NotFoundError('上下文来源已失效')
            for dependency in dependencies:
                check_dependency(tx, who, dependency, self.service.now())
                if dependency.material_revision_id is not None:
                    revision = tx.shared.revisions[dependency.material_revision_id]
                    self.materials.readable(tx, who, dependency.matter_id, revision.material_id, revision.revision)
        except NotFoundError:
            if context.status == 'active':
                tx.shared.contexts[context.id] = context.model_copy(update={'status': 'blocked',
                    'block_reason': 'shared_access_revoked', 'version': context.version + 1, 'updated_at': self.service.now()})
            return False
        return True

    async def check(self, who: Principal, identifier: str) -> tuple[SharedContext, list[SourceDependency]]:
        """持久标记旧上下文失效，再向调用者拒绝正文和后续模型使用。"""
        who = await self.service.actor(who)
        assert who.user_id is not None
        async with self.service.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await self.service.actor(who)
            context = self.owned(tx, who, identifier)
            valid = self.current(tx, who, context)
            dependencies = self.dependencies(tx, context.id)
        if not valid:
            raise SharedAccessRevokedError('共享资料权限已失效，请开始新对话并重新选择来源')
        return context, dependencies

    async def sources(self, who: Principal, identifier: str) -> tuple[SharedSourceRef, ...]:
        """来源引用来自已核对的固定依赖，不接受模型自报的授权字段。"""
        _, dependencies = await self.check(who, identifier)
        return tuple(source_ref(dependency) for dependency in dependencies)

    async def check_runtime(self, who: Principal, session_id: str) -> tuple[str, ...]:
        """仅已绑定来源的会话进入共享授权；普通会话仍走其原有身份与归属规则。"""
        if who.user_id is None:
            raise PermissionDeniedError('共享阅读需要员工身份')
        identifiers = await self.service.store.runtime_contexts(who.require_tenant(), who.user_id, session_id)
        if len(identifiers) > 1:
            raise SharedAccessRevokedError('业务会话存在冲突来源，请开始新会话')
        for identifier in identifiers:
            await self.check(who, identifier)
        return identifiers

    async def require_private_runtime(self, who: Principal, session_id: str) -> None:
        """普通路由不能携带曾绑定共享来源的会话，包括入口检查后的并发绑定。"""
        if who.user_id is not None and await self.check_runtime(who, session_id):
            raise InvalidRequestError('共享阅读会话请在对应对话中继续，不能转成普通个人业务')

    async def read(self, who: Principal, identifier: str) -> dict[str, JsonValue]:
        """正文补读只输出原生选择的快照和精确版本引用，无对象路径。"""
        context, dependencies = await self.check(who, identifier)
        texts = [d for d in dependencies if d.material_revision_id is None]
        assert texts
        return {'id': context.id, 'title': texts[0].title_snapshot,
            'content': '\n'.join(dict.fromkeys(d.content_snapshot for d in texts if d.content_snapshot)),
            'material_revision_ids': [str(d.material_revision_id) for d in dependencies if d.material_revision_id is not None],
            'sources': [source_ref(d).model_dump(mode='json') for d in dependencies]}

    async def metadata(self, who: Principal, identifier: str) -> dict[str, JsonValue]:
        """失效上下文仍可由本人查询当前版本并结束，不补读来源正文。"""
        who = await self.service.actor(who)
        assert who.user_id is not None
        async with self.service.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await self.service.actor(who)
            context = self.owned(tx, who, identifier)
            return {key: value for key, value in context.model_dump(mode='json').items()
                if key in ('id', 'version', 'kind', 'status', 'runtime_session_id', 'created_at')}

    async def listing(self, who: Principal, *, limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """本人可整理旧阅读，目录不含来源标题正文或其他员工的上下文。"""
        who = await self.service.actor(who)
        assert who.user_id is not None
        async with self.service.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await self.service.actor(who)
            records = [{key: value for key, value in context.model_dump(mode='json').items()
                if key in ('id', 'version', 'kind', 'status', 'runtime_session_id', 'created_at')}
                for context in tx.shared.contexts.values() if context.user_id == who.user_id]
            return page(records, limit=limit, cursor=cursor)

    async def close(self, who: Principal, identifier: str, body: SharedChange) -> dict[str, JsonValue]:
        """原生确认终止本人阅读，历史引用保持，旧上下文不能重新启用。"""
        who = await self.service.actor(who)
        assert who.user_id is not None
        payload: dict[str, JsonValue] = {'action': 'shared.context.close', 'id': identifier, 'body': body.model_dump(mode='json')}
        async with self.service.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await self.service.actor(who)
            current = self.owned(tx, who, identifier)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            if current.version != body.expected_version:
                raise ConflictError('阅读状态已变化，请重新核对')
            if current.status != 'closed':
                current = current.model_copy(update={'status': 'closed', 'updated_at': self.service.now(),
                    'version': current.version + 1})
                tx.shared.contexts[identifier] = current
            result: dict[str, JsonValue] = {'id': current.id, 'status': current.status, 'version': current.version}
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result
