"""桌面客户端浏览器接力登录的页面与路由（ADR 0021；流程见 ``identity.desktop``）。

- 等待页：``GET /v1/auth/feishu/login?client=desktop`` 成功时由 ``api.identity`` 调
  ``wait_page_response`` 返回，在桌面客户端的 WebView 里显示确认码、轮询进度；
- 确认页：飞书回调建了接力记录时由 ``api.identity`` 调 ``confirm_page_response`` 返回，在系统
  浏览器里显示确认码与「确认登录」按钮；
- ``POST /v1/auth/feishu/desktop/confirm``：浏览器确认（表单只带一次性确认串）；
- ``GET /v1/auth/feishu/desktop/status``：等待页轮询（凭 WebView 的登录尝试 cookie）；
- ``GET /v1/auth/feishu/desktop/redeem``：WebView 换取登录会话，302 到 ``return_to``。

这些接口都不需要平台凭据：等待、轮询与换取凭登录尝试 cookie，确认凭确认串。页面是固定模板，
插入的值一律转义；脚本只有等待页里的一段固定轮询脚本，经 CSP nonce 放行，不外联。
"""

from __future__ import annotations

import html
import secrets
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from xuanpu_server.api.login_http import login_response, read_bounded
from xuanpu_server.identity.cookies import (
    cookie_values,
    login_attempt_cookie_name,
    login_cookie_name,
    set_login_attempt_cookie,
)
from xuanpu_server.identity.login import LOGIN_ATTEMPT_TTL_SECONDS

if TYPE_CHECKING:
    from xuanpu_server.identity.login import LoginRedirect
    from xuanpu_server.identity.models import LoginClient
    from xuanpu_server.platform import Platform

CONFIRM_PATH = "/v1/auth/feishu/desktop/confirm"
STATUS_PATH = "/v1/auth/feishu/desktop/status"
REDEEM_PATH = "/v1/auth/feishu/desktop/redeem"
MAX_CONFIRM_BODY_BYTES = 1024
"""确认表单的最大字节数：只有一个 43 字符的确认串。"""
POLL_INTERVAL_MS = 2000
"""等待页轮询间隔（毫秒）。"""

_STYLE = (
    "body{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;"
    "background:#f6f7f5;color:#1d2a22;margin:0;display:flex;min-height:100vh;"
    "align-items:center;justify-content:center;padding:16px;box-sizing:border-box}"
    "main{background:#fff;border-radius:16px;padding:32px;max-width:440px;width:100%;"
    "box-shadow:0 2px 12px rgba(0,0,0,.06)}"
    "h1{font-size:20px;margin:0 0 12px}p{line-height:1.6;margin:8px 0}"
    ".code{font-size:34px;letter-spacing:6px;font-weight:600;margin:16px 0;"
    "font-variant-numeric:tabular-nums}"
    ".btn{display:inline-block;background:#183c2a;color:#fff;border:0;border-radius:10px;"
    "padding:12px 20px;font-size:16px;text-decoration:none;cursor:pointer;margin-top:12px}"
    ".muted{color:#5b6b61;font-size:14px}"
    "@media (prefers-color-scheme:dark){body{background:#141a16;color:#e6ece8}"
    "main{background:#1f2622}.muted{color:#9fb0a5}.btn{background:#3d7a57}}"
)


DESKTOP_USER_AGENT_TOKEN = "XuanpuDesktop/"
"""桌面客户端在 WebView 用户代理里附加的标记（见 ``xuanpu-desktop`` 的 ``policy::user_agent``）。"""


def desktop_client_of(user_agent: str | None) -> LoginClient:
    """没带 ``client`` 参数时按用户代理判断发起登录的客户端。

    用户代理可以伪造，但伪造的结果只是让自己走桌面接力（与显式带 ``client=desktop`` 相同），
    换取仍要凭只在发起方的登录尝试 cookie，不扩大任何权限。
    """
    if user_agent is not None and DESKTOP_USER_AGENT_TOKEN in user_agent:
        return "desktop"
    return "browser"


