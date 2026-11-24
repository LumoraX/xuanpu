"""客户端异常。"""

from __future__ import annotations

from typing import Any


class XuanpuError(Exception):
    """平台返回了错误响应。

    Attributes:
        status: HTTP 状态码。
        code: 平台的错误码（见 HTTP 接口文档）。
        message: 提示文案。
        details: 错误响应里的其余字段（如业务包校验的 ``problems``、会话持有者 ``owner``）。
    """

    def __init__(self, status: int, code: str, message: str, details: dict[str, Any]) -> None:
        """记录错误响应。"""
        super().__init__(f"{code}（HTTP {status}）：{message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details


class XuanpuTransportError(XuanpuError):
    """连不上平台，或响应不是平台的格式。"""

    def __init__(self, message: str) -> None:
        """记录原因。"""
        super().__init__(0, "transport_error", message, {})


__all__ = ["XuanpuError", "XuanpuTransportError"]
