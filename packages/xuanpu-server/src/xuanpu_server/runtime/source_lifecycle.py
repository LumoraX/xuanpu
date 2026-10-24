"""维护循环回收来源失效的引擎和缓存，不在模型回调内关闭自身。"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xuanpu_server.runtime.sessions import SessionManager

logger = logging.getLogger(__name__)


async def release_revoked(manager: SessionManager) -> list[str]:
    """授权不可用即停止旧引擎；权限仓储故障也不继续使用缓存。"""
    released = []
    for key, live in list(manager._live.items()):
        binding = live.runtime.source
        if binding is None or not binding.protected:
            continue
        try:
            await binding.check()
        except Exception as exc:
            logger.info('共享来源引擎需要退出: %s', type(exc).__name__)
            async with manager._lock_for(key):
                if manager._live.get(key) is live:
                    await manager._detach(key, force=True)
                    await manager._runtimes.release_scope(live.runtime.key)
                    released.append(key[1])
    return released


async def release_idle(manager: SessionManager) -> list[str]:
    """员工会话空闲回收沿用原引擎释放规则及持有权顺序。"""
    deadline = manager._now() - manager._runtimes.defaults.session_idle_seconds
    released = []
    for key, live in list(manager._live.items()):
        if live.idle and live.last_used <= deadline:
            async with manager._lock_for(key):
                current = manager._live.get(key)
                if current is live and live.idle and await manager._detach(key, force=False):
                    released.append(key[1])
    return released
