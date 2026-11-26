"""Google OAuth 2.0 与 Google People API 客户端，以及联系人模型映射器。"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from server.domains.people.importer import (
    _TYPE_LABEL_MAP,
    _assemble_name,
    _finalize_profile,
    _infer_im_kind,
    _parse_gender,
)
from server.domains.people.models import BirthInfo

if TYPE_CHECKING:
    from server.domains.people.google_config import GoogleContactsConfig

GOOGLE_PERSON_FIELDS = (
    "names,nicknames,organizations,phoneNumbers,emailAddresses,"
    "birthdays,addresses,biographies,urls,genders,imClients,userDefined,metadata"
)
_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=10.0)


class GoogleContactsError(RuntimeError):
    """Google OAuth 或 People API 调用失败。"""


class GoogleAuthExpiredError(GoogleContactsError):
    """访问令牌或刷新令牌已失效，需用户重新授权关联。"""


class GoogleSyncTokenExpiredError(GoogleContactsError):
    """Google 增量 syncToken 已过期（HTTP 410），需回退全量同步。"""


@dataclass(slots=True)
class GoogleTokenBundle:
    """Google OAuth 令牌响应。"""

    access_token: str
    expires_in: int
    refresh_token: str | None = None
    scope: str = ""
    id_token: str = ""


@dataclass(slots=True)
class GoogleUserInfo:
    """已授权 Google 账号基础标识（用于展示当前关联邮箱与防错绑）。"""

    sub: str
    email: str
    display_name: str


@dataclass(slots=True)
class GoogleConnectionsPage:
    """People API people/me/connections 单页响应。"""

    connections: list[dict[str, Any]]
    next_page_token: str | None = None
    next_sync_token: str | None = None


@dataclass(slots=True)
class MappedGoogleContact:
    """Google People 单条联系人转换结果。"""

    resource_name: str
    etag: str
    deleted: bool
    profile: dict[str, Any] | None
    warnings: list[str]


class GoogleContactsClient(Protocol):
    """可替换的 Google OAuth + People API 客户端协议（方便测试与 mock）。"""

    async def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> GoogleTokenBundle:
        """用一次性授权码 + PKCE verifier 换取访问令牌与刷新令牌。"""
        ...

    async def refresh_access_token(self, *, refresh_token: str) -> GoogleTokenBundle:
        """用刷新令牌换取新的访问令牌。"""
        ...

    async def fetch_userinfo(
        self, *, access_token: str, id_token: str = ""
    ) -> GoogleUserInfo:
        """获取已授权 Google 账号的 sub、邮箱与显示名。"""
        ...

    async def list_connections(
        self,
        *,
        access_token: str,
        page_token: str | None = None,
        sync_token: str | None = None,
        page_size: int = 200,
    ) -> GoogleConnectionsPage:
        """分页拉取个人通讯录（支持 syncToken 增量同步）。"""
        ...

    async def revoke_token(self, *, token: str) -> None:
        """尽力撤销 Google 侧令牌（解除关联时调用，失败不阻断本地解绑）。"""
        ...

    async def close(self) -> None:
        """释放底层连接池。"""
        ...


def _decode_jwt_claims_unverified(id_token: str) -> dict[str, Any]:
    """仅解析 Google 令牌直出响应里 id_token 的载荷字段（该响应来自 HTTPS 直连令牌端点）。"""
    parts = id_token.split(".")
    if len(parts) != 3:
        return {}
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64.encode("ascii"))
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


class HttpxGoogleContactsClient:
    """基于 httpx 的标准 Google OAuth2 + People API 实现。"""

    def __init__(
        self,
        config: GoogleContactsConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """可注入自定义 httpx.AsyncClient（如 MockTransport）；否则按需创建并托管关闭。"""
        self._config = config
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT, follow_redirects=False
        )

    async def close(self) -> None:
        """仅关闭自己创建的客户端。"""
        if self._owns_client:
            await self._client.aclose()

    def _raise_oauth_error(self, resp: httpx.Response, stage: str) -> None:
        """将 Google OAuth 错误规整为明确的领域异常。"""
        err_code = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                err_code = str(body.get("error") or "")
        except ValueError:
            pass
        if resp.status_code in (400, 401) and err_code in ("invalid_grant", "unauthorized_client"):
            raise GoogleAuthExpiredError("Google 授权已失效或已被撤销，请重新关联 Google 账号")
        raise GoogleContactsError(f"Google {stage}失败（HTTP {resp.status_code}）")

    async def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> GoogleTokenBundle:
        """调用 Google OAuth2 /token 端点换取令牌。"""
        try:
            resp = await self._client.post(
                f"{self._config.token_base}/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                    "code": code,
                    "code_verifier": code_verifier,
                    "redirect_uri": redirect_uri,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise GoogleContactsError("连接 Google 授权服务失败，请稍后重试") from exc
        if resp.status_code >= 400:
            self._raise_oauth_error(resp, "令牌交换")
        body = resp.json()
        access_token = str(body.get("access_token") or "").strip()
        if not access_token:
            raise GoogleContactsError("Google 未返回有效的访问令牌")
        return GoogleTokenBundle(
            access_token=access_token,
            expires_in=int(body.get("expires_in") or 3600),
            refresh_token=str(body["refresh_token"]).strip() if body.get("refresh_token") else None,
            scope=str(body.get("scope") or ""),
            id_token=str(body.get("id_token") or ""),
        )

    async def refresh_access_token(self, *, refresh_token: str) -> GoogleTokenBundle:
        """使用 refresh_token 刷新 access_token。"""
        try:
            resp = await self._client.post(
                f"{self._config.token_base}/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                    "refresh_token": refresh_token,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise GoogleContactsError("刷新 Google 授权令牌失败，请稍后重试") from exc
        if resp.status_code >= 400:
            self._raise_oauth_error(resp, "令牌刷新")
        body = resp.json()
        access_token = str(body.get("access_token") or "").strip()
        if not access_token:
            raise GoogleAuthExpiredError("Google 未返回新的访问令牌，请重新关联")
        return GoogleTokenBundle(
            access_token=access_token,
            expires_in=int(body.get("expires_in") or 3600),
            refresh_token=refresh_token,
            scope=str(body.get("scope") or ""),
        )

    async def fetch_userinfo(
        self, *, access_token: str, id_token: str = ""
    ) -> GoogleUserInfo:
        """优先从 id_token 读取账号标识，缺省时通过 People API /v1/people/me 补齐。"""
        claims = _decode_jwt_claims_unverified(id_token) if id_token else {}
        sub = str(claims.get("sub") or "").strip()
        email = str(claims.get("email") or "").strip()
        name = str(claims.get("name") or "").strip()
        if sub and email:
            return GoogleUserInfo(sub=sub, email=email, display_name=name or email)

        try:
            resp = await self._client.get(
                f"{self._config.people_api_base}/v1/people/me",
                params={"personFields": "names,emailAddresses"},
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            if sub:
                return GoogleUserInfo(sub=sub, email=email, display_name=name or sub)
            raise GoogleContactsError("读取 Google 账号资料失败") from exc
        if resp.status_code == 401:
            raise GoogleAuthExpiredError("Google 访问令牌无效，请重新关联")
        if resp.status_code >= 400:
            if sub:
                return GoogleUserInfo(sub=sub, email=email, display_name=name or sub)
            raise GoogleContactsError(f"读取 Google 账号资料失败（HTTP {resp.status_code}）")
        body = resp.json()
        if not sub:
            rn = str(body.get("resourceName") or "").strip()
            sub = rn.removeprefix("people/") if rn else ""
        if not email:
            emails = body.get("emailAddresses") or []
            if emails and isinstance(emails[0], dict):
                email = str(emails[0].get("value") or "").strip()
        if not name:
            names = body.get("names") or []
            if names and isinstance(names[0], dict):
                name = str(names[0].get("displayName") or "").strip()
        if not sub:
            raise GoogleContactsError("无法识别 Google 账号标识")
        return GoogleUserInfo(sub=sub, email=email, display_name=name or email or sub)

    async def list_connections(
        self,
        *,
        access_token: str,
        page_token: str | None = None,
        sync_token: str | None = None,
        page_size: int = 200,
    ) -> GoogleConnectionsPage:
        """分页读取 Google Contacts（支持 syncToken 增量同步与 410 过期检测）。"""
        params: dict[str, Any] = {
            "personFields": GOOGLE_PERSON_FIELDS,
            "pageSize": min(max(page_size, 1), 1000),
            "requestSyncToken": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        if sync_token:
            params["syncToken"] = sync_token
        try:
            resp = await self._client.get(
                f"{self._config.people_api_base}/v1/people/me/connections",
                params=params,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise GoogleContactsError("连接 Google 通讯录接口失败，请稍后重试") from exc
        if resp.status_code == 401:
            raise GoogleAuthExpiredError("Google 授权已过期，请重新关联 Google 账号")
        if resp.status_code == 410:
            raise GoogleSyncTokenExpiredError("Google 同步游标已过期，将自动切换全量同步")
        if resp.status_code >= 400:
            raise GoogleContactsError(f"拉取 Google 通讯录失败（HTTP {resp.status_code}）")
        body = resp.json()
        raw_conns = body.get("connections") or []
        return GoogleConnectionsPage(
            connections=[c for c in raw_conns if isinstance(c, dict)],
            next_page_token=body.get("nextPageToken") or None,
            next_sync_token=body.get("nextSyncToken") or None,
        )

    async def revoke_token(self, *, token: str) -> None:
        """尽力通知 Google 撤销令牌；网络错误不阻断本地解绑。"""
        try:
            await self._client.post(
                f"{self._config.token_base}/revoke",
                data={"token": token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError:
            return


# ---------- Google People API -> PersonProfile 映射 ----------


def _is_primary(item: dict[str, Any]) -> bool:
    """检查 Google 字段 metadata.primary。"""
    meta = item.get("metadata")
    return isinstance(meta, dict) and bool(meta.get("primary"))


def _pick_first_primary(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """优先取标记为 primary 的元素，否则取首元素。"""
    for item in items:
        if _is_primary(item):
            return item
    return items[0] if items else None


def google_person_to_profile(raw: dict[str, Any]) -> MappedGoogleContact:
    """将 Google People API 返回的 Person 对象映射为标准 PersonProfile。"""
    resource_name = str(raw.get("resourceName") or "").strip()
    etag = str(raw.get("etag") or "").strip()
    meta = raw.get("metadata")
    if isinstance(meta, dict) and bool(meta.get("deleted")):
        return MappedGoogleContact(
            resource_name=resource_name,
            etag=etag,
            deleted=True,
            profile=None,
            warnings=[],
        )

    warnings: list[str] = []
    name = ""
    aliases: list[str] = []
    names = [n for n in (raw.get("names") or []) if isinstance(n, dict)]
    primary_name = _pick_first_primary(names)
    if primary_name:
        display = str(primary_name.get("displayName") or "").strip()
        assembled = _assemble_name(
            str(primary_name.get("familyName") or ""),
            str(primary_name.get("givenName") or ""),
            str(primary_name.get("middleName") or ""),
        )
        name = display or assembled
    for nick in raw.get("nicknames") or []:
        if isinstance(nick, dict) and nick.get("value"):
            aliases.append(str(nick["value"]).strip())

    org = ""
    dept = ""
    title = ""
    orgs = [o for o in (raw.get("organizations") or []) if isinstance(o, dict)]
    primary_org = _pick_first_primary(orgs)
    if primary_org:
        org = str(primary_org.get("name") or "").strip()
        dept = str(primary_org.get("department") or "").strip()
        title = str(primary_org.get("title") or "").strip()

    contacts: list[dict[str, Any]] = []
    for ph in raw.get("phoneNumbers") or []:
        if not isinstance(ph, dict) or not ph.get("value"):
            continue
        raw_lbl = str(ph.get("formattedType") or ph.get("type") or "手机").strip()
        lbl = _TYPE_LABEL_MAP.get(raw_lbl.upper(), raw_lbl)
        contacts.append({
            "kind": "phone",
            "value": str(ph["value"]).strip(),
            "label": lbl,
            "primary": _is_primary(ph),
        })
    for em in raw.get("emailAddresses") or []:
        if not isinstance(em, dict) or not em.get("value"):
            continue
        raw_lbl = str(em.get("formattedType") or em.get("type") or "邮箱").strip()
        lbl = _TYPE_LABEL_MAP.get(raw_lbl.upper(), raw_lbl)
        contacts.append({
            "kind": "email",
            "value": str(em["value"]).strip(),
            "label": lbl,
            "primary": _is_primary(em),
        })
    for im in raw.get("imClients") or []:
        if not isinstance(im, dict) or not im.get("username"):
            continue
        proto = str(im.get("formattedProtocol") or im.get("protocol") or "").strip()
        kind, val = _infer_im_kind(proto, str(im["username"]))
        default_lbl = {"wechat": "微信", "qq": "QQ", "feishu": "飞书", "other": "社交"}[kind]
        contacts.append({
            "kind": kind,
            "value": val,
            "label": proto or default_lbl,
            "primary": False,
        })

    birth: dict[str, Any] | None = None
    for bday in raw.get("birthdays") or []:
        if not isinstance(bday, dict):
            continue
        dt = bday.get("date")
        if isinstance(dt, dict):
            year = dt.get("year")
            month = dt.get("month")
            day = dt.get("day")
            if isinstance(year, int) and not (1900 <= year <= 2100):
                year = None
            try:
                birth = BirthInfo(
                    year=year if isinstance(year, int) else None,
                    month=month if isinstance(month, int) else None,
                    day=day if isinstance(day, int) else None,
                    calendar="solar",
                ).model_dump()
                break
            except Exception:
                warnings.append("Google 生日字段无效，已忽略")

    gender = "unknown"
    genders = [g for g in (raw.get("genders") or []) if isinstance(g, dict)]
    primary_gender = _pick_first_primary(genders)
    if primary_gender and primary_gender.get("value"):
        gender = _parse_gender(str(primary_gender["value"]))

    city = ""
    extra: dict[str, str] = {}
    addrs = [a for a in (raw.get("addresses") or []) if isinstance(a, dict)]
    primary_addr = _pick_first_primary(addrs)
    if primary_addr:
        city = str(primary_addr.get("city") or primary_addr.get("region") or "").strip()
        formatted = str(primary_addr.get("formattedValue") or "").strip()
        if not formatted:
            parts = [
                str(primary_addr.get(k) or "").strip()
                for k in ("country", "region", "city", "streetAddress")
            ]
            formatted = " ".join(p for p in parts if p)
        if formatted:
            extra["address"] = formatted

    urls = [u for u in (raw.get("urls") or []) if isinstance(u, dict) and u.get("value")]
    if urls:
        extra["website"] = str(urls[0]["value"]).strip()

    for kv in raw.get("userDefined") or []:
        if isinstance(kv, dict) and kv.get("key") and kv.get("value"):
            extra[str(kv["key"]).strip()] = str(kv["value"]).strip()

    bios = [
        str(b.get("value") or "").strip()
        for b in (raw.get("biographies") or [])
        if isinstance(b, dict) and b.get("value")
    ]

    parsed = _finalize_profile(
        {
            "name": name,
            "aliases": aliases,
            "gender": gender,
            "birth": birth,
            "organization": org,
            "department": dept,
            "title": title,
            "city": city,
            "contacts": contacts,
            "tags": [],
            "summary": "\n".join(bios),
            "extra": extra,
        },
        warnings,
    )
    return MappedGoogleContact(
        resource_name=resource_name,
        etag=etag,
        deleted=False,
        profile=parsed.profile if parsed is not None else None,
        warnings=parsed.warnings if parsed is not None else warnings,
    )
