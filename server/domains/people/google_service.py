"""纯个人空间 Google 账号关联与 Google 通讯录只读同步服务。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, ValidationError

from server.core.errors import (
    AuthenticationError,
    ConflictError,
    InvalidRequestError,
    PermissionDeniedError,
    RuntimeUnavailableError,
)
from server.domains.connections.credential_vault import cipher
from server.domains.identity.cookies import login_token_hash
from server.domains.identity.urls import safe_return_to
from server.domains.people.google_client import (
    GoogleAuthExpiredError,
    GoogleContactsClient,
    GoogleContactsError,
    GoogleSyncTokenExpiredError,
    HttpxGoogleContactsClient,
    google_person_to_profile,
)
from server.domains.people.google_store import (
    GoogleAccountRecord,
    GoogleAttempt,
    GoogleContactLink,
    GoogleContactsStore,
)
from server.domains.people.importer import (
    MAX_IMPORT_ITEMS,
    _active_people_count,
    find_import_candidates,
)
from server.domains.people.models import MAX_PEOPLE, Identifier, PersonProfile, StrictModel

if TYPE_CHECKING:
    from collections.abc import Callable

    from server.core.secrets import SecretVault
    from server.core.tenancy import Principal
    from server.domains.identity.service import IdentityService
    from server.domains.people.google_config import GoogleContactsConfig
    from server.domains.people.service import PeopleService
    from server.domains.workspace.service import WorkspaceService

_STATE_RE = re.compile(r"^[a-f0-9]{64}$")
_ATTEMPT_TTL_SECONDS = 300.0
_TOKEN_REFRESH_BUFFER_SECONDS = 60.0
_MAX_PAGES = 20


class GoogleSyncItemInput(StrictModel):
    """前端确认同步时的单条 Google 联系人指令。"""

    resource_name: str = Field(default="", max_length=256)
    etag: str = Field(default="", max_length=256)
    action: Literal["create", "merge", "skip"] = "create"
    profile: PersonProfile
    target_person_id: Identifier | None = None
    target_version: int | None = Field(default=None, ge=1)


class GoogleSyncCommitInput(StrictModel):
    """确认同步请求体：支持基于预览逐条提交或一键自动增量同步。"""

    request_id: str = Field(min_length=1, max_length=128)
    mode: Literal["preview", "auto"] = "preview"
    full_sync: bool = False
    items: list[GoogleSyncItemInput] = Field(default_factory=list, max_length=MAX_IMPORT_ITEMS)
    next_sync_token: str | None = Field(default=None, max_length=2048)
    deleted_resource_names: list[str] = Field(default_factory=list, max_length=MAX_IMPORT_ITEMS)


def _pkce_challenge(verifier: str) -> str:
    """按 RFC 7636 计算 S256 PKCE code_challenge。"""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _token_scope(user_id: str, kind: str, revision: str) -> str:
    """生成绑定员工、令牌种类与修订号的 AAD 作用域。"""
    return "\0".join(("google_contacts", user_id, kind, revision))


def _append_query_param(url: str, key: str, value: str) -> str:
    """向站内 return_to 路径安全追加单个查询参数。"""
    parts = urlsplit(url)
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != key]
    params.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


class GoogleContactsService:
    """个人 Google 账号关联与 Google Contacts 只读同步服务。"""

    def __init__(
        self,
        *,
        config: GoogleContactsConfig,
        store: GoogleContactsStore,
        people: PeopleService,
        workspace: WorkspaceService,
        identity: IdentityService,
        vault: SecretVault,
        client: GoogleContactsClient | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        """装配配置、双后端仓储、加密保管库与可替换 HTTP 客户端。"""
        self._config = config
        self._store = store
        self._people = people
        self._workspace = workspace
        self._identity = identity
        self._vault = vault
        self._client: GoogleContactsClient = client or HttpxGoogleContactsClient(config)
        self._now = now

    async def close(self) -> None:
        """关闭外部 HTTP 客户端。"""
        await self._client.close()

    @property
    def _public_url(self) -> str | None:
        return self._identity.settings.public_url

    @property
    def callback_url(self) -> str | None:
        """面向 Google Cloud Console 配置的固定回调地址。"""
        if not self._public_url:
            return None
        return f"{self._public_url}/v1/people/google/callback"

    def _require_configured(self) -> str:
        """校验 Google 关联已启用且 OAuth 凭据与平台 public_url 齐全。"""
        if not self._config.enabled:
            raise ConflictError(
                "Google 通讯录关联未启用，请先在服务配置中开启 google_contacts.enabled"
            )
        if not self._config.client_id.strip() or not self._config.client_secret.strip():
            raise ConflictError("尚未配置 Google OAuth client_id / client_secret，暂无法发起关联")
        if not self._public_url:
            raise ConflictError("请先配置平台 public_url 后再关联 Google 账号")
        return f"{self._public_url}/v1/people/google/callback"

    def _seal(self, tenant: str, user: str, kind: str, revision: str, plaintext: str) -> bytes:
        return cipher(self._vault).seal_scoped(
            tenant, _token_scope(user, kind, revision), plaintext
        )

    def _open(self, tenant: str, user: str, kind: str, revision: str, blob: bytes) -> str:
        return cipher(self._vault).open_scoped(tenant, _token_scope(user, kind, revision), blob)

    async def status(self, who: Principal) -> dict[str, Any]:
        """读取当前员工的 Google 关联状态；未配置时安全返回 configured=false，不发外网请求。"""
        tenant, user = await self._workspace.require_employee(who)
        configured = self._config.is_configured(self._public_url)
        account = await self._store.get_account(tenant, user)
        if account is None:
            return {
                "enabled": self._config.enabled,
                "configured": configured,
                "linked": False,
                "callback_url": self.callback_url,
                "account_email": "",
                "account_name": "",
                "linked_at": None,
                "last_synced_at": None,
                "synced_count": 0,
                "has_sync_token": False,
            }
        links = await self._store.list_links(tenant, user)
        return {
            "enabled": self._config.enabled,
            "configured": configured,
            "linked": True,
            "callback_url": self.callback_url,
            "account_email": account.email,
            "account_name": account.display_name,
            "linked_at": account.linked_at,
            "last_synced_at": account.last_synced_at,
            "synced_count": len(links),
            "has_sync_token": bool(account.sync_token),
        }

    async def start_connect(
        self, who: Principal, *, return_to: str = "/console/entry.html"
    ) -> dict[str, Any]:
        """为当前浏览器登录员工生成 Google OAuth2 + PKCE 授权跳转地址与一次性绑定 Cookie。"""
        if who.key_id is not None:
            raise PermissionDeniedError("仅支持员工本人在浏览器中关联 Google 账号")
        tenant, user = await self._workspace.require_employee(who)
        redirect_uri = self._require_configured()
        # 提前确认保管库具备作用域加密能力，避免回调时才发现无法密文落库
        cipher(self._vault)

        now = self._now()
        state = secrets.token_hex(32)
        cookie = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(48)
        safe_target = safe_return_to(return_to, fallback="/console/entry.html")

        await self._store.put_attempt(
            GoogleAttempt(
                state_hash=login_token_hash(state),
                tenant_id=tenant,
                user_id=user,
                cookie_hash=login_token_hash(cookie),
                code_verifier=code_verifier,
                return_to=safe_target,
                expires_at=now + _ATTEMPT_TTL_SECONDS,
            ),
            now=now,
        )
        query = urlencode({
            "client_id": self._config.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self._config.scopes),
            "state": state,
            "code_challenge": _pkce_challenge(code_verifier),
            "code_challenge_method": "S256",
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
        })
        return {
            "authorization_url": f"{self._config.auth_base}/o/oauth2/v2/auth?{query}",
            "cookie": cookie,
        }

    async def complete_callback(
        self,
        *,
        state: str,
        code: str,
        cookie: str | None,
        current_login: str | None,
        error: str = "",
    ) -> dict[str, str]:
        """校验一次性 state、同浏览器 Cookie 与当前员工会话，换取并密文保存 Google 令牌。"""
        if not _STATE_RE.fullmatch(state):
            raise AuthenticationError("Google 授权状态无效")
        now = self._now()
        attempt = await self._store.take_attempt(login_token_hash(state), now=now)
        if (
            attempt is None
            or cookie is None
            or not hmac.compare_digest(login_token_hash(cookie), attempt.cookie_hash)
        ):
            raise AuthenticationError("Google 授权状态无效或已过期，请重新发起关联")

        session = (
            None
            if current_login is None
            else await self._identity.store.get_login_session(
                login_token_hash(current_login), now=now
            )
        )
        if session is None or (session.tenant_id, session.user_id) != (
            attempt.tenant_id,
            attempt.user_id,
        ):
            raise AuthenticationError("授权期间登录身份已变化，请重新登录并关联 Google 账号")

        if error:
            return {
                "return_to": _append_query_param(attempt.return_to, "google_contacts", "cancelled")
            }
        if not code.strip():
            raise AuthenticationError("Google 未返回授权码，请重试")

        redirect_uri = self._require_configured()
        try:
            bundle = await self._client.exchange_code(
                code=code.strip(),
                code_verifier=attempt.code_verifier,
                redirect_uri=redirect_uri,
            )
            userinfo = await self._client.fetch_userinfo(
                access_token=bundle.access_token, id_token=bundle.id_token
            )
        except GoogleContactsError as exc:
            raise RuntimeUnavailableError(str(exc)) from exc

        existing = await self._store.get_account(attempt.tenant_id, attempt.user_id)
        same_google_account = existing is not None and existing.google_sub == userinfo.sub
        revision = secrets.token_hex(8)
        access_cipher = self._seal(
            attempt.tenant_id, attempt.user_id, "access", revision, bundle.access_token
        )
        refresh_plain = bundle.refresh_token
        if (
            refresh_plain is None
            and same_google_account
            and existing is not None
            and existing.refresh_token_cipher is not None
        ):
            refresh_plain = self._open(
                attempt.tenant_id,
                attempt.user_id,
                "refresh",
                existing.token_revision,
                existing.refresh_token_cipher,
            )
        refresh_cipher = (
            self._seal(attempt.tenant_id, attempt.user_id, "refresh", revision, refresh_plain)
            if refresh_plain
            else None
        )

        # 若更换了另一个 Google 账号，清空旧账号的增量游标与外部联系人映射
        if existing is not None and not same_google_account:
            await self._store.delete_account(attempt.tenant_id, attempt.user_id)

        record = GoogleAccountRecord(
            tenant_id=attempt.tenant_id,
            user_id=attempt.user_id,
            google_sub=userinfo.sub,
            email=userinfo.email,
            display_name=userinfo.display_name,
            scopes=bundle.scope or " ".join(self._config.scopes),
            token_revision=revision,
            access_token_cipher=access_cipher,
            refresh_token_cipher=refresh_cipher,
            access_expires_at=now + max(bundle.expires_in, 60),
            sync_token=existing.sync_token if same_google_account and existing else None,
            linked_at=existing.linked_at if same_google_account and existing else now,
            last_synced_at=existing.last_synced_at if same_google_account and existing else None,
            updated_at=now,
        )
        await self._store.upsert_account(record)
        await self._store.append_audit(
            attempt.tenant_id,
            attempt.user_id,
            "people.google.linked",
            {"provider": "google", "has_refresh_token": refresh_cipher is not None},
            now,
        )
        return {"return_to": _append_query_param(attempt.return_to, "google_contacts", "linked")}

    async def _require_linked_account(self, tenant: str, user: str) -> GoogleAccountRecord:
        account = await self._store.get_account(tenant, user)
        if account is None:
            raise ConflictError("当前尚未关联 Google 账号，请先完成关联")
        return account

    async def _valid_access_token(
        self, tenant: str, user: str, account: GoogleAccountRecord, *, force_refresh: bool = False
    ) -> tuple[str, GoogleAccountRecord]:
        """解密访问令牌；临期或失效时自动使用 refresh_token 刷新并更新密文。"""
        now = self._now()
        if not force_refresh and account.access_expires_at > now + _TOKEN_REFRESH_BUFFER_SECONDS:
            token = self._open(
                tenant, user, "access", account.token_revision, account.access_token_cipher
            )
            return token, account
        if account.refresh_token_cipher is None:
            raise AuthenticationError("Google 授权已过期且缺少刷新令牌，请重新关联 Google 账号")
        refresh_token = self._open(
            tenant, user, "refresh", account.token_revision, account.refresh_token_cipher
        )
        try:
            refreshed = await self._client.refresh_access_token(refresh_token=refresh_token)
        except GoogleAuthExpiredError as exc:
            raise AuthenticationError(str(exc)) from exc
        except GoogleContactsError as exc:
            raise RuntimeUnavailableError(str(exc)) from exc

        new_rev = secrets.token_hex(8)
        updated = GoogleAccountRecord(
            tenant_id=account.tenant_id,
            user_id=account.user_id,
            google_sub=account.google_sub,
            email=account.email,
            display_name=account.display_name,
            scopes=refreshed.scope or account.scopes,
            token_revision=new_rev,
            access_token_cipher=self._seal(tenant, user, "access", new_rev, refreshed.access_token),
            refresh_token_cipher=self._seal(tenant, user, "refresh", new_rev, refresh_token),
            access_expires_at=now + max(refreshed.expires_in, 60),
            sync_token=account.sync_token,
            linked_at=account.linked_at,
            last_synced_at=account.last_synced_at,
            updated_at=now,
        )
        await self._store.upsert_account(updated)
        return refreshed.access_token, updated

    async def _fetch_google_connections(
        self,
        tenant: str,
        user: str,
        account: GoogleAccountRecord,
        *,
        use_sync_token: bool,
    ) -> tuple[list[dict[str, Any]], str | None, bool, list[str]]:
        """分页拉取 Google People connections，处理 401 刷新与 410 syncToken 自动回退。"""
        access_token, account = await self._valid_access_token(tenant, user, account)
        sync_token = account.sync_token if use_sync_token else None
        warnings: list[str] = []

        async def _pull(token: str, cursor: str | None) -> tuple[list[dict[str, Any]], str | None]:
            collected: list[dict[str, Any]] = []
            page_token: str | None = None
            next_sync: str | None = None
            for _ in range(_MAX_PAGES):
                page = await self._client.list_connections(
                    access_token=token,
                    page_token=page_token,
                    sync_token=cursor if page_token is None else None,
                    page_size=200,
                )
                collected.extend(page.connections)
                if page.next_sync_token:
                    next_sync = page.next_sync_token
                if not page.next_page_token or len(collected) >= MAX_IMPORT_ITEMS:
                    if page.next_page_token and len(collected) >= MAX_IMPORT_ITEMS:
                        warnings.append(f"单次最多同步 {MAX_IMPORT_ITEMS} 位联系人，超出部分已截断")
                    break
                page_token = page.next_page_token
            return collected[:MAX_IMPORT_ITEMS], next_sync

        try:
            raw_list, next_sync = await _pull(access_token, sync_token)
            return raw_list, next_sync, bool(sync_token), warnings
        except GoogleAuthExpiredError:
            access_token, account = await self._valid_access_token(
                tenant, user, account, force_refresh=True
            )
            try:
                raw_list, next_sync = await _pull(access_token, sync_token)
                return raw_list, next_sync, bool(sync_token), warnings
            except GoogleAuthExpiredError as exc:
                raise AuthenticationError(str(exc)) from exc
            except GoogleSyncTokenExpiredError:
                warnings.append("Google 增量游标已过期，已自动切换为全量同步")
                raw_list, next_sync = await _pull(access_token, None)
                return raw_list, next_sync, False, warnings
            except GoogleContactsError as exc:
                raise RuntimeUnavailableError(str(exc)) from exc
        except GoogleSyncTokenExpiredError:
            warnings.append("Google 增量游标已过期，已自动切换为全量同步")
            try:
                raw_list, next_sync = await _pull(access_token, None)
                return raw_list, next_sync, False, warnings
            except GoogleContactsError as exc:
                raise RuntimeUnavailableError(str(exc)) from exc
        except GoogleContactsError as exc:
            raise RuntimeUnavailableError(str(exc)) from exc

    async def preview_sync(self, who: Principal, *, full_sync: bool = False) -> dict[str, Any]:
        """从已关联 Google 账号只读拉取通讯录并与本地档案匹配查重，生成待确认预览。"""
        tenant, user = await self._workspace.require_employee(who)
        self._require_configured()
        account = await self._require_linked_account(tenant, user)

        raw_conns, next_sync_token, is_incremental, warnings = await self._fetch_google_connections(
            tenant, user, account, use_sync_token=not full_sync
        )
        state = await self._people._store.snapshot(tenant, user)
        links = await self._store.list_links(tenant, user)
        remaining_quota = max(0, MAX_PEOPLE - _active_people_count(state))

        preview_items: list[dict[str, Any]] = []
        deleted_rns: list[str] = []
        skipped_count = 0
        new_count = 0
        linked_count = 0
        duplicate_count = 0

        for raw in raw_conns:
            mapped = google_person_to_profile(raw)
            if mapped.deleted:
                if mapped.resource_name:
                    deleted_rns.append(mapped.resource_name)
                continue
            if mapped.profile is None:
                skipped_count += 1
                continue

            warnings.extend(mapped.warnings)
            existing_link = links.get(mapped.resource_name) if mapped.resource_name else None
            candidates: list[dict[str, Any]] = []
            linked_person_hit = False
            if existing_link is not None:
                rec = state.persons.get(existing_link.person_id)
                if (
                    rec is not None
                    and rec["trashed_at"] is None
                    and rec.get("merged_into_id") is None
                ):
                    rp = rec["profile"]
                    candidates.append({
                        "person_id": rec["id"],
                        "name": rp["name"],
                        "organization": rp.get("organization", ""),
                        "title": rp.get("title", ""),
                        "version": rec["version"],
                        "linked": True,
                    })
                    linked_person_hit = True

            for cand in find_import_candidates(state, mapped.profile, limit=5):
                if not any(c["person_id"] == cand["person_id"] for c in candidates):
                    candidates.append(cand)

            if linked_person_hit:
                status = "linked"
                suggested_action = "merge"
                linked_count += 1
            elif candidates:
                status = "duplicate"
                suggested_action = "merge"
                duplicate_count += 1
            else:
                status = "new"
                suggested_action = "create"
                new_count += 1

            preview_items.append({
                "index": len(preview_items),
                "resource_name": mapped.resource_name,
                "etag": mapped.etag,
                "profile": mapped.profile,
                "status": status,
                "candidates": candidates,
                "suggested_action": suggested_action,
                "warnings": mapped.warnings,
            })

        return {
            "provider": "google",
            "account_email": account.email,
            "is_incremental": is_incremental,
            "total_fetched": len(raw_conns),
            "valid_count": len(preview_items),
            "skipped_count": skipped_count,
            "new_count": new_count,
            "linked_count": linked_count,
            "duplicate_count": duplicate_count,
            "deleted_count": len(deleted_rns),
            "deleted_resource_names": deleted_rns,
            "next_sync_token": next_sync_token,
            "remaining_quota": remaining_quota,
            "warnings": warnings[:50],
            "items": preview_items,
        }

    async def commit_sync(self, who: Principal, raw: dict[str, Any]) -> dict[str, Any]:
        """提交 Google 通讯录同步（支持确认预览批次或一键自动增量同步），更新映射与 syncToken。"""
        try:
            req = GoogleSyncCommitInput.model_validate(raw)
        except ValidationError as exc:
            raise InvalidRequestError(f"同步请求无效：{exc.errors()[0]['msg']}") from exc

        tenant, user = await self._workspace.require_employee(who)
        self._require_configured()
        account = await self._require_linked_account(tenant, user)

        sync_items: list[dict[str, Any]] = []
        next_sync_token = req.next_sync_token
        deleted_rns = list(req.deleted_resource_names)

        if req.mode == "auto":
            pv = await self.preview_sync(who, full_sync=req.full_sync)
            next_sync_token = pv["next_sync_token"]
            deleted_rns = list(pv["deleted_resource_names"])
            for row in pv["items"]:
                first_cand = (row["candidates"] or [None])[0]
                if row["suggested_action"] == "merge" and first_cand is not None:
                    sync_items.append({
                        "resource_name": row["resource_name"],
                        "etag": row["etag"],
                        "action": "merge",
                        "profile": row["profile"],
                        "target_person_id": first_cand["person_id"],
                        "target_version": first_cand["version"],
                    })
                else:
                    sync_items.append({
                        "resource_name": row["resource_name"],
                        "etag": row["etag"],
                        "action": "create",
                        "profile": row["profile"],
                    })
        else:
            if not req.items and not deleted_rns and not next_sync_token:
                raise InvalidRequestError("同步条目不能为空")
            for item in req.items:
                entry: dict[str, Any] = {
                    "resource_name": item.resource_name,
                    "etag": item.etag,
                    "action": item.action,
                    "profile": item.profile.model_dump(),
                }
                if item.action == "merge":
                    entry["target_person_id"] = item.target_person_id
                    entry["target_version"] = item.target_version
                sync_items.append(entry)

        now = self._now()
        created: list[dict[str, Any]] = []
        merged: list[dict[str, Any]] = []
        skipped_count = 0
        new_links: list[GoogleContactLink] = []

        if sync_items:
            commit_payload: list[dict[str, Any]] = []
            for entry in sync_items:
                p: dict[str, Any] = {"action": entry["action"], "profile": entry["profile"]}
                if entry["action"] == "merge":
                    p["target_person_id"] = entry.get("target_person_id")
                    p["target_version"] = entry.get("target_version")
                commit_payload.append(p)
            result = await self._people.commit_import(who, commit_payload, req.request_id)
            created = result["created"]
            merged = result["merged"]
            skipped_count = result["skipped_count"]

            create_cursor = 0
            for entry in sync_items:
                rn = str(entry.get("resource_name") or "").strip()
                etag = str(entry.get("etag") or "").strip()
                if entry["action"] == "create":
                    if create_cursor < len(created):
                        pid = created[create_cursor]["id"]
                        create_cursor += 1
                        if rn:
                            new_links.append(
                                GoogleContactLink(
                                    resource_name=rn, person_id=pid, etag=etag, synced_at=now
                                )
                            )
                elif entry["action"] == "merge" and rn and entry.get("target_person_id"):
                    new_links.append(
                        GoogleContactLink(
                            resource_name=rn,
                            person_id=str(entry["target_person_id"]),
                            etag=etag,
                            synced_at=now,
                        )
                    )

        if new_links:
            await self._store.upsert_links(tenant, user, new_links)
        if deleted_rns:
            await self._store.delete_links(tenant, user, deleted_rns)

        # 刷新 sync_token 与 last_synced_at（重新读取以防期间刚刷新过 token）
        latest_acc = await self._store.get_account(tenant, user) or account
        updated_acc = GoogleAccountRecord(
            tenant_id=latest_acc.tenant_id,
            user_id=latest_acc.user_id,
            google_sub=latest_acc.google_sub,
            email=latest_acc.email,
            display_name=latest_acc.display_name,
            scopes=latest_acc.scopes,
            token_revision=latest_acc.token_revision,
            access_token_cipher=latest_acc.access_token_cipher,
            refresh_token_cipher=latest_acc.refresh_token_cipher,
            access_expires_at=latest_acc.access_expires_at,
            sync_token=next_sync_token if next_sync_token is not None else latest_acc.sync_token,
            linked_at=latest_acc.linked_at,
            last_synced_at=now,
            updated_at=now,
        )
        await self._store.upsert_account(updated_acc)
        await self._store.append_audit(
            tenant,
            user,
            "people.google.synced",
            {
                "provider": "google",
                "mode": req.mode,
                "created_count": len(created),
                "merged_count": len(merged),
                "skipped_count": skipped_count,
                "deleted_links_count": len(deleted_rns),
            },
            now,
        )
        return {
            "created_count": len(created),
            "merged_count": len(merged),
            "skipped_count": skipped_count,
            "deleted_links_count": len(deleted_rns),
            "synced_at": now,
            "has_sync_token": bool(updated_acc.sync_token),
            "created": created,
            "merged": merged,
        }

    async def unlink(self, who: Principal) -> dict[str, Any]:
        """解除当前员工的 Google 账号关联并清理密文令牌与外部映射（保留已导入本地联系人）。"""
        tenant, user = await self._workspace.require_employee(who)
        account = await self._store.get_account(tenant, user)
        if account is not None:
            token_to_revoke: str | None = None
            try:
                if account.refresh_token_cipher is not None:
                    token_to_revoke = self._open(
                        tenant,
                        user,
                        "refresh",
                        account.token_revision,
                        account.refresh_token_cipher,
                    )
                else:
                    token_to_revoke = self._open(
                        tenant, user, "access", account.token_revision, account.access_token_cipher
                    )
            except Exception:
                token_to_revoke = None
            if token_to_revoke:
                await self._client.revoke_token(token=token_to_revoke)

        removed = await self._store.delete_account(tenant, user)
        if removed:
            await self._store.append_audit(
                tenant, user, "people.google.unlinked", {"provider": "google"}, self._now()
            )
        return {"unlinked": removed}
