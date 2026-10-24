"""先持久保存固定发布意图，再在事务外复制并复核，失败不半公开。"""
from __future__ import annotations

import asyncio
import hashlib
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from xuanpu_server.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    RuntimeUnavailableError,
)
from xuanpu_server.handoffs.packets import grant_packet
from xuanpu_server.handoffs.result_publication import require_result
from xuanpu_server.ids import new_id
from xuanpu_server.shared_matters.models import MaterialRevision, Publication, SharedMaterial
from xuanpu_server.shared_matters.permissions import access
from xuanpu_server.shared_matters.source_dependencies import check_publisher_sources
from xuanpu_server.tenancy import Principal
from xuanpu_server.workspace.models import FileData

if TYPE_CHECKING:
    from pydantic import JsonValue

    from xuanpu_server.coordination.models import CoordinationTransaction
    from xuanpu_server.shared_matters.materials import SharedMaterials
    from xuanpu_server.shared_matters.models import SharedSourceRef
    from xuanpu_server.shared_matters.schemas import PublishMaterialInput

# 代次只用于追踪，不作为旧writer已经死亡或对象空间已腾空的证明。
COPY_PROCESS_GENERATION = new_id('copygen')


class Publications:
    """有界复制与持久租约共用实例，不在数据库锁内读写对象。"""
    def __init__(self, materials: SharedMaterials) -> None:
        """使用同一服务、对象和私人版本仓储。"""
        self.materials, self.service = materials, materials.service
        self._copies = asyncio.Semaphore(self.service.config.max_concurrent_copies)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    async def source(self, tenant: str, user: str, item: str, revision: int) -> tuple[dict[str, Any], dict[str, Any]]:
        """源员工来自当前身份；原文件必须仍为本人有效文件。"""
        source = await self.materials.workspace.file_source(tenant, user, item, revision)
        if source is None or source[0]['kind'] != 'file' or source[0]['trashed_at'] is not None:
            raise NotFoundError('本人文件版本不存在')
        return source

    async def prepare(self, who: Principal, identifier: str, *, request_id: str, expected: int,
                      body: PublishMaterialInput | None = None, data: bytes | None = None,
                      name: str = '', mime: str = 'application/octet-stream',
                      source_refs: tuple[SharedSourceRef, ...] = (), draft_message_id: int | None = None,
                      result_for_handoff: str | None = None, handoff_version: int | None = None) -> dict[str, JsonValue]:
        """固定请求指纹和空间预留，意图、材料、事项版本与回执原子提交。"""
        if self._closed:
            raise RuntimeUnavailableError('共享发布正在停止，请稍后重试')
        who = await self.service.actor(who)
        assert who.user_id is not None
        checksum = hashlib.sha256(data).hexdigest() if data is not None else None
        payload: dict[str, JsonValue] = {'action': 'shared.publish', 'id': identifier,
            'body': body.model_dump(mode='json') if body else {'request_id': request_id,
                'expected_version': expected, 'name': name, 'mime': mime, 'sha256': checksum}}
        if result_for_handoff is not None:
            if body is not None or handoff_version is None:
                raise InvalidRequestError('本交接成果需要固定版本且只能新建材料')
            payload.update(result_for_handoff=result_for_handoff,handoff_version=handoff_version)
        if source_refs:
            payload.update(source_refs=[ref.model_dump(mode='json') for ref in source_refs], draft_message_id=draft_message_id)
        async with self.service.store.transaction(who.require_tenant(), participants=(who.user_id,)) as tx:
            await self.service.actor(who)
            check_publisher_sources(tx, who, source_refs, self.service.now(), self.materials)
            if result_for_handoff is not None:
                await require_result(tx,self.materials,who,identifier,result_for_handoff)
            else:
                access(tx, who, identifier, self.service.now(), write=True)
            old = tx.receipt(who.actor, request_id, payload)
            if old is not None:
                return old
            self.service.enabled(tx.tenant_id)
            matter = await require_result(tx,self.materials,who,identifier,result_for_handoff,
                version=handoff_version,shared_version=expected,projected=True) if result_for_handoff is not None else await self.service.writable(tx,who,identifier,expected)
            source: tuple[dict[str, Any], dict[str, Any]] | None = None
            if body is not None:
                source = await self.source(tx.tenant_id, who.user_id, body.source_item_id, body.source_revision)
                name, mime = source[0]['data']['name'], source[1]['mime_type']
                size, checksum = source[1]['size'], source[1]['sha256']
            else:
                size = len(data or b'')
            self._check_capacity(tx,name,mime,size)
            now = self.service.now()
            material = self._material(tx, identifier, who.user_id, name, body.material_id if body else None)
            if result_for_handoff is not None:
                material=material.model_copy(update={'result_for_handoff':result_for_handoff})
                tx.shared.materials[material.id]=material
            matter = self.service.bump(tx, matter, now=now)
            publication_id = new_id('pub')
            revision = max((p.revision for p in tx.shared.publications.values() if p.material_id == material.id), default=0) + 1
            publication = Publication(tenant_id=tx.tenant_id, id=publication_id, matter_id=identifier,
                material_id=material.id, publisher_id=who.user_id, publisher_epoch=tx.participant_epochs[who.user_id],
                expected_matter_version=matter.version, access_epoch=matter.access_epoch, revision=revision,
                object_key=f'{tx.tenant_id}/shared/{identifier}/{publication_id}', name=name, mime_type=mime,
                source_kind='shared_result' if result_for_handoff is not None else ('private_file' if body else ('text' if source_refs else 'upload')), source_item_id=body.source_item_id if body else None,
                source_revision=body.source_revision if body else None, source_item_version=source[0]['version'] if source else None,
                expected_sha256=str(checksum), expected_size=size, source_refs=source_refs,
                result_for_handoff=result_for_handoff,handoff_version=handoff_version,created_at=now, updated_at=now)
            tx.shared.publications[publication_id] = publication
            self.service.event(tx, who, matter, 'publication_requested', {'publication_id': publication_id, 'material_id': material.id})
            result: dict[str, JsonValue] = {'id': publication_id, 'material_id': material.id, 'status': 'pending'}
            tx.record_receipt(who.actor, request_id, payload, result)
            return result

    def _check_capacity(self,tx: CoordinationTransaction,name: str,mime: str,size: int) -> None:
        """文件元数据、单份大小及持久未知writer预留一起计入容量。"""
        try:
            FileData(name=name, mime_type=mime, size=size)
        except ValidationError as exc:
            raise InvalidRequestError('材料名称、格式或大小无效') from exc
        if size > self.service.config.max_material_bytes:
            raise InvalidRequestError('共享材料超过单文件上限')
        used = sum(r.size for r in tx.shared.revisions.values()) + sum(
            p.expected_size for p in tx.shared.publications.values() if p.status == 'pending' or (p.status in ('failed', 'withdrawn') and not p.object_cleaned))
        if used + size > self.service.config.max_storage_bytes:
            raise ConflictError('共享副本和历史版本已达到空间上限')

    def _material(self, tx: CoordinationTransaction, identifier: str, user: str, name: str, material_id: str | None) -> SharedMaterial:
        """新材料独立建档，新增版本不得覆盖既有revision或并行发布。"""
        if material_id is not None:
            material = tx.shared.materials.get(material_id)
            if material is None or material.matter_id != identifier or material.status == 'withdrawn':
                raise NotFoundError('共享材料不存在')
            if any(p.material_id == material_id and p.status == 'pending' for p in tx.shared.publications.values()):
                raise ConflictError('该材料已有待完成发布')
            return material
        now = self.service.now()
        material = SharedMaterial(tenant_id=tx.tenant_id, id=new_id('mat'), matter_id=identifier,
            publisher_id=user, name=name, created_at=now, updated_at=now)
        tx.shared.materials[material.id] = material
        return material

    async def claim(self, tenant: str, publication_id: str) -> Publication | None:
        """恢复只领取到期意图；当前身份、权限和预留版本都必须仍有效。"""
        async with self.service.store.transaction(tenant) as read:
            publication = read.shared.publications.get(publication_id)
        if publication is None:
            raise NotFoundError('发布意图不存在')
        who = Principal(tenant_id=tenant, user_id=publication.publisher_id, role='tenant_member', key_id=None)
        async with self.service.store.transaction(tenant, participants=(publication.publisher_id,)) as tx:
            await self.service.actor(who)
            current = tx.shared.publications[publication_id]
            if current.status != 'pending' or current.lease_until > self.service.now():
                return None
            await self._current(tx, who, current)
            now = self.service.now()
            claimed = current.model_copy(update={'claim_token': new_id('copy'), 'lease_until': now + self.service.config.copy_lease_seconds,
                'copy_generation': COPY_PROCESS_GENERATION, 'copy_closed': False,
                'uncertain_copy': current.uncertain_copy or not current.copy_closed,
                'version': current.version + 1, 'updated_at': now})
            tx.shared.publications[publication_id] = claimed
            return claimed

    async def _current(self, tx: CoordinationTransaction, who: Principal, publication: Publication) -> None:
        """复制前和提交前共用当前源、身份代次、权限及事项版本检查。"""
        if self._closed:
            raise RuntimeUnavailableError('共享发布正在停止')
        self.service.enabled(tx.tenant_id)
        if publication.result_for_handoff is not None and publication.handoff_version is None:
            raise ConflictError('成果发布缺少固定交接版本')
        matter = await require_result(tx,self.materials,who,publication.matter_id,publication.result_for_handoff,
            version=publication.handoff_version,shared_version=publication.expected_matter_version) if publication.result_for_handoff is not None else await self.service.writable(tx, who, publication.matter_id, publication.expected_matter_version)
        if tx.participant_epochs[publication.publisher_id] != publication.publisher_epoch or matter.access_epoch != publication.access_epoch:
            raise ConflictError('发布期间权限已改变')
        check_publisher_sources(tx, who, publication.source_refs, self.service.now(), self.materials)
        if publication.source_kind == 'private_file':
            item, revision = await self.source(tx.tenant_id, publication.publisher_id,
                publication.source_item_id or '', publication.source_revision or 0)
            if item['version'] != publication.source_item_version or (revision['sha256'], revision['size']) != (publication.expected_sha256, publication.expected_size):
                raise ConflictError('发布期间原文件版本已变化')

    async def execute(self, tenant: str, publication_id: str, *, data: bytes | None = None) -> None:
        """复制失效或损坏时只标记失败；取消留下可恢复的持久意图。"""
        if self._closed:
            raise RuntimeUnavailableError('共享发布正在停止')
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        claimed: Publication | None = None
        try:
            async with self._copies:
                claimed = await self.claim(tenant, publication_id)
                if claimed is None:
                    return
                await self._copy(claimed, data)
                await self.closed(claimed)
                await self.finish(claimed)
                await self.cleanup(tenant, publication_id, force=True)
        except Exception as exc:
            await self.fail(tenant, publication_id, type(exc).__name__, token=claimed.claim_token if claimed else None)
        finally:
            self._tasks.discard(task)

    async def close(self) -> None:
        """停止新复制并取消等待在途HTTP发布，不把线程取消等同磁盘写入已结束。"""
        self._closed = True
        tasks = tuple(task for task in self._tasks if task is not asyncio.current_task())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _copy(self, publication: Publication, data: bytes | None) -> None:
        """对象不在DB事务内读写，所有路径有界且校验固定SHA。"""
        objects = self.materials.objects
        if await objects.exists(publication.object_key):
            content = await objects.get_limited(publication.object_key, max_bytes=self.service.config.max_material_bytes)
        else:
            if data is None and publication.source_kind == 'private_file':
                _, revision = await self.source(publication.tenant_id, publication.publisher_id,
                    publication.source_item_id or '', publication.source_revision or 0)
                data = await objects.get_limited(revision['object_key'], max_bytes=self.service.config.max_material_bytes)
            if data is None:
                raise ConflictError('上传字节未落盘，请明确重新发布')
            self._verify(publication, data)
            await objects.put(publication.object_key, data, content_type=publication.mime_type)
            content = await objects.get_limited(publication.object_key, max_bytes=self.service.config.max_material_bytes)
        self._verify(publication, content)

    @staticmethod
    def _verify(publication: Publication, data: bytes) -> None:
        """长度和SHA任何一项不符都不能公开材料。"""
        if len(data) != publication.expected_size or hashlib.sha256(data).hexdigest() != publication.expected_sha256:
            raise ConflictError('共享副本字节校验不一致')

    async def closed(self, claimed: Publication) -> None:
        """对象调用正常返回才记录writer结束；取消或异常不猜测底层写入已经停下。"""
        async with self.service.store.transaction(claimed.tenant_id) as tx:
            current = tx.shared.publications[claimed.id]
            if current.claim_token == claimed.claim_token and not current.copy_closed:
                tx.shared.publications[current.id] = current.model_copy(update={'copy_closed': True,
                    'version': current.version + 1, 'updated_at': self.service.now()})

    async def finish(self, claimed: Publication) -> None:
        """最终检查与ready revision、历史和事项版本同事务提交。"""
        who = Principal(tenant_id=claimed.tenant_id, user_id=claimed.publisher_id, role='tenant_member', key_id=None)
        async with self.service.store.transaction(claimed.tenant_id, participants=(claimed.publisher_id,)) as tx:
            current = tx.shared.publications[claimed.id]
            if current.status != 'pending' or current.claim_token != claimed.claim_token:
                return
            await self._current(tx, who, current)
            now = self.service.now()
            revision_id = 'srv_' + current.id.removeprefix('pub_')
            tx.shared.revisions[revision_id] = MaterialRevision(tenant_id=tx.tenant_id, id=revision_id,
                matter_id=current.matter_id, material_id=current.material_id, revision=current.revision,
                publisher_id=current.publisher_id, object_key=current.object_key, sha256=current.expected_sha256,
                size=current.expected_size, mime_type=current.mime_type, source_kind=current.source_kind,
                source_item_id=current.source_item_id, source_user_id=current.publisher_id if current.source_kind == 'private_file' else None,
                source_revision=current.source_revision, source_refs=current.source_refs, created_at=now, updated_at=now)
            material = tx.shared.materials[current.material_id]
            tx.shared.materials[material.id] = material.model_copy(update={'status': 'ready', 'current_revision': current.revision,
                'version': material.version + 1, 'updated_at': now})
            tx.shared.publications[current.id] = current.model_copy(update={'status': 'ready', 'claim_token': None,
                'lease_until': 0, 'version': current.version + 1, 'updated_at': now})
            if current.result_for_handoff is not None:
                grant_packet(tx,tx.collaboration.handoffs[current.result_for_handoff],current.publisher_id,now,revisions=(revision_id,))
            matter = self.service.bump(tx, tx.shared.matters[current.matter_id], now=now)
            self.service.event(tx, who, matter, 'material_published', {'material_id': current.material_id,
                'material_revision_id': revision_id, 'sha256': current.expected_sha256, 'size': current.expected_size})

    async def fail(self, tenant: str, publication_id: str, code: str, *, token: str | None) -> None:
        """失败不删除历史；对象清理只针对尚未公开且由本租约持有的意图。"""
        async with self.service.store.transaction(tenant) as tx:
            current = tx.shared.publications.get(publication_id)
            if current is None or current.status != 'pending' or (token is not None and current.claim_token != token):
                return
            if token is None and current.lease_until > self.service.now():
                return
            tx.shared.publications[publication_id] = current.model_copy(update={'status': 'failed', 'error_code': code,
                'claim_token': None, 'lease_until': 0, 'version': current.version + 1, 'updated_at': self.service.now()})
            material = tx.shared.materials[current.material_id]
            if material.current_revision == 0:
                tx.shared.materials[material.id] = material.model_copy(update={'status': 'failed',
                    'version': material.version + 1, 'updated_at': self.service.now()})
        await self.cleanup(tenant, publication_id)

    async def cleanup(self, tenant: str, publication_id: str, *, force: bool = False) -> None:
        """清理可重试；删除发生在事务外，成功后再释放预留空间。"""
        async with self.service.store.transaction(tenant) as read:
            publication = read.shared.publications[publication_id]
            if publication.status not in ('failed', 'withdrawn'):
                return
            if publication.lease_until > self.service.now() and not force:
                return
        await self.materials.objects.delete(publication.object_key)
        async with self.service.store.transaction(tenant) as tx:
            current = tx.shared.publications[publication_id]
            closed = current.copy_closed and not current.uncertain_copy
            if current.status in ('failed', 'withdrawn') and not current.object_cleaned and closed:
                tx.shared.publications[publication_id] = current.model_copy(update={'object_cleaned': True,
                    'copy_closed': True, 'uncertain_copy': False,
                    'version': current.version + 1, 'updated_at': self.service.now()})
