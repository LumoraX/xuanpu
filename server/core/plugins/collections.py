"""插件零 DDL 扩展集合存储（按 tenant / plugin / user / collection 四级隔离与乐观锁版本控制）。

- 非核心后端/前端插件无需改动核心 SQL 表结构，避免蓝绿回滚因破坏性 DDL 失败；
- 目录权限 0700、文件权限 0600，采用临时文件原子替换落盘，重启无损持久化。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import TYPE_CHECKING, Any

from server.core.errors import ConflictError, InvalidRequestError, NotFoundError
from server.core.ids import new_id
from server.core.plugins.models import PluginCollectionItem
from server.core.tenancy import tenant_directory

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from server.core.tenancy import Principal

_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}$")
_MAX_ITEMS_PER_COLLECTION = 1000
_MAX_RECORD_BYTES = 128 * 1024


def _validate_ident(value: str, label: str) -> str:
    """校验插件或集合名称，严防路径穿越。"""
    cleaned = value.strip()
    if not _SAFE_NAME_RE.fullmatch(cleaned) or ".." in cleaned:
        raise InvalidRequestError(f"{label}标识不合法：{value!r}")
    return cleaned


def _fs_segment(ident: str) -> str:
    """将带命名空间冒号的插件标识转为跨平台安全的单段目录名。"""
    return ident.replace(":", "__")


class PluginCollectionStore:
    """按 ``(tenant_id, plugin_id, user_id, collection)`` 隔离落盘的插件扩展集合存储。"""

    def __init__(self, data_dir: Path, *, now: Callable[[], float] = time.time) -> None:
        """绑定数据根目录与时钟。"""
        self._root = data_dir / "plugin_collections"
        self._now = now
        self._lock = asyncio.Lock()

    def _collection_path(
        self, who: Principal, plugin_id: str, collection: str,
    ) -> tuple[str, str, Path]:
        """计算私域插件集合的隔离文件路径并返回 ``(tenant_id, user_id, path)``。"""
        tenant = who.require_tenant()
        user_id = who.user_id or who.actor
        safe_plugin = _fs_segment(_validate_ident(plugin_id, "插件"))
        safe_coll = _fs_segment(_validate_ident(collection, "集合"))
        safe_user = "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in user_id)
        path = self._root / tenant_directory(tenant) / safe_plugin / safe_user / f"{safe_coll}.json"
        return tenant, user_id, path

    def _read_sync(self, path: Path) -> list[PluginCollectionItem]:
        """同步读取集合内全部条目。"""
        if not path.is_file():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [PluginCollectionItem.model_validate(item) for item in raw]

    def _write_sync(self, path: Path, items: list[PluginCollectionItem]) -> None:
        """原子写回集合文件并收紧目录/文件权限。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([i.model_dump(mode="json") for i in items], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    async def list(
        self, who: Principal, plugin_id: str, collection: str, *, limit: int = 100,
    ) -> list[PluginCollectionItem]:
        """列出本人在指定插件集合下的全部记录（按更新时间倒序）。"""
        _, _, path = self._collection_path(who, plugin_id, collection)
        items = await asyncio.to_thread(self._read_sync, path)
        items.sort(key=lambda r: r.updated_at, reverse=True)
        return items[: max(1, min(limit, _MAX_ITEMS_PER_COLLECTION))]

    async def get(
        self, who: Principal, plugin_id: str, collection: str, record_id: str,
    ) -> PluginCollectionItem:
        """按 ID 读取单条记录；不存在抛出 NotFoundError。"""
        _, _, path = self._collection_path(who, plugin_id, collection)
        items = await asyncio.to_thread(self._read_sync, path)
        for item in items:
            if item.record_id == record_id:
                return item
        raise NotFoundError("插件集合记录不存在")

    async def put(
        self,
        who: Principal,
        plugin_id: str,
        collection: str,
        data: dict[str, Any],
        *,
        record_id: str | None = None,
        expected_version: int | None = None,
    ) -> PluginCollectionItem:
        """新建或按乐观锁更新单条插件集合记录。"""
        if not isinstance(data, dict):
            raise InvalidRequestError("插件记录 data 必须是 JSON 对象")
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if len(encoded) > _MAX_RECORD_BYTES:
            raise InvalidRequestError(f"单条插件记录体积超过 {_MAX_RECORD_BYTES} 字节上限")

        tenant, user_id, path = self._collection_path(who, plugin_id, collection)
        async with self._lock:
            items = await asyncio.to_thread(self._read_sync, path)
            ts = self._now()
            if record_id is not None:
                for idx, existing in enumerate(items):
                    if existing.record_id == record_id:
                        if expected_version is not None and existing.version != expected_version:
                            raise ConflictError(
                                f"插件记录版本冲突（期望 {expected_version}，当前 {existing.version}）"
                            )
                        updated = existing.model_copy(
                            update={"data": dict(data), "version": existing.version + 1, "updated_at": ts}
                        )
                        items[idx] = updated
                        await asyncio.to_thread(self._write_sync, path, items)
                        return updated
                if expected_version is not None:
                    raise NotFoundError("要更新的插件集合记录不存在")
            if len(items) >= _MAX_ITEMS_PER_COLLECTION:
                raise InvalidRequestError(f"单个插件集合最多保存 {_MAX_ITEMS_PER_COLLECTION} 条记录")
            created = PluginCollectionItem(
                record_id=record_id or new_id("plr"),
                tenant_id=tenant,
                user_id=user_id,
                plugin_id=plugin_id.strip(),
                collection=collection.strip(),
                data=dict(data),
                version=1,
                created_at=ts,
                updated_at=ts,
            )
            items.append(created)
            await asyncio.to_thread(self._write_sync, path, items)
            return created

    async def delete(
        self, who: Principal, plugin_id: str, collection: str, record_id: str,
    ) -> bool:
        """删除本人一条插件集合记录；不存在抛出 NotFoundError。"""
        _, _, path = self._collection_path(who, plugin_id, collection)
        async with self._lock:
            items = await asyncio.to_thread(self._read_sync, path)
            remaining = [i for i in items if i.record_id != record_id]
            if len(remaining) == len(items):
                raise NotFoundError("插件集合记录不存在")
            await asyncio.to_thread(self._write_sync, path, remaining)
            return True
