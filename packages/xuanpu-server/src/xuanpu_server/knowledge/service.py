"""本人知识来源的登记、检索、刷新与删除（R4-D0）。

- 来源只能是员工本人工作区文件的明确版本（经工作区按本人权限读取，不替员工去别处抓取）；
- 只接受实际能按 UTF-8 解码、格式也对的 TXT/MD/CSV/JSON，二进制与不支持的格式拒绝，
  不解析 PDF/OCR/网页/音视频；
- 检索先确定此刻仍可读的来源集合，再匹配与计数：无权来源的正文、命中数与引用都不出现；
  原文件被删或移入回收站的来源标 stale 并排除；原文件有新版本只提示「有更新」，不自动换；
- 字面关键词匹配（%、_ 就是字符本身），首期是有界扫描，不宣称向量语义召回；
- 上限：每人 200 个来源、5000 万字、25000 个文本块；单文件 20 MiB；查询 500 字、最多 10 条命中。

登记在请求内同步提取并原子写入：进程中途退出不留半截文本块，用同一材料重试即可。
"""
from __future__ import annotations

import csv
import io
import json
import logging
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from xuanpu_server.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    PermissionDeniedError,
)
from xuanpu_server.ids import new_id

from .store import Chunk, KnowledgeSource, SourceRef

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from pydantic import JsonValue

    from xuanpu_server.tenancy import Principal
    from xuanpu_server.workspace.service import WorkspaceService

    from .store import KnowledgeStore

logger = logging.getLogger(__name__)
EXTRACTOR = 'utf8-text/1'
MIME_FORMATS = {'text/plain': 'text', 'text/markdown': 'text', 'text/csv': 'csv', 'application/json': 'json'}
MAX_SOURCES, MAX_CHARS, MAX_CHUNKS = 200, 50_000_000, 25_000
MAX_FILE_BYTES, CHUNK_CHARS, EXCERPT_CHARS = 20 * 1024 * 1024, 1000, 2000


class KnowledgeConfig(BaseModel):
    """按租户白名单开启，空名单始终关闭。"""

    model_config = ConfigDict(frozen=True, extra='forbid')
    enabled: StrictBool = False
    tenants: tuple[str, ...] = Field(default=(), max_length=1000)

    def allows(self, tenant: str) -> bool:
        """不能借空名单推导全企业开启。"""
        return self.enabled and tenant in self.tenants


class RegisterSource(BaseModel):
    """登记本人一份文件的明确版本。"""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)
    kind: Literal['workspace_file'] = 'workspace_file'
    item_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)


class SearchInput(BaseModel):
    """检索：查询 1–500 字，最多 10 条命中。"""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)
    query: str = Field(min_length=1, max_length=500, pattern=r'\S')
    limit: int = Field(default=10, ge=1, le=10)


class SourceChange(BaseModel):
    """刷新或删除来源须带当前版本。"""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)
    expected_version: int = Field(ge=1)


def extract(content: bytes, mime_type: str) -> str:
    """实际解码并核对格式；不符一律拒绝，不猜编码。"""
    fmt = MIME_FORMATS.get(mime_type)
    if fmt is None:
        raise InvalidRequestError('只支持 UTF-8 的 TXT、Markdown、CSV、JSON 文本')
    try:
        text = content.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise InvalidRequestError('文件不是 UTF-8 文本') from exc
    if '\x00' in text:
        raise InvalidRequestError('文件含二进制内容')
    if fmt == 'json':
        try:
            json.loads(text)
        except ValueError as exc:
            raise InvalidRequestError('JSON 格式不正确') from exc
    elif fmt == 'csv':
        try:
            list(csv.reader(io.StringIO(text)))
        except csv.Error as exc:
            raise InvalidRequestError('CSV 格式不正确') from exc
    return text


