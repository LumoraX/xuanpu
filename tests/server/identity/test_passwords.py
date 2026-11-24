"""口令摘要（``identity.passwords``）：核对与重算、并发上限排队超时、口令规则、损坏的摘要。"""

from __future__ import annotations

import anyio
import pytest
from argon2 import PasswordHasher

from server.core.errors import InvalidRequestError, PlatformError, RuntimeUnavailableError
from server.domains.identity.passwords import PasswordHashing, validate_new_password

CHEAP = {"time_cost": 1, "memory_cost": 1024, "parallelism": 1}


async def test_verify_and_rehash_when_parameters_change() -> None:
    """对的口令匹配、错的不匹配；参数升级后匹配时顺手给出新摘要。"""
    old = PasswordHashing(PasswordHasher(**CHEAP))
    stored = await old.hash("a long enough secret")
    assert stored.startswith("$argon2id$")
    assert await old.verify(stored, "a long enough secret") == (True, None)
    assert await old.verify(stored, "wrong secret here") == (False, None)
    upgraded = PasswordHashing(PasswordHasher(time_cost=2, memory_cost=1024, parallelism=1))
    matched, rehashed = await upgraded.verify(stored, "a long enough secret")
    assert matched and rehashed is not None and rehashed != stored
    await upgraded.verify_absent("anything")


async def test_corrupt_hash_is_an_internal_error() -> None:
    """库里的摘要格式不对不当作口令错吞掉，报内部错误。"""
    hashing = PasswordHashing(PasswordHasher(**CHEAP))
    with pytest.raises(PlatformError):
        await hashing.verify("not-a-phc-string", "whatever")


async def test_queue_timeout_reports_busy() -> None:
    """名额被占满时排队超时报 503。"""
    hashing = PasswordHashing(PasswordHasher(**CHEAP), max_concurrent=1, queue_seconds=0.05)
    await hashing._slots.acquire()
    try:
        with pytest.raises(RuntimeUnavailableError):
            await hashing.hash("a long enough secret")
    finally:
        hashing._slots.release()
    with anyio.fail_after(5):
        assert await hashing.hash("a long enough secret")


def test_password_rules() -> None:
    """至少 10 个字符、最多 128 个，不能与用户名相同。"""
    validate_new_password("0123456789", username="zhang")
    for bad in ("short", "x" * 129, "ZhangSan01"):
        with pytest.raises(InvalidRequestError):
            validate_new_password(bad, username="zhangsan01")
