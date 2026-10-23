"""共享材料只经当前ACL代理读取，限定授权精确到不可变revision。"""
from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from xuanpu_server.errors import ConflictError, InvalidRequestError, NotFoundError
from xuanpu_server.objectstore.base import ObjectNotFoundError, ObjectTooLargeError
from xuanpu_server.shared_matters.permissions import access
from xuanpu_server.shared_matters.publication import Publications
from xuanpu_server.shared_matters.views import page

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.objectstore import ObjectStore
    from xuanpu_server.shared_matters.models import MaterialRevision, SharedMaterial
    from xuanpu_server.shared_matters.schemas import PublishMaterialInput, SharedChange
    from xuanpu_server.shared_matters.service import SharedMatterService
    from xuanpu_server.tenancy import Principal
    from xuanpu_server.workspace.store import WorkspaceStore


class SharedMaterials:
    """显式共享副本独立于原文件，撤回不篡改历史SHA。"""
    def __init__(self, service: SharedMatterService, workspace: WorkspaceStore, objects: ObjectStore) -> None:
        """使用原对象仓储及固定版本读取，不另建私有文件复制通道。"""
        self.service, self.workspace, self.objects = service, workspace, objects
        self.publications = Publications(self)

    async def publish(self, who: Principal, identifier: str, body: PublishMaterialInput) -> dict[str, JsonValue]:
        """本人明确选定版本，原件归属和其他文件权限保持各自约束。"""
        result = await self.publications.prepare(who, identifier, request_id=body.request_id, expected=body.expected_version, body=body)
        await self.publications.execute(who.require_tenant(), str(result['id']))
        return result

    async def upload(self, who: Principal, identifier: str, *, request_id: str, expected: int,
                     name: str, mime: str, data: bytes) -> dict[str, JsonValue]:
        """原生上传需要明确确认；重试指纹包含实际字节SHA。"""
        if len(data) > self.service.config.max_material_bytes:
            raise InvalidRequestError('共享材料超过单文件上限')
        result = await self.publications.prepare(who, identifier, request_id=request_id, expected=expected,
            name=name, mime=mime, data=data)
        await self.publications.execute(who.require_tenant(), str(result['id']), data=data)
        return result

    async def publication(self, who: Principal, identifier: str, publication_id: str) -> dict[str, JsonValue]:
        """只返回授权范围内状态，稳定回执不缓存正文或对象路径。"""
        who = await self.service.actor(who)
        assert who.user_id is not None
        async with self.service.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await self.service.actor(who)
            access(tx, who, identifier, self.service.now(), full=True)
            current = tx.shared.publications.get(publication_id)
            if current is None or current.matter_id != identifier:
                raise NotFoundError('发布意图不存在')
            return {k: v for k, v in current.model_dump(mode='json').items() if k in (
                'id', 'material_id', 'revision', 'version', 'status', 'error_code')}

    def readable(self, tx: CoordinationTransaction, who: Principal, identifier: str,
                 material_id: str, revision: int, *, trail: frozenset[str] = frozenset()) -> tuple[SharedMaterial, MaterialRevision]:
        """派生来源逐级复查当前权限，历史来源证明不授予永久读取权。"""
        if len(trail) >= 16 or material_id in trail:
            raise NotFoundError('共享材料不存在')
        _, grants = access(tx, who, identifier, self.service.now())
        material = tx.shared.materials.get(material_id)
        selected = next((r for r in tx.shared.revisions.values() if r.material_id == material_id
                         and r.matter_id == identifier and r.revision == revision), None)
        if material is None or material.matter_id != identifier or material.status == 'withdrawn' or selected is None or selected.status != 'ready':
            raise NotFoundError('共享材料不存在')
        if not any(g.scope == 'full' or selected.id in g.material_revisions for g in grants):
            raise NotFoundError('共享材料不存在')
        for source in selected.source_refs:
            matter, _ = access(tx, who, source.shared_id, self.service.now())
            if matter.access_epoch != source.access_epoch:
                raise NotFoundError('共享材料来源已失效')
            if source.material_revision_id is not None:
                original = tx.shared.revisions.get(source.material_revision_id)
                if original is None:
                    raise NotFoundError('共享材料来源不存在')
                self.readable(tx, who, source.shared_id, original.material_id, original.revision, trail=trail | {material_id})
        return material, selected

    async def listing(self, who: Principal, identifier: str, *, limit: int = 50, cursor: str | None = None) -> dict[str, JsonValue]:
        """先过滤每个材料版本的当前授权，再计算可见数量。"""
        who = await self.service.actor(who)
        assert who.user_id is not None
        async with self.service.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await self.service.actor(who)
            access(tx, who, identifier, self.service.now())
            records: list[dict[str, JsonValue]] = []
            for material in tx.shared.materials.values():
                if material.matter_id != identifier:
                    continue
                revisions: list[JsonValue] = []
                for revision in tx.shared.revisions.values():
                    if revision.material_id != material.id:
                        continue
                    try:
                        self.readable(tx, who, identifier, material.id, revision.revision)
                    except NotFoundError:
                        continue
                    revisions.append({k: v for k, v in revision.model_dump(mode='json').items()
                                      if k in ('id', 'revision', 'sha256', 'size', 'mime_type', 'created_at')})
                if revisions:
                    records.append({'id': material.id, 'name': material.name, 'revisions': revisions})
            return page(records, limit=limit, cursor=cursor)

    async def download(self, who: Principal, identifier: str, material_id: str, revision: int) -> tuple[bytes, str, str, str]:
        """有界读取后再次查当前授权，平台不产生公开永久URL。"""
        who = await self.service.actor(who)
        assert who.user_id is not None
        async with self.service.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await self.service.actor(who)
            material, selected = self.readable(tx, who, identifier, material_id, revision)
        try:
            data = await self.objects.get_limited(selected.object_key, max_bytes=self.service.config.max_material_bytes)
        except (ObjectNotFoundError, ObjectTooLargeError) as exc:
            raise ConflictError('共享副本缺失或超过大小限制') from exc
        if len(data) != selected.size or hashlib.sha256(data).hexdigest() != selected.sha256:
            raise ConflictError('共享副本校验不一致')
        async with self.service.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await self.service.actor(who)
            self.readable(tx, who, identifier, material_id, revision)
        return data, selected.mime_type, material.name, selected.sha256

    async def withdraw(self, who: Principal, identifier: str, material_id: str, body: SharedChange) -> dict[str, JsonValue]:
        """撤回阻断新请求；不宣称已交付字节能被追回。"""
        who = await self.service.actor(who)
        assert who.user_id is not None
        payload: dict[str, JsonValue] = {'action': 'shared.withdraw', 'id': identifier, 'material_id': material_id, 'body': body.model_dump(mode='json')}
        async with self.service.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await self.service.actor(who)
            access(tx, who, identifier, self.service.now(), write=True)
            old = tx.receipt(who.actor, body.request_id, payload)
            if old is not None:
                return old
            matter = await self.service.writable(tx, who, identifier, body.expected_version)
            material = tx.shared.materials.get(material_id)
            if material is None or material.matter_id != identifier:
                raise NotFoundError('共享材料不存在')
            if material.status == 'withdrawn':
                raise ConflictError('该材料已撤回')
            now = self.service.now()
            tx.shared.materials[material.id] = material.model_copy(update={'status': 'withdrawn', 'version': material.version + 1, 'updated_at': now})
            for revision in list(tx.shared.revisions.values()):
                if revision.material_id == material_id and revision.status == 'ready':
                    tx.shared.revisions[revision.id] = revision.model_copy(update={'status': 'withdrawn', 'version': revision.version + 1, 'updated_at': now})
            for publication in list(tx.shared.publications.values()):
                if publication.material_id == material_id and publication.status == 'pending':
                    tx.shared.publications[publication.id] = publication.model_copy(update={'status': 'withdrawn', 'version': publication.version + 1, 'updated_at': now})
            matter = self.service.bump(tx, matter, now=now, invalidate=True)
            self.service.event(tx, who, matter, 'material_withdrawn', {'material_id': material_id})
            result: dict[str, JsonValue] = {'id': material_id, 'status': 'withdrawn', 'version': matter.version}
            tx.record_receipt(who.actor, body.request_id, payload, result)
            return result