def chunk(source_id: str, text: str) -> list[Chunk]:
    """按行聚成约 1000 字的块（超长单行按长度切开）；空白文件不产生块。"""
    pieces: list[str] = []
    buffer = ''
    for line in text.splitlines(keepends=True):
        while len(line) > CHUNK_CHARS:
            if buffer:
                pieces.append(buffer)
                buffer = ''
            pieces.append(line[:CHUNK_CHARS])
            line = line[CHUNK_CHARS:]
        if len(buffer) + len(line) > CHUNK_CHARS and buffer:
            pieces.append(buffer)
            buffer = ''
        buffer += line
    if buffer:
        pieces.append(buffer)
    return [Chunk(source_id=source_id, ordinal=i, text=p) for i, p in enumerate(pieces) if p.strip()]


def source_view(source: KnowledgeSource, *, has_update: bool = False) -> dict[str, JsonValue]:
    """来源视图：版本与依据，不含正文。"""
    return {'id': source.id, 'kind': source.ref.kind, 'item_id': source.ref.item_id,
            'revision': source.ref.revision, 'title': source.title, 'sha256': source.sha256,
            'status': source.status, 'chunks': source.chunks, 'chars': source.chars, 'version': source.version,
            'has_update': has_update, 'created_at': source.created_at, 'updated_at': source.updated_at}