def _page(title: str, body: str, *, script: str | None = None) -> HTMLResponse:
    """把固定模板包成完整页面：不缓存、禁止被嵌入、不带 Referer；脚本凭 nonce 放行。"""
    nonce = secrets.token_urlsafe(16)
    script_tag = f'<script nonce="{nonce}">{script}</script>' if script else ""
    document = (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main>{body}</main>{script_tag}</body></html>"
    )
    csp = (
        f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
        "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    headers = {
        "Cache-Control": "no-store",
        "Content-Security-Policy": csp,
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
    return HTMLResponse(document, headers=headers)


def _wait_script() -> str:
    """等待页的固定轮询脚本：ready 时去换取，confirm 时提示去浏览器确认，失效或超时提示重试。"""
    deadline_ms = LOGIN_ATTEMPT_TTL_SECONDS * 1000
    return (
        "(()=>{const s=document.getElementById('status');const started=Date.now();"
        "const stop=(t)=>{s.textContent=t;document.getElementById('retry').hidden=false};"
        "document.getElementById('retry').addEventListener('click',()=>location.reload());"
        "const tick=async()=>{"
        f"if(Date.now()-started>{deadline_ms}){{stop('登录已超时，请重新开始。');return}}"
        f"try{{const r=await fetch('{STATUS_PATH}',{{credentials:'same-origin',cache:'no-store'}});"
        "const v=(await r.json()).status;"
        f"if(v==='ready'){{s.textContent='已确认，正在登录…';location.href='{REDEEM_PATH}';return}}"
        "if(v==='invalid'){stop('登录已失效，请重新开始。');return}"
        "s.textContent=v==='confirm'?'请在浏览器中核对确认码，并点「确认登录」。'"
        ":'等待浏览器中完成飞书授权…';"
        "}catch(e){s.textContent='暂时连不上服务器，正在重试…'}"
        f"setTimeout(tick,{POLL_INTERVAL_MS})}};tick()}})()"
    )


def wait_page_response(outcome: LoginRedirect, *, secure: bool) -> HTMLResponse:
    """桌面客户端的等待页：写登录尝试 cookie，显示确认码与在系统浏览器打开授权页的链接。

    链接用 ``target="_blank"``：桌面客户端把指向登录来源的新窗口交给系统浏览器打开。
    """
    if outcome.attempt_cookie is None or outcome.confirm_code is None:
        raise ValueError("等待页要求开始桌面登录成功的结果")
    body = (
        "<h1>用飞书登录玄圃</h1>"
        "<p>将在浏览器中打开飞书授权。授权后浏览器会显示一个确认码，请核对与下面一致：</p>"
        f'<div class="code">{html.escape(outcome.confirm_code)}</div>'
        f'<a class="btn" href="{html.escape(outcome.location)}" target="_blank" '
        'rel="noopener noreferrer">在浏览器中打开飞书授权</a>'
        '<p id="status" class="muted" role="status">等待浏览器中完成飞书授权…</p>'
        '<button id="retry" class="btn" type="button" hidden>重新开始</button>'
    )
    response = _page("用飞书登录玄圃", body, script=_wait_script())
    set_login_attempt_cookie(
        response, outcome.attempt_cookie, max_age=LOGIN_ATTEMPT_TTL_SECONDS, secure=secure
    )
    return response


def confirm_page_response(outcome: LoginRedirect) -> HTMLResponse:
    """浏览器里的确认页：显示确认码，表单带一次性确认串。不是本人发起的登录不要确认。"""
    if outcome.handoff_nonce is None or outcome.confirm_code is None:
        raise ValueError("确认页要求建了桌面接力的回调结果")
    body = (
        "<h1>确认登录玄圃桌面客户端</h1>"
        "<p>飞书授权已完成。请核对玄圃客户端上显示的确认码：</p>"
        f'<div class="code">{html.escape(outcome.confirm_code)}</div>'
        f'<form method="post" action="{CONFIRM_PATH}">'
        f'<input type="hidden" name="nonce" value="{html.escape(outcome.handoff_nonce)}">'
        '<button class="btn" type="submit">确认登录</button></form>'
        '<p class="muted">只有当你本人正在玄圃客户端登录、且客户端显示同一确认码时才确认。'
        "如果不是你发起的，请直接关闭本页。</p>"
    )
    return _page("确认登录玄圃", body)


def _confirm_result(confirmed: bool) -> HTMLResponse:
    """确认结果页。"""
    if confirmed:
        body = (
            "<h1>已确认</h1><p>请回到玄圃客户端，客户端会自动完成登录。</p>"
            '<p class="muted">现在可以关闭本页。</p>'
        )
        return _page("已确认", body)
    body = (
        "<h1>确认未成功</h1><p>这次确认已经用过或已过期。请回到玄圃客户端重新登录。</p>"
    )
    response = _page("确认未成功", body)
    response.status_code = 400
    return response


def _single(values: list[str]) -> str | None:
    """同名 cookie 恰好一个才认，否则按没带（与回调同一口径）。"""
    return values[0] if len(values) == 1 else None


def desktop_login_router(platform: Platform) -> APIRouter:
    """桌面接力的确认、轮询与换取路由。"""
    router = APIRouter()
    identity = platform.identity

    @router.post(CONFIRM_PATH)
    async def confirm(request: Request) -> HTMLResponse:
        """浏览器确认：表单字段 ``nonce``；超长、不是 ASCII 或字段不止一个都按确认失败。"""
        raw = await read_bounded(request, MAX_CONFIRM_BODY_BYTES)
        if len(raw) > MAX_CONFIRM_BODY_BYTES or not raw.isascii():
            return _confirm_result(False)
        nonces = parse_qs(raw.decode("ascii")).get("nonce", [])
        return _confirm_result(await identity.desktop.confirm(_single(nonces)))

    @router.get(STATUS_PATH)
    async def status(request: Request) -> JSONResponse:
        """等待页轮询：``{"status": "pending" | "confirm" | "ready" | "invalid"}``。"""
        secure = identity.settings.cookie_secure
        attempts = cookie_values(
            request.headers.getlist("cookie"), name=login_attempt_cookie_name(secure)
        )
        value = await identity.desktop.status(_single(attempts))
        return JSONResponse({"status": value}, headers={"Cache-Control": "no-store"})

    @router.get(REDEEM_PATH)
    async def redeem(request: Request) -> RedirectResponse:
        """换取登录会话：成功 302 到 ``return_to`` 并写登录 cookie；总清掉登录尝试 cookie。"""
        secure = identity.settings.cookie_secure
        headers = request.headers.getlist("cookie")
        outcome = await identity.desktop.redeem(
            _single(cookie_values(headers, name=login_attempt_cookie_name(secure))),
            _single(cookie_values(headers, name=login_cookie_name(secure))),
        )
        return login_response(outcome, secure=secure, clear_attempt=True)

    return router


__all__ = [
    "CONFIRM_PATH",
    "REDEEM_PATH",
    "STATUS_PATH",
    "DESKTOP_USER_AGENT_TOKEN",
    "confirm_page_response",
    "desktop_client_of",
    "desktop_login_router",
    "wait_page_response",
]
