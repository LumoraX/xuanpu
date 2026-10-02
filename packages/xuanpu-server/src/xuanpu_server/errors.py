"""平台的异常类型。每个异常带一个稳定的错误码与 HTTP 状态码，接入层据此统一成响应。"""

from __future__ import annotations


class PlatformError(Exception):
    """平台异常的基类。

    Attributes:
        code: 稳定的错误码，供调用方按码处理；不随提示文案变化。
        status: 对应的 HTTP 状态码。
    """

    code = "internal_error"
    status = 500

    def __init__(self, message: str) -> None:
        """记录提示文案。文案会返回给调用方，不得包含密钥明文或其他租户的信息。"""
        super().__init__(message)
        self.message = message


class InvalidRequestError(PlatformError):
    """请求本身不合法。"""

    code = "invalid_request"
    status = 400


class AuthenticationError(PlatformError):
    """没有凭据或凭据无效。"""

    code = "unauthenticated"
    status = 401


class LoginSessionInvalidError(AuthenticationError):
    """登录 cookie 认不出调用者：会话不存在或已过期、员工已停用、租户已停用。

    对外与 ``AuthenticationError`` 完全相同（同一个 401、同一句提示，不透露是哪一步没过）；
    接入层见到它时另外清掉浏览器里的登录 cookie。
    """


class PermissionDeniedError(PlatformError):
    """凭据有效，但无权做这个操作。"""

    code = "permission_denied"
    status = 403


class CsrfRejectedError(PermissionDeniedError):
    """用登录 cookie 认证的写请求没过防跨站请求伪造的检查（见 ``identity.cookies``）。

    单独一个错误码，让控制台能分清「没权限」与「请求的形状不对」（缺 ``X-Requested-By``
    头，或 ``Origin`` 与平台对外地址不符）。
    """

    code = "csrf_rejected"


class NotFoundError(PlatformError):
    """资源不存在。访问其他租户的资源同样报这个错，不泄露其存在与否。"""

    code = "not_found"
    status = 404


class ConflictError(PlatformError):
    """与现有状态冲突（重复发布、会话已关闭等）。"""

    code = "conflict"
    status = 409


class SessionBusyError(ConflictError):
    """会话正由另一个实例持有。

    Attributes:
        owner: 当前持有者的实例标识，供路由层把请求转过去。
    """

    code = "session_busy"

    def __init__(self, message: str, *, owner: str) -> None:
        """记录当前持有者。"""
        super().__init__(message)
        self.owner = owner


class QuotaExceededError(PlatformError):
    """超出租户配额。"""

    code = "quota_exceeded"
    status = 429


class PackageInvalidError(InvalidRequestError):
    """业务包不符合规范。

    Attributes:
        problems: 逐条的问题描述。
    """

    code = "package_invalid"

    def __init__(self, message: str, problems: tuple[str, ...] = ()) -> None:
        """记录问题明细。"""
        super().__init__(message)
        self.problems = problems


class SecretMissingError(InvalidRequestError):
    """业务包引用的密钥尚未配置。"""

    code = "secret_missing"


class RuntimeUnavailableError(PlatformError):
    """运行时依赖不可用（沙盒后端、连接器进程、模型网关配置等）。"""

    code = "runtime_unavailable"
    status = 503


class LeaseInterruptedError(RuntimeUnavailableError):
    """本实例对会话的持有中断过：上一次确认的租约到期之后才再次确认，或会话在此期间被改过。

    中间可能有别的实例接手、写过、又放了手，本实例刚准备的引擎里的历史不可信，这次请求
    作废。直接重试即可（重试时从存储重新续接），所以对外与运行时暂不可用相同：503
    ``runtime_unavailable``。
    """


class SchemaError(RuntimeError):
    """控制面表结构的迁移失败，或表结构落后于代码要求。"""


__all__ = [
    "AuthenticationError",
    "ConflictError",
    "CsrfRejectedError",
    "InvalidRequestError",
    "LeaseInterruptedError",
    "LoginSessionInvalidError",
    "NotFoundError",
    "PackageInvalidError",
    "PermissionDeniedError",
    "PlatformError",
    "QuotaExceededError",
    "RuntimeUnavailableError",
    "SchemaError",
    "SecretMissingError",
    "SessionBusyError",
]
