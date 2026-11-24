"""能力市场（匠人市场 + 通用技能市场）的目录扫描、筛选、依赖自动安装与权限测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from server.api.app import create_app
from server.domains.marketplace import MarketplaceConfig, MarketplaceService

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
CATALOG = Path(__file__).resolve().parents[3] / "lumorax-catalog"


async def test_marketplace_lists_artisans_and_skill_libraries_with_filters(world: Any) -> None:
    """市场按匠人/技能库分类列出独立生态目录（含 skills/artisans 分组），并汇聚传递密钥。"""
    p = world.platform
    p.marketplace = MarketplaceService(
        p.packages, p.vault, config=MarketplaceConfig(catalog_dirs=(CATALOG, EXAMPLES))
    )
    app = create_app(p)
    headers = {"Authorization": f"Bearer {world.admin_key}", "Accept-Language": "zh-CN"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as http:
        all_resp = await http.get("/v1/marketplace/items", headers=headers)
        assert all_resp.status_code == 200
        body = all_resp.json()
        assert body["counts"]["skill_library"] >= 1
        assert body["counts"]["artisan"] >= 3
        assert "geo" in body["categories"]
        assert "environment" in body["categories"]

        # Skill 市场只返回通用技能库
        skills_resp = await http.get("/v1/marketplace/items?kind=skill_library", headers=headers)
        skill_items = skills_resp.json()["items"]
        assert all(item["kind"] == "skill_library" for item in skill_items)
        common = next(item for item in skill_items if item["name"] == "common-skills")
        assert common["installed"] is False
        assert any(sk["skill_id"] == "weather" and sk["category"] == "environment" for sk in common["skills"])

        # 匠人市场中的 travel-planner 自动汇总所依赖技能库声明的 amap-key
        artisans_resp = await http.get("/v1/marketplace/items?kind=artisan&q=出行", headers=headers)
        artisan_items = artisans_resp.json()["items"]
        planner = next(item for item in artisan_items if item["name"] == "travel-planner")
        assert planner["uses"] == ["common-skills"]
        assert planner["secrets"] == [{"name": "amap-key", "configured": False}]


async def test_marketplace_install_resolves_skill_library_dependency_and_reports_secrets(
    world: Any,
) -> None:
    """一键安装出行规划匠人时自动先安装依赖的 common-skills，并提示待配置的 amap-key。"""
    p = world.platform
    p.marketplace = MarketplaceService(
        p.packages, p.vault, config=MarketplaceConfig(catalog_dirs=(CATALOG,))
    )
    app = create_app(p)
    admin_headers = {"Authorization": f"Bearer {world.admin_key}", "Accept-Language": "zh-CN"}
    member_headers = {"Authorization": f"Bearer {world.member_key}", "Accept-Language": "zh-CN"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as http:
        # 普通成员可只读浏览，不可安装
        browse = await http.get("/v1/marketplace/items", headers=member_headers)
        assert browse.status_code == 200
        forbidden = await http.post(
            "/v1/marketplace/install", json={"name": "travel-planner"}, headers=member_headers
        )
        assert forbidden.status_code == 403

        # 管理员一键安装 travel-planner → 自动先装 common-skills，再装 travel-planner
        install = await http.post(
            "/v1/marketplace/install", json={"name": "travel-planner"}, headers=admin_headers
        )
        assert install.status_code == 201
        res = install.json()
        assert [item["name"] for item in res["installed"]] == ["common-skills", "travel-planner"]
        assert all(item["status"] == "installed" for item in res["installed"])
        assert res["missing_secrets"] == ["amap-key"]

        # 再次安装幂等不报错
        again = await http.post(
            "/v1/marketplace/install", json={"name": "travel-planner"}, headers=admin_headers
        )
        assert again.status_code == 201
        assert all(item["status"] == "already_installed" for item in again.json()["installed"])

        # 配好 amap-key 后市场列表反映已配置状态
        put_secret = await http.put(
            "/v1/secrets/amap-key", json={"value": "real-demo-key"}, headers=admin_headers
        )
        assert put_secret.status_code == 200
        after = (await http.get("/v1/marketplace/items?kind=artisan", headers=admin_headers)).json()
        planner = next(item for item in after["items"] if item["name"] == "travel-planner")
        assert planner["installed"] is True
        assert planner["active"] is True
        assert planner["secrets"] == [{"name": "amap-key", "configured": True}]
