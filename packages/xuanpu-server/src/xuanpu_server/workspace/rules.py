"""工作台事务内的不变量、关联与回收规则。"""

from __future__ import annotations

import secrets
from copy import deepcopy
from typing import Any

from xuanpu_server.errors import ConflictError, InvalidRequestError, NotFoundError
from xuanpu_server.workspace.models import WorkspaceState, fingerprint


def public(item: dict[str, Any]) -> dict[str, Any]:
    """外部响应不泄露回收批次或存储位置。"""
    return deepcopy({k: v for k, v in item.items() if not k.startswith("_")})


def get_item(
    state: WorkspaceState, item_id: str, *, kind: str | None = None, active: bool = False
) -> dict[str, Any]:
    """仅从当前员工快照查记录，拒绝错类型或回收状态。"""
    item = state.items.get(item_id)
    if item is None or (kind is not None and item["kind"] != kind):
        raise NotFoundError("工作台记录不存在")
    if active and item["trashed_at"] is not None:
        raise ConflictError("记录已在回收站，请先恢复")
    return item


def check_version(item: dict[str, Any], version: int) -> None:
    """所有改变状态的请求都要求明确的当前版本。"""
    if type(version) is not int or version < 1:
        raise InvalidRequestError("version 必须为正整数")
    if item["version"] != version:
        raise ConflictError("记录版本已变化，请刷新后重试")


def check_relations(state: WorkspaceState, item: dict[str, Any]) -> None:
    """同属本人、活动引用、无目录环和兄弟名称唯一。"""
    data = item["data"]
    matter_id = data.get("matter_id")
    if matter_id is not None:
        get_item(state, matter_id, kind="matter", active=True)
    if item["kind"] not in ("folder", "file"):
        return
    parent = data.get("parent_id")
    visited = {item["id"]}
    while parent is not None:
        if parent in visited:
            raise ConflictError("目录不能移入自身或子目录")
        visited.add(parent)
        parent = get_item(state, parent, kind="folder", active=True)["data"]["parent_id"]
    for other in state.items.values():
        if (
            other["id"] != item["id"]
            and other["kind"] in ("folder", "file")
            and other["trashed_at"] is None
            and other["data"]["parent_id"] == data["parent_id"]
            and other["data"]["name"].casefold() == data["name"].casefold()
        ):
            raise ConflictError("同一目录已有同名文件或文件夹")


def request_key(value: str) -> str:
    """创建和内容替换的幂等键必须显式提供。"""
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise InvalidRequestError("request_id 必须为1至128字的非空字符串")
    return value


def replay(state: WorkspaceState, request_id: str, payload: Any) -> dict[str, Any] | None:
    """重复请求只返回原响应，不重复执行或记审计。"""
    old = state.requests.get(request_id)
    if old is None:
        return None
    if old["fingerprint"] != fingerprint(payload):
        raise ConflictError("request_id 已用于其他操作或内容")
    return dict(deepcopy(old["result"]))


def remember(state: WorkspaceState, request_id: str, payload: Any, result: dict[str, Any]) -> None:
    """幂等指纹与原响应同事务持久化。"""
    state.requests[request_id] = {"fingerprint": fingerprint(payload), "result": deepcopy(result)}


def new_item(kind: str, data: dict[str, Any], now: float) -> dict[str, Any]:
    """生成稳定、不含用户提供路径的记录标识。"""
    return {
        "id": "ws_" + secrets.token_hex(16),
        "kind": kind,
        "data": data,
        "version": 1,
        "created_at": now,
        "updated_at": now,
        "trashed_at": None,
    }


def audit(state: WorkspaceState, actor: str, action: str, now: float, item: dict[str, Any]) -> None:
    """只记录标识、类型和版本，不记录正文、名字或内容。"""
    state.audits.append(
        {
            "actor": actor,
            "kind": "workspace." + action,
            "at": now,
            "data": {k: item[k] for k in ("id", "kind", "version") if k in item},
        }
    )


def touch(item: dict[str, Any], now: float) -> None:
    """版本与更新时间始终一起前进。"""
    item["version"] += 1
    item["updated_at"] = now


def recycle(
    state: WorkspaceState, item: dict[str, Any], restoring: bool, now: float
) -> list[dict[str, Any]]:
    """目录整批回收/恢复；已回收子项保留原批次。"""
    if restoring:
        if item["trashed_at"] is None:
            raise ConflictError("记录没有在回收站")
        batch = item.get("_trash_batch")
        members = [v for v in state.items.values() if v.get("_trash_batch") == batch]
        if item["id"] != batch:
            raise ConflictError("请先恢复所属文件夹")
        for record in members:
            record["trashed_at"] = None
            record.pop("_trash_batch", None)
        for record in members:
            check_relations(state, record)
    else:
        if item["trashed_at"] is not None:
            raise ConflictError("记录已在回收站")
        if item["kind"] == "matter" and any(
            v["data"].get("matter_id") == item["id"] for v in state.items.values()
        ):
            raise ConflictError("事项仍有关联记录，请先解除关联（包括回收站记录）")
        ids = {item["id"]}
        while True:
            children = {
                v["id"]
                for v in state.items.values()
                if v["kind"] in ("folder", "file")
                and v["trashed_at"] is None
                and v["data"].get("parent_id") in ids
            }
            if children <= ids:
                break
            ids |= children
        members = [state.items[i] for i in ids]
        for record in members:
            record["trashed_at"] = now
            record["_trash_batch"] = item["id"]
    for record in members:
        touch(record, now)
    return members
