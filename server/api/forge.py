"""管家造匠（Artisan Forge）与三层不可变发布节点 HTTP 路由（ADR 0025）。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from server.api.auth import Caller  # noqa: TC001 —— FastAPI 运行时解析
from server.engine.forge.models import ReleaseLayer  # noqa: TC001 —— FastAPI 运行时解析

if TYPE_CHECKING:
    from server.platform import Platform


class CreateDraftBody(BaseModel):
    """新建造匠草稿请求体。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    package_name: str = Field(min_length=1, max_length=63)
    description: str = Field(min_length=1, max_length=500)
    entry: str = Field(default="main", min_length=1, max_length=63)
    display_name: str | None = Field(default=None, max_length=40)


class CheckoutDraftBody(BaseModel):
    """从存量版本检出草稿请求体。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    package_name: str = Field(min_length=1, max_length=63)
    version: str | None = Field(default=None, max_length=64)


class ControlMutationBody(BaseModel):
    """L-Control 强类型清单修改请求体。"""

    model_config = ConfigDict(extra="forbid")
    action: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)


class ContentPatchBody(BaseModel):
    """L-Content 受控内容补丁请求体。"""

    model_config = ConfigDict(extra="forbid")
    patches: list[dict[str, Any]] = Field(min_length=1, max_length=32)


class ReviewDraftBody(BaseModel):
    """管家审核请求体。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    summary: str = Field(min_length=1, max_length=2000)
    approved: bool


class RollbackBody(BaseModel):
    """发布节点回退请求体。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    to_release_id: str = Field(min_length=1, max_length=80)
    mode: Literal["retired", "revoked"] = "retired"


def forge_router(platform: Platform) -> APIRouter:
    """挂载造匠工作坊与三层发布节点管理接口。"""
    router = APIRouter()

    @router.post("/v1/forge/drafts")
    async def create_draft(body: CreateDraftBody, who: Caller) -> dict[str, Any]:
        """新建隔离造匠草稿。"""
        rec = await platform.forge.create_draft(
            who, package_name=body.package_name, description=body.description,
            entry=body.entry, display_name=body.display_name,
        )
        return rec.model_dump(mode="json")

    @router.post("/v1/forge/drafts/checkout")
    async def checkout_draft(body: CheckoutDraftBody, who: Caller) -> dict[str, Any]:
        """从已发布不可变版本检出造匠草稿。"""
        rec = await platform.forge.checkout_draft(
            who, package_name=body.package_name, version=body.version
        )
        return rec.model_dump(mode="json")

    @router.post("/v1/forge/drafts/{draft_id}/control")
    async def mutate_control(
        draft_id: str, body: ControlMutationBody, who: Caller,
    ) -> dict[str, Any]:
        """L-Control 强类型代码工具修改 ``xuanpu.yaml``。"""
        res = await platform.forge.apply_control(who, draft_id, body.action, body.params)
        return {"summary": res.summary, "version": res.manifest.metadata.version, "diff": res.diff}

    @router.post("/v1/forge/drafts/{draft_id}/content")
    async def patch_content(
        draft_id: str, body: ContentPatchBody, who: Caller,
    ) -> dict[str, Any]:
        """L-Content 受控内容编辑（PreToolUseHook 硬拒裸改 ``xuanpu.yaml``）。"""
        res = await platform.forge.apply_content_patches(who, draft_id, body.patches)
        return {
            "ok": res.ok, "message": res.message,
            "diffs": res.diffs, "post_checks": list(res.post_checks),
        }

    @router.get("/v1/forge/drafts/{draft_id}/diff")
    async def draft_diff(draft_id: str, who: Caller) -> dict[str, Any]:
        """查看造匠草稿相对基线的全部文件 Diff。"""
        return {"draft_id": draft_id, "diffs": await platform.forge.list_diff(who, draft_id)}

    @router.post("/v1/forge/drafts/{draft_id}/gates")
    async def run_gates(draft_id: str, who: Caller) -> dict[str, Any]:
        """依次执行 L1 静态校验、L2 沙盒单测与 L3 评测契约核验。"""
        l1 = await platform.forge.run_l1_validate(who, draft_id)
        l2 = await platform.forge.run_l2_unit_tests(who, draft_id)
        l3 = await platform.forge.run_l3_eval_check(who, draft_id)
        return {
            "passed": l1.passed and l2.passed and l3.passed,
            "l1": l1.model_dump(mode="json"),
            "l2": l2.model_dump(mode="json"),
            "l3": l3.model_dump(mode="json"),
        }

    @router.post("/v1/forge/drafts/{draft_id}/review")
    async def review_draft(
        draft_id: str, body: ReviewDraftBody, who: Caller,
    ) -> dict[str, Any]:
        """管家审核造匠草稿并固定 Diff、权限变化与门禁证据。"""
        rec = await platform.forge.review_draft(
            who, draft_id, summary=body.summary, approved=body.approved
        )
        return rec.model_dump(mode="json")

    @router.post("/v1/forge/drafts/{draft_id}/publish")
    async def publish_draft(draft_id: str, who: Caller) -> dict[str, Any]:
        """冻结不可变归档、发布版本并生成 L1 不可变发布节点。"""
        rec, release = await platform.forge.publish_draft(who, draft_id)
        return {"draft": rec.model_dump(mode="json"), "release": release.model_dump(mode="json")}

    @router.get("/v1/releases/{layer}/{target_name}")
    async def list_releases(
        layer: ReleaseLayer, target_name: str, who: Caller,
    ) -> dict[str, Any]:
        """查询指定层与目标的追加式不可变发布历史。"""
        tenant = who.require_tenant()
        items = await platform.releases.history(tenant, layer, target_name)
        return {"layer": layer, "target_name": target_name, "items": [i.model_dump(mode="json") for i in items]}

    @router.post("/v1/releases/artisan/{package_name}/rollback")
    async def rollback_artisan(
        package_name: str, body: RollbackBody, who: Caller,
    ) -> dict[str, Any]:
        """将 L1 匠人安全回退到历史不可变发布节点。"""
        node = await platform.forge.rollback_artisan(
            who, package_name, to_release_id=body.to_release_id, mode=body.mode
        )
        return node.model_dump(mode="json")

    @router.post("/v1/releases/{layer}/{target_name}/disable")
    async def disable_release(
        layer: ReleaseLayer, target_name: str, who: Caller, revoke: bool = True,
    ) -> dict[str, Any]:
        """禁用或紧急熔断当前生效发布节点。"""
        tenant = who.require_tenant()
        node = await platform.releases.disable(tenant, layer=layer, target_name=target_name, revoke=revoke)
        return node.model_dump(mode="json")

    return router
