"""仅供本机验收的飞书模拟端：真实授权重定向、PKCE 校验及一次性授权码。

只绑定 127.0.0.1，不用于生产；没有注入平台登录 cookie 的捷径。
"""

from __future__ import annotations

import hashlib
import html
import json
import secrets
import sys
from base64 import urlsafe_b64encode
from urllib.parse import urlencode

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

app = FastAPI()
attempts: dict[str, dict[str, str]] = {}
codes: dict[str, dict[str, str]] = {}
tokens: dict[str, str] = {}
stats = {"exchanges": 0, "pkce_verified": 0, "replay_rejected": 0}
USERS = {
    "alice": {"name": "林管理员", "avatar_url": "https://example.com/avatar.png"},
    "bob": {"name": '<img src=x onerror="window.pwned=1">张成员', "avatar_url": "javascript:alert(1)"},
}


@app.get("/healthz")
async def health() -> dict[str, int]:
    """让测试启动器确认模拟端就绪，也提供校验统计。"""
    return stats


@app.get("/open-apis/authen/v1/authorize")
async def authorize(request: Request) -> HTMLResponse:
    """展示选人按钮并保留平台发来的授权参数。"""
    query = dict(request.query_params)
    assert query["client_id"] == "demo-app"
    assert query["code_challenge_method"] == "S256"
    assert query["response_type"] == "code"
    attempt = secrets.token_urlsafe(16)
    attempts[attempt] = query
    buttons = "".join(
        f'<a id="{name}" href="/approve?attempt={attempt}&user={name}">{html.escape(user["name"])}</a><br>'
        for name, user in USERS.items()
    )
    return HTMLResponse("<html lang=zh-CN><meta charset=utf-8><h1>模拟飞书授权</h1>" + buttons)


@app.get("/approve")
async def approve(attempt: str, user: str) -> RedirectResponse:
    """选择员工后签发短期授权码，浏览器跳回真实平台回调。"""
    query = attempts.pop(attempt)
    assert user in USERS
    code = secrets.token_urlsafe(24)
    codes[code] = {**query, "user": user}
    params = urlencode({"state": query["state"], "code": code})
    return RedirectResponse(query["redirect_uri"] + "?" + params, status_code=302)


@app.post("/open-apis/authen/v2/oauth/token")
async def exchange(request: Request) -> JSONResponse:
    """授权码取出即消费，并核对应用、回调地址、PKCE，拒绝重放。"""
    body = await request.json()
    record = codes.pop(body.get("code", ""), None)
    if record is None:
        stats["replay_rejected"] += 1
        return JSONResponse({"code": 20003, "error": "invalid_grant"}, status_code=400)
    challenge = urlsafe_b64encode(hashlib.sha256(body["code_verifier"].encode()).digest()).rstrip(b"=").decode()
    valid = (body["client_secret"] == "demo-secret" and body["client_id"] == "demo-app"
             and body["redirect_uri"] == record["redirect_uri"]
             and body["grant_type"] == "authorization_code" and challenge == record["code_challenge"])
    if not valid:
        return JSONResponse({"code": 20049, "error": "invalid_grant"}, status_code=400)
    stats["pkce_verified"] += 1
    stats["exchanges"] += 1
    token = secrets.token_urlsafe(24)
    tokens[token] = record["user"]
    return JSONResponse({"code": 0, "access_token": token, "expires_in": 3600})


@app.get("/open-apis/authen/v1/user_info")
async def user_info(request: Request) -> dict[str, object]:
    """只接受已兑换令牌，返回对应员工身份。"""
    user = tokens[request.headers["authorization"].removeprefix("Bearer ")]
    return {"code": 0, "data": {**USERS[user], "union_id": user, "open_id": "open-" + user, "tenant_key": "demo-tenant"}}


@app.post("/open-apis/auth/v3/tenant_access_token/internal")
async def tenant_token(request: Request) -> dict[str, object]:
    """验证应用密钥后签发通讯录令牌。"""
    body = await request.json()
    assert body == {"app_id": "demo-app", "app_secret": "demo-secret"}
    return {"code": 0, "tenant_access_token": "demo-directory", "expire": 7200}


@app.get("/open-apis/contact/v3/users/batch")
async def contacts(request: Request) -> dict[str, object]:
    """通讯录对账返回请求的员工，全部保持在职。"""
    assert request.headers["authorization"] == "Bearer demo-directory"
    return {"code": 0, "data": {"items": [{"union_id": user, "status": {"is_activated": True}}
                                            for user in request.query_params.getlist("user_ids") if user in USERS]}}




@app.post("/v1/chat/completions")
async def model_reply(request: Request) -> StreamingResponse:
    """固定文本模型仅用于浏览器的发送与刷新恢复验收，不冒充真实模型验收。"""
    payload = await request.json()
    is_router = "玄圃业务分流器" in json.dumps(payload.get("messages", []), ensure_ascii=False)
    reply = json.dumps({"package":"demo", "entry":"greet", "message":None}) if is_router else "这是可恢复的测试回复。"
    if is_router and "浏览器提醒验收" in json.dumps(payload.get("messages", []), ensure_ascii=False):
        from datetime import UTC, datetime, timedelta
        reply = json.dumps({"package":"@reminders", "entry":"create", "message":None,
            "reminder":{"title":"浏览器提醒验收", "due_at":(datetime.now(UTC)+timedelta(seconds=5)).isoformat(), "timezone":"UTC", "repeat":"once"}})
    if is_router and "一小时后提醒我" in json.dumps(payload.get("messages", []), ensure_ascii=False):
        from datetime import UTC, datetime, timedelta
        reply = json.dumps({"package":"@reminders", "entry":"create", "message":None,
            "reminder":{"title":"可取消提醒", "due_at":(datetime.now(UTC)+timedelta(hours=1)).isoformat(), "timezone":"UTC", "repeat":"once"}})
    if is_router and "查看我的提醒" in json.dumps(payload.get("messages", []), ensure_ascii=False):
        reply = json.dumps({"package":"@reminders", "entry":"manage", "message":None})
    async def chunks():
        """输出合法 Chat SSE，保证前端测试不依赖真实模型。"""
        for delta, finish in [({"role": "assistant", "content": ""}, None),
                              ({"content": reply}, None), ({}, "stop")]:
            item = {"id": "test-chat", "object": "chat.completion.chunk", "created": 1,
                    "model": "mock", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(chunks(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), access_log=False)
