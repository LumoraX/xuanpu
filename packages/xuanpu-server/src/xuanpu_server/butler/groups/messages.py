"""会诊消息区（R5-D2）：只追加的消息、存储契约与内存实现。

- 消息只有两类作者：成员（发布的结论或追问回复）与管家（收口说明）。成员看不到彼此的内部
  过程，只看得到发到这里的内容。
- 消息按 ``(编组, key)`` 去重：同一事实（某次派工的结论、某步的收口）只追加一次，进程在追加
  前后被终止，重做不会多出第二条。序号在编组内从 1 递增，由存储分配。
- 消息内容是数据，不是指令：不改变计划、权限或任何成员的资格。
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime  # noqa: TC003 —— pydantic 在运行时解析字段类型
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from xuanpu_server.errors import NotFoundError

if TYPE_CHECKING:
    from xuanpu_server.tracing.models import TraceOwner

Author = Literal["member", "butler"]
MESSAGE_MAX = 3000
_KEY = re.compile(r"^[A-Za-z0-9_.:@/-]{1,200}$")


class GroupMessage(BaseModel):
    """消息区的一条消息。

    Attributes:
        seq: 编组内序号（存储分配；追加前为 0）。
        key: 去重键（例如 ``pub:<派工>``、``close:<步>``）。
        author: 成员或管家。
        step: 第几步。
        member: 作者是成员时的成员下标（从 0 起）。
        round: 第几轮：成员首个结论为 0，第 n 次追问的回复为 n；管家收口沿用触发它的轮次。
        mentions: 这条消息 @ 到的同一步成员下标。
        text: 内容（数据）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    group_id: str
    tenant_id: str
    user_id: str
    seq: int = Field(default=0, ge=0)
    key: str
    author: Author
    step: int = Field(ge=0)
    member: int | None = Field(default=None, ge=0)
    round: int = Field(default=0, ge=0)
    mentions: tuple[int, ...] = ()
    text: str = Field(min_length=1, max_length=MESSAGE_MAX)
    created_at: datetime

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        """去重键走有限字符集。"""
        if not _KEY.fullmatch(value):
            raise ValueError("消息键格式不对")
        return value


class MessageStore(Protocol):
    """会诊消息存储。"""

    async def append(self, owner: TraceOwner, message: GroupMessage) -> tuple[GroupMessage, bool]:
        """追加或回放同键消息；返回 ``(消息, 是否新追加)``。"""
        ...

    async def list(self, owner: TraceOwner, group_id: str) -> tuple[GroupMessage, ...]:
        """本人编组的全部消息，按序号。"""
        ...


def check_message_owner(owner: TraceOwner, message: GroupMessage) -> None:
    """消息归属必须与调用方一致（他人的与不存在的一样 404，不泄露存在性）。"""
    if (message.tenant_id, message.user_id) != (owner.tenant_id, owner.user_id):
        raise NotFoundError("编组不存在")


class MemoryMessageStore:
    """单进程内存实现。"""

    def __init__(self) -> None:
        """空存储。"""
        self._messages: dict[tuple[str, str], list[GroupMessage]] = {}
        self._lock = asyncio.Lock()

    async def append(self, owner: TraceOwner, message: GroupMessage) -> tuple[GroupMessage, bool]:
        """同键回放；否则分配下一个序号。"""
        check_message_owner(owner, message)
        async with self._lock:
            found = self._messages.setdefault((message.tenant_id, message.group_id), [])
            for existing in found:
                if existing.key == message.key:
                    check_message_owner(owner, existing)
                    return existing, False
            stored = message.model_copy(update={"seq": len(found) + 1})
            found.append(stored)
            return stored, True

    async def list(self, owner: TraceOwner, group_id: str) -> tuple[GroupMessage, ...]:
        """本人的消息。"""
        found = self._messages.get((owner.tenant_id, group_id), [])
        return tuple(m for m in found if m.user_id == owner.user_id)
