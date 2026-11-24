"""员工概览（R5-E0）的只读客户端，复用原认证和传输。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import _Http


class DashboardClient:
    """主客户端继承同一传输；只读。"""

    _http: _Http

    async def dashboard(self, *, timezone: str | None = None) -> dict[str, Any]:
        """本人概览：``blocks`` 按 today、waiting、in_progress、unread 排列，关闭的块不出现。

        每块 ``status`` 为 ``ok``（带 ``count`` 与至多 5 条 ``items``）或 ``error``（带 ``error``）。
        """
        params = {} if timezone is None else {"timezone": timezone}
        return await self._http.request("GET", "/v1/dashboard", params=params)
