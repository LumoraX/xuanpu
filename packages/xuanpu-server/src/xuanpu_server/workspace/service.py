"""个人工作台用例：严格模型、本人权限及原子领域操作。"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from xuanpu_server.errors import ConflictError, InvalidRequestError, PermissionDeniedError
from xuanpu_server.workspace.models import (
    KINDS,
    MAX_FILE_BYTES,
    QUOTA_BYTES,
    MessageInput,
    SettingsData,
    validate_data,
)
from xuanpu_server.workspace.rules import (
    apply_todo_change,
    audit,
    check_relations,
    check_version,
    get_item,
    new_item,
    public,
    recycle,
    remember,
    replay,
    request_key,
    touch,
)

if TYPE_CHECKING:
    import builtins
    from collections.abc import AsyncIterator, Callable

    from xuanpu_server.controlplane.store import ControlPlaneStore
    from xuanpu_server.identity.store import IdentityStore
    from xuanpu_server.objectstore import ObjectStore
    from xuanpu_server.tenancy import Principal
    from xuanpu_server.workspace.models import WorkspaceState
    from xuanpu_server.workspace.store import WorkspaceStore


class WorkspaceService:
    """所有入口复用员工授权，管理员也没有越权读取入口。"""

    def __init__(
        self,
        *,
        store: WorkspaceStore,
        control: ControlPlaneStore,
        identity: IdentityStore,
        objects: ObjectStore,
        now: Callable[[], float] = time.time,
    ) -> None:
        """注入同后端元数据、对象存储、身份和时钟。"""
        self.store, self._control, self._identity, self.objects = store, control, identity, objects
        self._now = now
        self.quota_bytes, self.max_file_bytes = QUOTA_BYTES, MAX_FILE_BYTES
        self.max_versions = 10000
        from .task_usage import bind_memory_usage

        bind_memory_usage(store, control, identity)

    async def require_employee(self, who: Principal) -> tuple[str, str]:
        """拒绝机器身份、停用员工与暂停租户。"""
        if who.tenant_id is None or who.user_id is None:
            raise PermissionDeniedError("个人工作台需要员工身份")
        tenant = await self._control.get_tenant(who.tenant_id)
        user = await self._identity.get_user(who.tenant_id, who.user_id)
        if tenant is None or tenant.status != "active" or user is None or user.status != "active":
            raise PermissionDeniedError("员工或租户不可用")
        return who.tenant_id, who.user_id

    @asynccontextmanager
    async def transaction(
        self, who: Principal, request_id: str | None = None
    ) -> AsyncIterator[WorkspaceState]:
        """向平台领域开放同员工原子事务，复用身份锁、关系检查和审计提交。"""
        async with self._transaction(who, request_id) as state:
            yield state

    @asynccontextmanager
    async def _transaction(
        self, who: Principal, request_id: str | None = None
    ) -> AsyncIterator[WorkspaceState]:
        """员工锁内再次验证身份，数据库额外加身份行锁。"""
        tenant, user = await self.require_employee(who)

        async def guard() -> None:
            """内存事务提交前重新验证当前身份。"""
            await self.require_employee(who)

        from xuanpu_server.work_tasks.execution_context import current_execution
        from xuanpu_server.work_tasks.execution_store import assert_lease

        binding = current_execution.get()
        async with self.store.transaction(tenant, user, guard, request_id) as state:
            if binding is not None:
                if (tenant, user) != (binding.lease.tenant_id, binding.lease.user_id):
                    raise PermissionDeniedError('执行身份不能跨员工或租户')
                assert_lease(state, binding.lease, binding.at(state))
            yield state
            if binding is not None:
                assert_lease(state, binding.lease, binding.at(state))

    async def create(
        self,
        who: Principal,
        kind: str,
        data: dict[str, Any],
        request_id: str,
        *,
        proposal_id: str | None = None,
    ) -> dict[str, Any]:
        """创建原生记录，并可将建议确认决定与记录放在同一事务。"""
        request_key(request_id)
        checked = validate_data(kind, data)
        if kind == "file" and (
            checked["source_url"] is None
            or any(k in data for k in ("size", "current_revision", "mime_type"))
        ):
            raise InvalidRequestError("文件正文请通过上传入口创建，链接仅接受名称和 HTTPS 地址")
        decision_key = None
        if proposal_id is not None:
            request_key(proposal_id)
            decision_key = request_key("proposal:" + proposal_id)
            if request_id != proposal_id + ":create":
                raise InvalidRequestError("建议创建请求标识不匹配")
        async with self._transaction(who, decision_key or request_id) as state:
            from .creation import create_in_state

            return create_in_state(state, who.actor, kind, checked, request_id, self._now(), proposal_id)

    async def list(
        self,
        who: Principal,
        *,
        kind: str | None = None,
        trashed: bool = False,
        search: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """按类型、回收状态与标题/正文查询本人记录。"""
        if kind is not None and kind not in KINDS:
            raise InvalidRequestError("工作台记录类型无效")
        if search is not None and len(search) > 500:
            raise InvalidRequestError("搜索文字过长")
        async with self._transaction(who) as state:
            found = [
                v
                for v in state.items.values()
                if (kind is None or v["kind"] == kind) and (v["trashed_at"] is not None) == trashed
            ]
            if search:
                found = [
                    v
                    for v in found
                    if search.casefold()
                    in " ".join(
                        str(v["data"].get(k, "")) for k in ("name", "title", "description", "tags")
                    ).casefold()
                ]
            return [
                public(v)
                for v in sorted(found, key=lambda v: (v["updated_at"], v["id"]), reverse=True)
            ]

    async def get(self, who: Principal, item_id: str) -> dict[str, Any]:
        """读取本人记录，包括明确标记的回收状态。"""
        async with self._transaction(who) as state:
            return public(get_item(state, item_id))

    async def update(
        self, who: Principal, item_id: str, data: dict[str, Any], version: int
    ) -> dict[str, Any]:
        """部分更新可编辑字段；内容派生字段不可伪造。"""
        async with self._transaction(who) as state:
            item = get_item(state, item_id, active=True)
            check_version(item, version)
            if item["kind"] == "file" and any(
                k in data for k in ("mime_type", "size", "current_revision", "source_url")
            ):
                raise InvalidRequestError("文件内容字段只能通过文件上传修改")
            apply_todo_change(state, item, data, self._now())
            audit(state, who.actor, "updated", self._now(), item)
            return public(item)

    async def trash(self, who: Principal, item_id: str, version: int) -> dict[str, Any]:
        """递归移入回收站，不删除任何文件正本。"""
        return await self._recycle(who, item_id, version, False)

    async def restore(self, who: Principal, item_id: str, version: int) -> dict[str, Any]:
        """恢复同批回收成员，存在名称冲突时整体回滚。"""
        return await self._recycle(who, item_id, version, True)

    async def _recycle(
        self, who: Principal, item_id: str, version: int, restoring: bool
    ) -> dict[str, Any]:
        """回收和恢复共用版本、批次和审计事务。"""
        async with self._transaction(who) as state:
            item = get_item(state, item_id)
            check_version(item, version)
            for changed in recycle(state, item, restoring, self._now()):
                audit(
                    state, who.actor, "restored" if restoring else "trashed", self._now(), changed
                )
            return public(item)

    async def upload(
        self,
        who: Principal,
        name: str,
        content: bytes,
        mime_type: str,
        request_id: str,
        parent_id: str | None = None,
        *, matter_id: str | None = None,
    ) -> dict[str, Any]:
        """保存第一版正本，名称不能成为对象存储路径。"""
        checked = validate_data(
            "file", {"name": name, "parent_id": parent_id, "mime_type": mime_type, "matter_id": matter_id}
        )
        return await self._write_content(who, content, mime_type, request_id, checked=checked)

    async def replace_content(
        self,
        who: Principal,
        item_id: str,
        content: bytes,
        mime_type: str,
        version: int,
        request_id: str,
    ) -> dict[str, Any]:
        """新建不可变版本，旧版本仍可下载且计入空间。"""
        return await self._write_content(
            who, content, mime_type, request_id, item_id=item_id, version=version
        )

    async def _write_content(
        self,
        who: Principal,
        content: bytes,
        mime_type: str,
        request_id: str,
        *,
        checked: dict[str, Any] | None = None,
        item_id: str | None = None,
        version: int | None = None,
    ) -> dict[str, Any]:
        """先锁员工验证配额，再写唯一对象，最后原子提交元数据。"""
        request_key(request_id)
        if len(content) > self.max_file_bytes:
            raise InvalidRequestError("单个文件不能超过20 MiB")
        checksum = hashlib.sha256(content).hexdigest()
        payload = {
            "op": "content",
            "id": item_id,
            "version": version,
            "data": checked,
            "mime_type": mime_type,
            "sha256": checksum,
        }
        async with self._transaction(who, request_id) as state:
            previous = replay(state, request_id, payload)
            if previous is not None:
                return previous
            if checked is None:
                item = get_item(state, item_id or "", kind="file", active=True)
                check_version(item, version if version is not None else 0)
                if item["data"]["source_url"] is not None:
                    raise ConflictError("外部链接不能替换为本地文件，请上传新文件")
                touch(item, self._now())
            else:
                if len(state.items) >= 10000:
                    raise ConflictError("每人最多保存10000条工作台记录")
                item = new_item("file", checked, self._now())
            check_relations(state, item)
            versions = state.versions.setdefault(item["id"], [])
            if sum(len(v) for v in state.versions.values()) >= self.max_versions:
                raise ConflictError("个人文件历史版本总数已达上限")
            if len(versions) >= 1000:
                raise ConflictError("单个文件最多保存1000个版本")
            if self._used_bytes(state) + len(content) > self.quota_bytes:
                raise ConflictError("个人文件空间不足，历史版本和回收站同样计入空间")
            revision = len(versions) + 1
            item["data"] = validate_data(
                "file",
                {
                    **item["data"],
                    "current_revision": revision,
                    "size": len(content),
                    "mime_type": mime_type,
                },
            )
            key = f"{who.tenant_id}/workspace/{item['id']}/{secrets.token_hex(16)}"
            await self.objects.put(key, content, content_type=mime_type)
            versions.append(
                {
                    "revision": revision,
                    "size": len(content),
                    "mime_type": mime_type,
                    "created_at": self._now(),
                    "sha256": checksum,
                    "object_key": key,
                }
            )
            state.items[item["id"]] = item
            result = public(item)
            remember(state, request_id, payload, result)
            audit(state, who.actor, "content_saved", self._now(), item)
            return result

    async def versions(self, who: Principal, item_id: str) -> builtins.list[dict[str, Any]]:
        """文件版本列表不暴露底层对象键。"""
        async with self._transaction(who) as state:
            get_item(state, item_id, kind="file")
            return [
                {k: v for k, v in r.items() if k != "object_key"}
                for r in state.versions.get(item_id, [])
            ]

    async def download(
        self, who: Principal, item_id: str, revision: int | None = None
    ) -> tuple[bytes, str, str]:
        """始终读取已有对象，任何链接都不由服务端抓取。"""
        from xuanpu_server.errors import NotFoundError

        async with self._transaction(who) as state:
            item = get_item(state, item_id, kind="file")
            wanted = item["data"]["current_revision"] if revision is None else revision
            chosen = next(
                (r for r in state.versions.get(item_id, []) if r["revision"] == wanted), None
            )
            if chosen is None:
                raise NotFoundError("文件版本不存在，外部链接请前往原地址")
            from xuanpu_server.objectstore.base import ObjectTooLargeError

            try:
                content = await self.objects.get_limited(chosen["object_key"], max_bytes=self.max_file_bytes)
            except ObjectTooLargeError as exc:
                raise InvalidRequestError("文件正本超过大小限制") from exc
            return content, chosen["mime_type"], item["data"]["name"]

    @staticmethod
    def _used_bytes(state: WorkspaceState) -> int:
        """所有历史版本计入空间，不因回收而扣减。"""
        return sum(int(r["size"]) for versions in state.versions.values() for r in versions)

    async def summary(self, who: Principal) -> dict[str, Any]:
        """本人的活动数量与包括回收站的实际文件用量。"""
        async with self._transaction(who) as state:
            counts = {
                kind: sum(
                    v["kind"] == kind and v["trashed_at"] is None for v in state.items.values()
                )
                for kind in KINDS
            }
            return {
                "counts": counts,
                "used_bytes": self._used_bytes(state),
                "quota_bytes": self.quota_bytes,
                "max_file_bytes": self.max_file_bytes,
            }

    async def settings(self, who: Principal) -> dict[str, Any]:
        """默认设置同样具有明确版本1。"""
        async with self._transaction(who) as state:
            return dict(state.settings)

    async def update_settings(
        self, who: Principal, data: dict[str, Any], version: int
    ) -> dict[str, Any]:
        """偏好按当前版本更新，字段采用严格模型。"""
        async with self._transaction(who) as state:
            check_version(state.settings, version)
            old = {k: v for k, v in state.settings.items() if k != "version"}
            try:
                checked = SettingsData.model_validate({**old, **data}).model_dump()
            except ValidationError as exc:
                raise InvalidRequestError("助手偏好字段或时区无效") from exc
            state.settings = {**checked, "version": version + 1}
            audit(state, who.actor, "settings_updated", self._now(), state.settings)
            return dict(state.settings)

    async def messages(
        self,
        who: Principal,
        after_id: int = 0,
        limit: int = 100,
        *,
        latest: bool = False,
        matter_id: str | None = None,
        before_id: int | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """有界游标读取本人持久主对话。"""
        tenant, user = await self.require_employee(who)
        if (
            type(after_id) is not int
            or not 0 <= after_id <= 9223372036854775807
            or type(limit) is not int
            or not 1 <= limit <= 100
            or (
                before_id is not None
                and (type(before_id) is not int or not 1 <= before_id <= 9223372036854775807)
            )
        ):
            raise InvalidRequestError("主对话分页范围无效")
        if matter_id is not None:
            async with self._transaction(who) as state:
                get_item(state, matter_id, kind="matter")
        result = await self.store.messages(
            tenant, user, after_id, limit, matter_id=matter_id, before_id=before_id, latest=latest
        )
        await self.require_employee(who)
        return result

    async def append_message(
        self,
        who: Principal,
        role: str,
        text: str,
        request_id: str,
        attachments: builtins.list[str] | None = None,
        matter_id: str | None = None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """保存主对话及本人附件关联；助手角色仅供服务内部调用。"""
        request_key(request_id)
        try:
            data = MessageInput.model_validate(
                {
                    "role": role,
                    "text": text,
                    "attachments": attachments or [],
                    "matter_id": matter_id,
                }
            ).model_dump()
        except ValidationError as exc:
            raise InvalidRequestError("主对话输入无效") from exc
        try:
            encoded = json.dumps(metadata or {}, allow_nan=False)
            if len(encoded.encode()) > 65536:
                raise ValueError("结果元数据过大")
            data["metadata"] = json.loads(encoded)
        except (ValueError, TypeError, RecursionError) as exc:
            raise InvalidRequestError("主对话结果元数据无效或超过64 KiB") from exc
        payload = {"op": "message", **data}
        async with self._transaction(who, request_id) as state:
            previous = replay(state, request_id, payload)
            if previous is not None:
                return previous
            for attachment in data["attachments"]:
                get_item(state, attachment, kind="file", active=True)
            if matter_id is not None:
                get_item(state, matter_id, kind="matter", active=True)
            result = {
                "id": state.next_message_id,
                **data,
                "created_at": self._now(),
                "request_id": request_id,
            }
            state.next_message_id += 1
            state.messages.append(result)
            remember(state, request_id, payload, result)
            audit(state, who.actor, "message_saved", self._now(), result)
            return dict(result)

    async def find_message(self, who: Principal, request_id: str) -> dict[str, Any] | None:
        """按请求恢复本人已持久化的助手结果。"""
        tenant, user = await self.require_employee(who)
        request_key(request_id)
        result = await self.store.find_message(tenant, user, request_id)
        await self.require_employee(who)
        return result

    async def pending_proposals(self, who: Principal) -> builtins.list[dict[str, Any]]:
        """刷新后恢复尚未处理的助手建议，始终只读本人。"""
        tenant, user = await self.require_employee(who)
        result = await self.store.pending_proposals(tenant, user)
        await self.require_employee(who)
        return result

    async def reserve_proposal(
        self, who: Principal, request_id: str, action: str, data: dict[str, Any]
    ) -> None:
        """确认和取消先原子冻结动作与内容，跨进程重试只能继续同一操作。"""
        request_key(request_id)
        key = request_key("proposal:" + request_id)
        if action not in ("confirm", "cancel") or not isinstance(data, dict):
            raise InvalidRequestError("建议只支持确认或取消，内容必须是对象")
        try:
            encoded = json.dumps(data, allow_nan=False)
            if len(encoded.encode()) > 65536:
                raise ValueError("建议数据过大")
            payload = {"action": action, "data": json.loads(encoded)}
        except (ValueError, TypeError, RecursionError) as exc:
            raise InvalidRequestError("建议数据无效或超过64 KiB") from exc
        async with self._transaction(who, key) as state:
            if action == "cancel" and request_id + ":create" in state.requests:
                raise ConflictError("该建议已创建，不能再取消")
            if replay(state, key, payload) is not None:
                return
            remember(state, key, payload, {"state": "cancelled"} if action == "cancel" else {})
            audit(state, who.actor, "proposal_reserved", self._now(), {})

    async def proposal_result(self, who: Principal, request_id: str) -> dict[str, Any] | None:
        """读取与领域提交一起保存的权威结果，不依赖终态聊天消息。"""
        tenant, user = await self.require_employee(who)
        request_key(request_id)
        result = await self.store.proposal_result(tenant, user, request_id)
        await self.require_employee(who)
        return result

    async def claim_route(
        self, who: Principal, request_id: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        """短事务认领一次路由，跨进程去重且完整请求指纹不可变。"""
        key = request_key("route:" + request_key(request_id))
        payload = {"op": "route", "input": data}
        async with self._transaction(who, key) as state:
            prior = replay(state, key, payload)
            if prior is not None and prior.get("state") == "complete":
                return prior
            if prior is not None and prior.get("lease_until", 0) > self._now():
                raise ConflictError("该消息正在处理中，请稍后重试")
            claim = {
                **(prior or {}),
                "state": "claimed",
                "token": secrets.token_hex(16),
                "lease_until": self._now() + 60,
            }
            remember(state, key, payload, claim)
            return dict(claim)

    async def _change_route(
        self, who: Principal, request_id: str, token: str, changes: dict[str, Any]
    ) -> None:
        """租约令牌隔离迟到工作者，更新不改变原始输入指纹。"""
        key = request_key("route:" + request_key(request_id))
        async with self._transaction(who, key) as state:
            record = state.requests.get(key)
            if record is None or record["result"].get("token") != token:
                raise ConflictError("消息处理权已变化，请重试读取结果")
            record["result"] = {**record["result"], **changes}
            if "choice" in changes and changes["choice"] is None:
                record["result"].pop("choice", None)
                record["result"].pop("catalog", None)

    async def save_route_choice(
        self,
        who: Principal,
        request_id: str,
        token: str,
        choice: dict[str, Any],
        catalog: builtins.list[dict[str, str]],
    ) -> None:
        """任何会话副作用之前持久保存选择和版本目录。"""
        await self._change_route(who, request_id, token, {"choice": choice, "catalog": catalog})

    async def complete_route(
        self, who: Principal, request_id: str, token: str, outcome: dict[str, Any]
    ) -> None:
        """先持久保存路由结果，再追加可恢复的展示消息。"""
        await self._change_route(
            who, request_id, token, {"state": "complete", "outcome": outcome, "lease_until": 0}
        )

    async def release_route(self, who: Principal, request_id: str, token: str) -> None:
        """失败或取消释放认领；进程崩溃则最多等待六十秒租约。"""
        key = request_key("route:" + request_key(request_id))
        async with self._transaction(who, key) as state:
            record = state.requests.get(key)
            if record is not None and record["result"].get("token") == token:
                record["result"]["lease_until"] = 0

    async def discard_route_choice(self, who: Principal, request_id: str, token: str) -> None:
        """未产生会话的解析错误可以重采样，不把无效选择永久缓存。"""
        await self._change_route(who, request_id, token, {"choice": None})
