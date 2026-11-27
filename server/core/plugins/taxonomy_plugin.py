"""企业文档自动业务归类与自动归档 L2 后端插件（ADR 0027）。

- 提供在线单文件归类（预览/落盘）、工作台未归档文件批量一键归类、按业务域统计看板，
  以及持久化至 ``PluginCollectionStore`` 的个人/租户自定义关键词归类规则；
- 大文件（含 14MB+ Markdown/TXT/CSV/JSON）采用「首中尾有界采样 + 章节骨架」毫秒级分类，不撑爆内存。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from server.core.errors import InvalidRequestError
from server.core.ids import new_id
from server.core.plugins.models import BackendPluginManifest, PluginHealth
from server.domains.workspace.taxonomy import (
    BUILTIN_CATEGORIES,
    CustomTaxonomyRule,
    auto_file_workspace_document,
)

if TYPE_CHECKING:
    from server.core.plugins.host import PluginContext
    from server.core.tenancy import Principal


class DocTaxonomyBackendPlugin:
    """L2 业务域插件：文档自动业务归类、事项匹配、自动建目录归档与自定义规则管理。"""

    manifest = BackendPluginManifest(
        plugin_id="doc-taxonomy",
        version="1.0.0",
        display_name="文档业务归类与自动归档",
        description="按 10 大企业业务域与自定义规则自动识别文档类别、关联项目事项、移动至规范目录并同步知识索引。",
        trust_level="in_process_verified",
        permissions=("collection.read", "collection.write"),
        collections=("rules", "records"),
    )

    def __init__(self) -> None:
        """初始化私有宿主上下文引用。"""
        self._ctx: PluginContext | None = None

    async def mount(self, ctx: PluginContext) -> None:
        """注册文档归类、批量归档、分类统计与规则配置动态路由及工具。"""
        self._ctx = ctx
        ctx.add_route("GET", "categories", self._list_categories)
        ctx.add_route("GET", "rules", self._list_rules)
        ctx.add_route("PUT", "rules", self._upsert_rule)
        ctx.add_route("DELETE", "rules", self._delete_rule)
        ctx.add_route("POST", "classify", self._classify_one)
        ctx.add_route("POST", "batch-classify", self._batch_classify)
        ctx.add_route("GET", "summary", self._summary)
        ctx.add_tool(
            "classify_document",
            "对个人工作台文件执行业务归类并自动归档至规范目录、标签与关联事项。",
            {"item_id": {"type": "string"}, "apply": {"type": "boolean"}},
            self._tool_classify,
            required=("item_id",),
        )

    async def health(self) -> PluginHealth:
        """探活检查。"""
        ok = self._ctx is not None
        return PluginHealth(ok=ok, detail="mounted" if ok else "unmounted", checked_at=datetime.now(UTC))

    async def unmount(self, *, drain_timeout: float = 15.0) -> None:
        """干净卸载。"""
        _ = drain_timeout
        self._ctx = None

    async def load_custom_rules(self, who: Principal) -> list[CustomTaxonomyRule]:
        """从共享 PluginCollectionStore 读取当前用户保存的自定义业务分类规则。"""
        if self._ctx is None:
            return []
        rows = await self._ctx.collections.list(who, self.manifest.plugin_id, "rules", limit=100)
        rules: list[CustomTaxonomyRule] = []
        for row in rows:
            try:
                rules.append(CustomTaxonomyRule.model_validate(row.data))
            except Exception:
                continue
        return rules

    async def _list_categories(self, who: Principal, _sub: str, _payload: dict[str, Any]) -> dict[str, Any]:
        custom = await self.load_custom_rules(who)
        return {
            "builtin": [
                {
                    "category_id": c.category_id,
                    "display_name": c.display_name,
                    "folder_name": c.folder_name,
                    "default_tags": list(c.default_tags),
                }
                for c in BUILTIN_CATEGORIES
            ],
            "custom_rules": [r.model_dump(mode="json") for r in custom],
        }

    async def _list_rules(self, who: Principal, _sub: str, _payload: dict[str, Any]) -> dict[str, Any]:
        rules = await self.load_custom_rules(who)
        return {"items": [r.model_dump(mode="json") for r in rules]}

    async def _upsert_rule(self, who: Principal, _sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._ctx is not None
        rule = CustomTaxonomyRule.model_validate(payload)
        saved = await self._ctx.collections.put(
            who, self.manifest.plugin_id, "rules", rule.model_dump(mode="json"), record_id=rule.rule_id,
        )
        return {"rule": saved.data, "version": saved.version}

    async def _delete_rule(self, who: Principal, _sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._ctx is not None
        rule_id = str(payload.get("rule_id") or "").strip()
        if not rule_id:
            raise InvalidRequestError("缺少 rule_id")
        await self._ctx.collections.delete(who, self.manifest.plugin_id, "rules", rule_id)
        return {"deleted": True, "rule_id": rule_id}

    async def _classify_one(self, who: Principal, _sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._ctx is not None
        item_id = str(payload.get("item_id") or "").strip()
        if not item_id:
            raise InvalidRequestError("缺少 item_id")
        apply_flag = bool(payload.get("apply", True))
        index_kb = bool(payload.get("index_knowledge", True))
        rules = await self.load_custom_rules(who)
        res = await auto_file_workspace_document(
            self._ctx.platform,
            who,
            item_id,
            apply=apply_flag,
            index_knowledge=index_kb,
            custom_rules=rules,
            request_id=new_id("tax"),
        )
        # 持久化归类回执，供分类看板查询
        await self._ctx.collections.put(
            who,
            self.manifest.plugin_id,
            "records",
            {
                "item_id": item_id,
                "filename": res["filename"],
                "applied": apply_flag,
                "folder_id": res["folder_id"],
                "knowledge_indexed": res["knowledge_indexed"],
                "classification": res["classification"],
                "classified_at": datetime.now(UTC).isoformat(),
            },
            record_id=item_id,
        )
        return {k: v for k, v in res.items() if k != "prev_data"}

    async def _batch_classify(self, who: Principal, _sub: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert self._ctx is not None
        only_unfiled = bool(payload.get("only_unfiled", True))
        apply_flag = bool(payload.get("apply", True))
        limit = max(1, min(int(payload.get("limit") or 50), 200))
        files = await self._ctx.platform.workspace.list(who, kind="file")
        targets = [
            f for f in files
            if (not only_unfiled or (f["data"].get("parent_id") is None and not f["data"].get("tags")))
            and f["data"].get("source_url") is None
        ][:limit]
        results: list[dict[str, Any]] = []
        for f in targets:
            one = await self._classify_one(
                who, "classify", {"item_id": f["id"], "apply": apply_flag},
            )
            results.append({
                "item_id": f["id"],
                "filename": one["filename"],
                "category_id": one["classification"]["category_id"],
                "category_name": one["classification"]["category_name"],
                "folder_name": one["classification"]["folder_name"],
                "confidence": one["classification"]["confidence"],
                "applied": one["applied"],
            })
        return {"processed": len(results), "items": results}

    async def _summary(self, who: Principal, _sub: str, _payload: dict[str, Any]) -> dict[str, Any]:
        assert self._ctx is not None
        rows = await self._ctx.collections.list(who, self.manifest.plugin_id, "records", limit=200)
        by_category: dict[str, int] = {}
        for row in rows:
            cat = str((row.data.get("classification") or {}).get("category_name") or "未分类")
            by_category[cat] = by_category.get(cat, 0) + 1
        all_files = await self._ctx.platform.workspace.list(who, kind="file")
        unfiled = sum(
            1 for f in all_files
            if f["data"].get("parent_id") is None and not f["data"].get("tags")
        )
        return {
            "total_files": len(all_files),
            "classified_count": len(rows),
            "unfiled_count": unfiled,
            "by_category": by_category,
            "recent": [r.data for r in rows[:15]],
        }

    async def _tool_classify(self, who: Principal, args: dict[str, Any]) -> dict[str, Any]:
        return await self._classify_one(who, "classify", args)