class KnowledgeService:
    """本人知识来源服务。"""

    def __init__(self, workspace: WorkspaceService, store: KnowledgeStore, *,
                 config: KnowledgeConfig | None = None, now: Callable[[], float]) -> None:
        """依赖由平台注入。"""
        self.workspace, self.store, self.now = workspace, store, now
        self.config = config or KnowledgeConfig()
        # 来源删除后通知依赖方（问答上下文）立即失效；依赖方读取时也会再核，失败时仍拒绝使用
        self.removal_listeners: list[Callable[[str, str, str], Awaitable[None]]] = []

    async def _employee(self, who: Principal) -> tuple[str, str]:
        """在职员工且租户开启知识功能。"""
        tenant, user = await self.workspace.require_employee(who)
        if not self.config.allows(tenant):
            raise PermissionDeniedError('企业尚未开启知识检索')
        return tenant, user

    async def register(self, who: Principal, body: RegisterSource) -> dict[str, JsonValue]:
        """登记一份本人文件的明确版本：按本人权限下载、核对格式与上限、原子写入。"""
        tenant, user = await self._employee(who)
        ref = SourceRef(kind='workspace_file', item_id=body.item_id, revision=body.revision)
        live = [s for s in await self.store.sources(tenant, user) if s.status != 'deleted']
        existing = next((s for s in live if s.ref == ref), None)
        if existing is not None:
            return source_view(existing)
        if len(live) >= MAX_SOURCES:
            raise ConflictError('知识来源数量已达上限')
        item = await self.workspace.get(who, body.item_id)
        version = next((v for v in await self.workspace.versions(who, body.item_id)
                        if v['revision'] == body.revision), None)
        if item.get('trashed_at') is not None or version is None:
            raise NotFoundError('材料或版本不存在')
        if version['size'] > MAX_FILE_BYTES:
            raise InvalidRequestError('单个文件不能超过 20 MiB')
        content, mime, _ = await self.workspace.download(who, body.item_id, body.revision)
        text = extract(content, mime)
        source_id = new_id('ksr')
        chunks = chunk(source_id, text)
        if (sum(s.chars for s in live) + len(text) > MAX_CHARS
                or sum(s.chunks for s in live) + len(chunks) > MAX_CHUNKS):
            raise ConflictError('知识来源的文本总量已达上限')
        now = self.now()
        source = KnowledgeSource(
            tenant_id=tenant, owner=user, id=source_id, ref=ref, title=str(item['data'].get('name', '')),
            sha256=str(version['sha256']), mime_type=mime, extractor=EXTRACTOR, chars=len(text),
            chunks=len(chunks), created_at=now, updated_at=now)
        return source_view(await self.store.add(source, chunks))

    async def _readable(self, who: Principal, source: KnowledgeSource) -> tuple[bool, bool]:
        """此刻是否仍可读（文件在、版本在、SHA 一致），以及是否有更新的版本。"""
        try:
            item = await self.workspace.get(who, source.ref.item_id)
            versions = await self.workspace.versions(who, source.ref.item_id)
        except (NotFoundError, PermissionDeniedError):
            return False, False
        version = next((v for v in versions if v['revision'] == source.ref.revision), None)
        if item.get('trashed_at') is not None or version is None or version['sha256'] != source.sha256:
            return False, False
        current = int(item['data'].get('current_revision', source.ref.revision))
        return True, current > source.ref.revision

    async def _live(self, who: Principal) -> list[tuple[KnowledgeSource, bool]]:
        """本人此刻可读的来源（不可读的就地标 stale）。"""
        tenant, user = await self._employee(who)
        found = []
        for source in await self.store.sources(tenant, user):
            if source.status != 'ready':
                continue
            readable, newer = await self._readable(who, source)
            if not readable:
                await self.store.update(source.model_copy(update={
                    'status': 'stale', 'version': source.version + 1, 'updated_at': self.now()}),
                    expected_version=source.version)
                continue
            found.append((source, newer))
        return found

    async def sources(self, who: Principal) -> dict[str, JsonValue]:
        """本人来源列表（含 stale 与是否有更新；已删除的不列）。"""
        tenant, user = await self._employee(who)
        live = {s.id: newer for s, newer in await self._live(who)}
        items: list[JsonValue] = [source_view(s, has_update=live.get(s.id, False))
                                  for s in await self.store.sources(tenant, user) if s.status != 'deleted']
        return {'sources': items, 'total': len(items)}

    async def search(self, who: Principal, body: SearchInput) -> dict[str, JsonValue]:
        """先定可读来源，再字面匹配与计数；命中带来源、版本与位置。"""
        tenant, user = await self._employee(who)
        live = {s.id: s for s, _ in await self._live(who)}
        chunks, total = await self.store.match(tenant, user, list(live), body.query, body.limit)
        hits: list[JsonValue] = [{
            'source_id': c.source_id, 'title': live[c.source_id].title, 'item_id': live[c.source_id].ref.item_id,
            'revision': live[c.source_id].ref.revision, 'ordinal': c.ordinal,
            'excerpt': c.text[:EXCERPT_CHARS]} for c in chunks]
        return {'hits': hits, 'total': total}

    async def _own(self, who: Principal, source_id: str) -> KnowledgeSource:
        """本人的一个未删除来源；别人的或不存在的一律 404。"""
        tenant, user = await self._employee(who)
        source = next((s for s in await self.store.sources(tenant, user) if s.id == source_id), None)
        if source is None or source.status == 'deleted':
            raise NotFoundError('来源不存在')
        return source

    async def delete(self, who: Principal, source_id: str, body: SourceChange) -> dict[str, JsonValue]:
        """逻辑删除并清掉文本块（代次加一，之后检索不再出现）。"""
        source = await self._own(who, source_id)
        changed = source.model_copy(update={'status': 'deleted', 'epoch': source.epoch + 1,
                                            'version': source.version + 1, 'updated_at': self.now()})
        deleted = await self.store.update(changed, expected_version=body.expected_version)
        for listener in self.removal_listeners:
            try:
                await listener(deleted.tenant_id, deleted.owner, deleted.id)
            except Exception:
                # 来源已删除且文本块已清：依赖方下次访问会复核到来源不在并拒绝，不会用到旧正文；
                # 这里只是没能提前清掉回答缓存，记错误日志由运维跟进，不让员工的删除失败
                logger.exception('来源 %s 删除后通知依赖方失败', deleted.id)
        return source_view(deleted)

    async def refresh(self, who: Principal, source_id: str, body: SourceChange) -> dict[str, JsonValue]:
        """员工明确刷新：登记文件当前版本为新来源，旧来源删除（旧引用不改写）。"""
        source = await self._own(who, source_id)
        if source.version != body.expected_version:
            raise ConflictError('来源已变化，请刷新后重试')
        item = await self.workspace.get(who, source.ref.item_id)
        current = int(item['data'].get('current_revision', source.ref.revision))
        if current == source.ref.revision and source.status == 'ready':
            return source_view(source)
        fresh = await self.register(who, RegisterSource(item_id=source.ref.item_id, revision=current))
        await self.delete(who, source_id, SourceChange(expected_version=source.version))
        return fresh

