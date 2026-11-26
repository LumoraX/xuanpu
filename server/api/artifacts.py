"""过程产物（Trace Artifact）预览与转存正式工作台路由（ADR 0025）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from server.api.auth import Caller  # noqa: TC001 —— FastAPI 运行时解析
from server.engine.artifacts.store import artifact_view

if TYPE_CHECKING:
    from server.platform import Platform


class PromoteArtifactBody(BaseModel):
    """转存过程产物到个人正式空间的请求体。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_.:-]+$")
    parent_id: str | None = Field(default=None, max_length=80)
    matter_id: str | None = Field(default=None, max_length=80)


def artifacts_router(platform: Platform) -> APIRouter:
    """挂载过程产物列表、预览与一键转存接口。"""
    router = APIRouter()

    @router.get("/v1/traces/{trace_id}/artifacts")
    async def list_artifacts(trace_id: str, who: Caller) -> dict[str, Any]:
        """列出本人指定 trace 下的过程产物。"""
        items = await platform.trace_artifacts.list_for_trace(who, trace_id)
        return {"trace_id": trace_id, "items": [artifact_view(r) for r in items]}

    @router.get("/v1/artifacts/{artifact_id}/preview")
    async def preview_artifact(artifact_id: str, who: Caller) -> dict[str, Any]:
        """预览本人过程产物（UTF-8 文本返回有界正文）。"""
        return await platform.trace_artifacts.preview(who, artifact_id)

    @router.post("/v1/artifacts/{artifact_id}/promote")
    async def promote_artifact(
        artifact_id: str, body: PromoteArtifactBody, who: Caller,
    ) -> dict[str, Any]:
        """将本人过程产物一键转存到正式个人空间。"""
        return await platform.trace_artifacts.promote_to_workspace(
            who, artifact_id, request_id=body.request_id,
            parent_id=body.parent_id, matter_id=body.matter_id,
        )

    return router
