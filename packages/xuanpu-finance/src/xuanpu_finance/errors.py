"""财务边界的可传输领域错误。"""

from __future__ import annotations


class FinanceError(Exception):
    """携带稳定错误码和 HTTP 状态的领域异常。"""

    def __init__(self, code: str, status: int, message: str) -> None:
        """保留领域错误的结构，消息中包含可检索错误码。"""
        self.code = code
        self.status = status
        self.message = message
        super().__init__(f"{code}: {message}")

    def __reduce__(self) -> tuple[type[FinanceError], tuple[str, int, str]]:
        """保留结构化构造参数，使隔离解析子进程能还原同一领域错误。"""
        return type(self), (self.code, self.status, self.message)
