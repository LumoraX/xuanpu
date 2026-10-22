"""企业微信客户端授权与本人账号链接。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from xuanpu_server.api.auth import Caller  # noqa: TC001 —— FastAPI运行时解析员工身份依赖
from xuanpu_server.identity.cookies import cookie_values, login_cookie_name, set_login_cookie

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


def wecom_router(platform: Platform) -> APIRouter:
    """认证回调使用独立的防跨浏览器cookie，避免与飞书串用。"""
    router=APIRouter()
    secure=platform.identity.settings.cookie_secure
    cookie_name='__Host-xuanpu_wecom' if secure else 'xuanpu_wecom'

    async def begin(tenant: str,connection: str,return_to: str,who: Caller | None=None) -> RedirectResponse:
        """构造受限跳转并设置五分钟尝试cookie。"""
        outcome=await platform.wecom_login.start(tenant,connection,return_to,who=who)
        response=RedirectResponse(outcome['url'],status_code=302,headers={'Cache-Control':'no-store'})
        response.set_cookie(cookie_name,outcome['cookie'],max_age=300,path='/',secure=secure,httponly=True,samesite='lax')
        return response

    @router.get('/v1/auth/wecom/start')
    async def start(tenant: str=Query(max_length=63),connection: str=Query(max_length=100),
                    return_to: str=Query(default='/console/',max_length=2048)) -> RedirectResponse:
        """预配员工的企业微信客户端授权入口。"""
        return await begin(tenant,connection,return_to)

    @router.post('/v1/me/connections/{cid}/binding/start')
    async def bind_start(cid: str,who: Caller) -> JSONResponse:
        """当前员工本人确认链接自己的企微身份。"""
        outcome=await platform.wecom_login.start(who.require_tenant(),cid,'/console/',who=who)
        response=JSONResponse({'authorization_url':outcome['url']},headers={'Cache-Control':'no-store'})
        response.set_cookie(cookie_name,outcome['cookie'],max_age=300,path='/',secure=secure,httponly=True,samesite='lax')
        return response

    @router.get('/v1/auth/wecom/callback')
    async def callback(request: Request,state: str=Query(default='',max_length=128),
                       code: str=Query(default='',max_length=512)) -> RedirectResponse:
        """单次回调，成功后替换登录会话并删除旧会话。"""
        attempts=cookie_values(request.headers.getlist('cookie'),name=cookie_name)
        logins=cookie_values(request.headers.getlist('cookie'),name=login_cookie_name(secure))
        cookie=attempts[0] if len(attempts)==1 else None
        previous=logins[0] if len(logins)==1 else None
        outcome=await platform.wecom_login.callback(state,code,cookie,previous)
        response=RedirectResponse(outcome['return_to'],status_code=302,
                                  headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer'})
        response.delete_cookie(cookie_name,path='/',secure=secure,httponly=True,samesite='lax')
        set_login_cookie(response,outcome['session_token'],max_age=outcome['max_age'],secure=secure)
        return response

    return router
