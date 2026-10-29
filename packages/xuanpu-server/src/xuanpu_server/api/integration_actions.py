"""经 cookie 员工身份认证的通用人工连接器接入。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from xuanpu_server.api.auth import HumanCaller  # noqa: TC001 —— FastAPI 运行时解析依赖

if TYPE_CHECKING:
    from xuanpu_server.platform import Platform


class IntegrationActionBody(BaseModel):
    """人工操作仅允许声明参数与显式确认，不能注入调用来源或身份。"""
    model_config = ConfigDict(extra='forbid')
    arguments: dict[str, Any] = Field(default_factory=dict)
    confirmed: StrictBool = False


def integration_actions_router(platform: Platform) -> APIRouter:
    """登记与任何业务域无关的人工 HTTP 网关。"""
    router = APIRouter()

    @router.post('/v1/integration-actions/{package}/{connector}/{operation}')
    async def call_action(package: str, connector: str, operation: str,
                          body: IntegrationActionBody, who: HumanCaller) -> dict[str, Any]:
        """由当前有效员工 cookie 主体执行已安装包显式允许的操作。"""
        return await platform.manual_connectors.call(who, package, connector, operation,
                                                    body.arguments, confirmed=body.confirmed)

    return router
