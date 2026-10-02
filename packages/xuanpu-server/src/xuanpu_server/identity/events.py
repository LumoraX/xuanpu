"""飞书事件回调的纯计算：验签、解密、认出 URL 验证与事件、解析员工离职事件（设计第 3 节、7.3）。

只做计算，不碰存储、不读时钟、不写日志；编排（取配置与密钥、核对来源、停用）在
``identity.callback``。

**报文**（设计第 3 节已核对加密与签名；URL 验证与重推是 2026-10-02 对照飞书开放平台「事件概述」
「将事件发送至开发者服务器」文档核对的，见 ``identity.feishu`` 的模块说明）：

- 配了 Encrypt Key 后请求体是 ``{"encrypt": "<base64>"}``：AES-256-CBC，密钥是 Encrypt Key（UTF-8）
  的 SHA-256，IV 是密文的前 16 字节，PKCS7 填充；解出来是 UTF-8 的 JSON。飞书文档的样例（Encrypt
  Key ``test key`` 加密 ``hello world``）有测试守着。
- 签名在请求头 ``X-Lark-Signature``：``sha256(X-Lark-Request-Timestamp + X-Lark-Request-Nonce +
  Encrypt Key + 原始请求体)`` 的十六进制。
- URL 验证解密后是 ``{"challenge", "token", "type": "url_verification"}``，要在 1 秒内原样回
  ``{"challenge": …}``。**它不带签名头**：官方 Python SDK ``lark-oapi`` 的
  ``EventDispatcherHandler.do`` 先解密、处理 URL 验证，之后才验签；有人抓包确认飞书发来的 URL 验证
  有加密体、没有 ``X-Lark-Signature``（openclaw/openclaw#58905）。
- 2.0 版事件：``{"schema": "2.0", "header": {"event_id", "token", "create_time", "event_type",
  "tenant_key", "app_id"}, "event": {…}}``。员工离职 ``contact.user.deleted_v3`` 的 ``event.object``
  里有 ``open_id``、``union_id``、``user_id`` 与 ``status``（``is_frozen``、``is_resigned``、
  ``is_activated``、``is_exited``、``is_unjoin``）。

**错误**：请求体或解出来的内容不合预期一律报 ``EventFormatError``，异常文本只说哪一步、不带请求里的
内容。JSON 嵌套过深（``RecursionError``）、明文不是 UTF-8 也算在内，不会变成 500。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

if TYPE_CHECKING:
    from xuanpu_server.identity.models import DisabledReason

SIGNATURE_HEADER = "X-Lark-Signature"
TIMESTAMP_HEADER = "X-Lark-Request-Timestamp"
NONCE_HEADER = "X-Lark-Request-Nonce"
URL_VERIFICATION = "url_verification"
USER_DELETED = "contact.user.deleted_v3"
"""员工离职事件的类型。"""
MAX_EVENT_BODY_BYTES = 1024 * 1024
"""事件回调请求体的上限：飞书的事件只有几 KB，超过 1 MiB 不读完、不验签，直接拒绝。"""
_BLOCK = 16


class EventFormatError(ValueError):
    """请求体或解出来的内容不合预期。异常文本不含请求里的内容。"""


@dataclass(frozen=True)
class UrlVerification:
    """URL 验证请求：在飞书后台填事件地址时飞书发来，回 ``challenge`` 就算验证通过。"""

    challenge: str
    token: str = field(repr=False)


@dataclass(frozen=True)
class EventEnvelope:
    """一个 2.0 版事件：``header`` 里核对来源用的几项，加上原样的 ``event``。

    Attributes:
        event_type: 事件类型，如 ``contact.user.deleted_v3``。
        token: Verification Token，必须等于租户配置的那个。
        app_id: 飞书应用的 App ID，必须等于租户配置的那个。
        tenant_key: 飞书企业标识，必须等于租户绑定的那个。
        event_id: 事件标识（同一事件重推时不变）；没给为 None。
        event: 事件体。
    """

    event_type: str
    token: str = field(repr=False)
    app_id: str
    tenant_key: str
    event_id: str | None = None
    event: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class UnsupportedPayload:
    """平台不处理的格式（比如 1.0 版事件）。离职事件只有 2.0 版。"""

    description: str


@dataclass(frozen=True)
class UserDeleted:
    """一条员工离职事件里平台要用的：按 ``union_id`` 找人，按状态定停用原因。"""

    union_id: str
    reason: DisabledReason


Payload = UrlVerification | EventEnvelope | UnsupportedPayload


def expected_signature(timestamp: str, nonce: str, encrypt_key: str, body: bytes) -> str:
    """飞书的签名：``sha256(timestamp + nonce + encrypt_key + body)`` 的十六进制。"""
    return hashlib.sha256((timestamp + nonce + encrypt_key).encode("utf-8") + body).hexdigest()


def signature_matches(
    signature: str, *, timestamp: str, nonce: str, encrypt_key: str, body: bytes
) -> bool:
    """请求头里的签名对不对。常数时间比较（按字节比，非 ASCII 的签名也不会报错）。"""
    expected = expected_signature(timestamp, nonce, encrypt_key, body)
    return hmac.compare_digest(signature.encode("utf-8"), expected.encode("ascii"))


def _json_object(raw: bytes, what: str) -> dict[str, Any]:
    """解析 JSON 对象；不是 UTF-8、不是 JSON、嵌套过深、不是对象都报 ``EventFormatError``。"""
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise EventFormatError(f"{what}不是合法的 JSON") from exc
    if not isinstance(value, dict):
        raise EventFormatError(f"{what}不是 JSON 对象")
    return value


def encrypted_field(body: bytes) -> str:
    """请求体里的 ``encrypt``。平台要求加密：没有它（明文事件）同样报错。"""
    outer = _json_object(body, "请求体")
    encrypted = outer.get("encrypt")
    if not isinstance(encrypted, str):
        raise EventFormatError("请求体里没有字符串的 encrypt 字段（平台要求事件加密）")
    return encrypted


def is_plain_url_verification(body: bytes) -> bool:
    """请求体是不是一个没加密的 URL 验证（飞书后台没配 Encrypt Key 时才这样发）。

    只用来写排障日志。
    """
    try:
        outer = _json_object(body, "请求体")
    except EventFormatError:
        return False
    return outer.get("type") == URL_VERIFICATION and "encrypt" not in outer


def decrypt(encrypt_key: str, encrypted: str) -> bytes:
    """解开 ``encrypt``：base64，IV 取前 16 字节，AES-256-CBC（密钥是 Encrypt Key 的 SHA-256），
    去 PKCS7 填充。

    Raises:
        EventFormatError: base64 不合法、长度不对（不足 IV 加一个块，或不是 16 的倍数）、填充不对
            （多半是 Encrypt Key 与飞书后台的不一致）。
    """
    try:
        raw = base64.b64decode(encrypted, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise EventFormatError("encrypt 不是合法的 base64") from exc
    if len(raw) < 2 * _BLOCK or len(raw) % _BLOCK:
        raise EventFormatError("密文长度不对：要有 16 字节的 IV 和至少一个完整的 16 字节块")
    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    decryptor = Cipher(algorithms.AES(key), modes.CBC(raw[:_BLOCK])).decryptor()
    padded = decryptor.update(raw[_BLOCK:]) + decryptor.finalize()
    unpadder = padding.PKCS7(_BLOCK * 8).unpadder()
    try:
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError as exc:
        raise EventFormatError("填充不对：Encrypt Key 与飞书后台的不一致，或密文被改过") from exc


def _required_str(body: dict[str, Any], key: str, what: str) -> str:
    """必须有的非空字符串字段。"""
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise EventFormatError(f"{what}缺少 {key}")
    return value


def parse_payload(plaintext: bytes) -> Payload:
    """认出解密后的内容：URL 验证、2.0 版事件，或平台不处理的格式。

    Raises:
        EventFormatError: 不是 JSON 对象；URL 验证缺 ``challenge``、``token``；2.0 版事件缺
            ``header`` 或其中核对来源要用的几项，``event`` 不是对象。
    """
    body = _json_object(plaintext, "解密后的内容")
    if body.get("type") == URL_VERIFICATION:
        return UrlVerification(
            challenge=_required_str(body, "challenge", "URL 验证"),
            token=_required_str(body, "token", "URL 验证"),
        )
    if body.get("schema") != "2.0":
        return UnsupportedPayload("不是 2.0 版事件")
    header = body.get("header")
    event = body.get("event", {})
    if not isinstance(header, dict) or not isinstance(event, dict):
        raise EventFormatError("2.0 版事件缺少 header，或 event 不是对象")
    event_id = header.get("event_id")
    return EventEnvelope(
        event_type=_required_str(header, "event_type", "事件的 header "),
        token=_required_str(header, "token", "事件的 header "),
        app_id=_required_str(header, "app_id", "事件的 header "),
        tenant_key=_required_str(header, "tenant_key", "事件的 header "),
        event_id=event_id if isinstance(event_id, str) and event_id else None,
        event=event,
    )


def reason_from_status(
    *,
    is_resigned: bool | None = None,
    is_exited: bool | None = None,
    is_frozen: bool | None = None,
) -> DisabledReason | None:
    """飞书员工状态对应的停用原因；不需要停用时为 None。

    几项同时为真时取最终的那个：离职（``feishu_resigned``）> 主动退出（``feishu_exited``）> 暂停
    （``feishu_frozen``）。``is_activated``、``is_unjoin`` 不影响结论。对账（P1 Task 7）也用它：三项
    都不为真就不停用。
    """
    if is_resigned:
        return "feishu_resigned"
    if is_exited:
        return "feishu_exited"
    if is_frozen:
        return "feishu_frozen"
    return None


def user_deleted(envelope: EventEnvelope) -> UserDeleted:
    """解析员工离职事件的 ``event.object``。

    事件本身就是「员工离职」（飞书通讯录里删除员工时推送），所以状态里三项都不为真（或没给状态）时
    按离职（``feishu_resigned``）处理。

    Raises:
        EventFormatError: 缺 ``event.object`` 或其中的 ``union_id``，``status`` 不是对象或其中的项
            不是布尔值。
    """
    obj = envelope.event.get("object")
    if not isinstance(obj, dict):
        raise EventFormatError("离职事件缺少 event.object")
    union_id = _required_str(obj, "union_id", "离职事件的 event.object ")
    status = obj.get("status", {})
    if not isinstance(status, dict):
        raise EventFormatError("离职事件的 status 不是对象")
    if any(value is not None and not isinstance(value, bool) for value in status.values()):
        raise EventFormatError("离职事件的 status 里有不是布尔值的项")
    reason = reason_from_status(
        is_resigned=status.get("is_resigned"),
        is_exited=status.get("is_exited"),
        is_frozen=status.get("is_frozen"),
    )
    return UserDeleted(union_id=union_id, reason=reason or "feishu_resigned")


__all__ = [
    "MAX_EVENT_BODY_BYTES",
    "NONCE_HEADER",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "URL_VERIFICATION",
    "USER_DELETED",
    "EventEnvelope",
    "EventFormatError",
    "Payload",
    "UnsupportedPayload",
    "UrlVerification",
    "UserDeleted",
    "decrypt",
    "encrypted_field",
    "expected_signature",
    "is_plain_url_verification",
    "parse_payload",
    "reason_from_status",
    "signature_matches",
    "user_deleted",
]
