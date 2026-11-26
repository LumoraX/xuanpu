"""分类技能装配、多匠人共享通用技能库、渐进披露检索与位置上下文的端到端测试。"""

from __future__ import annotations

import importlib.util
import json
import shutil
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import taifeng
import yaml

from server.core.tenancy import Principal
from server.domains.identity.models import UserRecord
from server.engine.butler.service import ButlerMessageBody, ClientLocation
from server.engine.runtime.skill_assembly import (
    CategorizedSkillRecall,
    SkillCatalogEntry,
)
from spec import pack_package

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
CATALOG = Path(__file__).resolve().parents[3] / "lumorax-catalog"
COMMON_SKILLS = CATALOG / "skills" / "common-skills"
WEB_SKILLS = CATALOG / "skills" / "web-skills"
TRAVEL_PLANNER = CATALOG / "artisans" / "travel-planner"
DRIVER_DISPATCH = CATALOG / "artisans" / "driver-dispatch"
WEB_RESEARCH = CATALOG / "artisans" / "web-research"


def _pack_web_skills_for(base_url: str, tmp_path: Path) -> bytes:
    """复制 web-skills 并将 web 连接器指向本地测试搜索服务后打包。"""
    target = tmp_path / "web-skills-local"
    shutil.copytree(WEB_SKILLS, target)
    manifest_path = target / "xuanpu.yaml"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    data["spec"]["connectors"][0]["baseUrl"] = base_url
    manifest_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return pack_package(target)


def _start_demo_web_server(expected_token: str) -> tuple[ThreadingHTTPServer, str, list[tuple[str, dict[str, Any]]]]:
    """起一个本机 DeepSeek Search & Fetch 兼容测试服务，校验 Bearer 鉴权与请求体。"""
    from http.server import BaseHTTPRequestHandler

    received: list[tuple[str, dict[str, Any]]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            if self.headers.get("Authorization") != f"Bearer {expected_token}":
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'{"error":"unauthorized"}')
                return
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            received.append((self.path, body))
            if self.path == "/v1/tools/search":
                payload = {
                    "results": [
                        {
                            "rerank_score": 2.4,
                            "title": f"{body.get('query', '')} 官方介绍",
                            "url": "https://example.com/company-profile",
                            "snippet": "公开资料摘要",
                            "summary": "星澜科技成立于 2022 年，专注企业智能体平台研发，总部位于上海。",
                            "content": "",
                        }
                    ]
                }
            elif self.path == "/v1/tools/fetch":
                payload = {
                    "url": body.get("url", ""),
                    "markdown": "# 星澜科技官网\n\n核心产品为玄圃企业智能体平台，2026 年开放联网调研生态。",
                }
            else:
                self.send_response(404)
                self.end_headers()
                return
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", received


def _start_demo_map_server() -> tuple[ThreadingHTTPServer, str, str]:
    """起一个本机高德兼容地图服务，返回 (server, base_url, api_key)。"""
    spec = importlib.util.spec_from_file_location("demo_map_test", EXAMPLES / "demo-map-api" / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", str(module.DEFAULT_KEY)


def _pack_common_skills_for(base_url: str, tmp_path: Path) -> bytes:
    """复制 common-skills 并将 amap 连接器指向本地测试地图服务后打包。"""
    target = tmp_path / "common-skills-local"
    shutil.copytree(COMMON_SKILLS, target)
    manifest_path = target / "xuanpu.yaml"
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    data["spec"]["connectors"][0]["baseUrl"] = base_url
    manifest_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return pack_package(target)


async def test_categorized_skill_recall_matches_category_and_tags() -> None:
    """CategorizedSkillRecall 按分类名、标签与描述检索，并保留分类前缀。"""
    catalog = {
        "location": SkillCatalogEntry(
            skill_id="location", category="geo", tags=("定位", "当前位置", "IP定位"),
            display_name="当前定位", description="解析当前位置与坐标",
            source_package="common-skills", rel_path="geo/location",
        ),
        "map-route": SkillCatalogEntry(
            skill_id="map-route", category="geo", tags=("地图", "路线规划", "导航"),
            display_name="路线规划", description="查询驾车与地铁耗时",
            source_package="common-skills", rel_path="geo/map-route",
        ),
        "weather": SkillCatalogEntry(
            skill_id="weather", category="environment", tags=("天气", "降雨", "预报"),
            display_name="天气预报", description="查询实况天气与出行影响",
            source_package="common-skills", rel_path="environment/weather",
        ),
    }
    pool = [taifeng.RecallEntry(skill_id=k, description=v.description) for k, v in catalog.items()]
    recall = CategorizedSkillRecall(catalog)
    cancel = taifeng.CancellationToken()

    geo_hits = await recall.recall("geo 定位", pool, top_k=2, cancel=cancel)
    assert geo_hits and geo_hits[0].skill_id == "location"
    assert geo_hits[0].description.startswith("[geo]")

    env_hits = await recall.recall("environment 降雨 天气", pool, top_k=2, cancel=cancel)
    assert env_hits and env_hits[0].skill_id == "weather"
    assert env_hits[0].description.startswith("[environment]")


async def test_shared_skills_progressive_disclosure_and_multi_artisan_reuse(
    world: Any, step: Any, tmp_path: Path
) -> None:
    """端到端：通用技能库不暴露为匠人；出行规划与司机两匠人共享它并通过 search_skills 渐进发现与调用。"""
    server, base_url, api_key = _start_demo_map_server()
    try:
        p = world.platform
        # 1. 写入租户密钥并发布共享技能库 + 两位复用它的匠人
        await p.put_secret(world.admin, "amap-key", api_key)
        await p.put_secret(world.admin, "deepseek-api-key", "sk-test-unused")
        await p.packages.publish(
            world.admin, _pack_common_skills_for(base_url, tmp_path), activate=True
        )
        await p.packages.publish(world.admin, pack_package(WEB_SKILLS), activate=True)
        await p.packages.publish(world.admin, pack_package(TRAVEL_PLANNER), activate=True)
        await p.packages.publish(world.admin, pack_package(DRIVER_DISPATCH), activate=True)

        # 2. 校验：纯技能库不出现在可用匠人列表中，两位匠人均可见
        await p.identity.store.create_user(
            UserRecord(tenant_id="acme", user_id="usr_travel", display_name="林行", created_at=1.0)
        )
        who = Principal(role="tenant_member", tenant_id="acme", user_id="usr_travel", key_id=None)
        artisans = await p.artisans.available(who)
        artisan_pkgs = {a.ref.package for a in artisans}
        assert "common-skills" not in artisan_pkgs
        assert "web-skills" not in artisan_pkgs
        assert {"travel-planner", "driver-dispatch"} <= artisan_pkgs

        # 3. 运行 travel-planner 会话：验证 deferred 渐进披露（search_skills → read_skill → call_skill → 真实地图接口 + 真实沙盒脚本）
        world.models.script(
            # 第 1 步（trip-planner）：调用 search_skills 发现定位与地图技能 + read_skill 读 L3 清单
            step(calls=[
                ("c1", "search_skills", {"query": "geo 定位 地图 路线"}),
                ("c2", "read_skill", {"skill_id": "trip-planner", "path": "references/confirmation-checklist.md"}),
            ]),
            # 第 2 步（trip-planner）：按发现结果调用共享技能 location（触发子 turn）
            step(calls=[("c3", "call_skill", {"skill_id": "location", "args": {"hint": "当前位置"}, "reason": "我需要解析员工当前位置"})]),
            # 第 3 步（location 子技能）：调用共享连接器 api__amap__ipLocate
            step(calls=[("c4", "api__amap__ipLocate", {})]),
            # 第 4 步（location 子技能）：返回定位结果
            step(text="当前按IP推算位于上海市（310000），坐标约 121.445427,31.223251。"),
            # 第 5 步（trip-planner）：调用共享技能 map-poi
            step(calls=[
                ("c5", "call_skill", {"skill_id": "map-poi", "args": {"keywords": "半岛酒店", "city": "上海市"}, "reason": "我需要检索同城半岛酒店精确地址与坐标"}),
            ]),
            # 第 6 步（map-poi 子技能）：调用共享连接器 api__amap__searchPoi
            step(calls=[("c6", "api__amap__searchPoi", {"keywords": "半岛酒店", "city": "上海市", "citylimit": True})]),
            # 第 7 步（map-poi 子技能）：返回 POI
            step(text="上海半岛酒店，中山东一路32号，坐标 121.488296,31.242799，adcode 310101。"),
            # 第 8 步（trip-planner）：调用共享技能 departure-window（带沙盒脚本）
            step(calls=[
                ("c7", "call_skill", {"skill_id": "departure-window", "args": {"target": "14:30"}, "reason": "我需要通过脚本精确倒推出发与提醒时间"}),
            ]),
            # 第 9 步（departure-window 子技能）：执行真实沙盒脚本 calc_departure
            step(calls=[(
                "c8", "run_script",
                {
                    "skill_id": "departure-window",
                    "script_name": "calc_departure",
                    "args": {
                        "target_arrival": "2026-11-22T14:30:00+08:00",
                        "duration_seconds": 1800,
                        "mode": "driving",
                        "weather": "小雨",
                        "timezone": "Asia/Shanghai",
                        "now_iso": "2026-11-22T12:00:00+08:00",
                        "extra_buffer_minutes": 0,
                        "remind_before_minutes": 15,
                    },
                },
            )]),
            # 第 10 步（departure-window 子技能）：回报计算结果
            step(text="建议 13:30 出发，13:15 提醒。"),
            # 第 11 步（trip-planner）：汇总方案请员工确认
            step(text="已为你推算到上海半岛酒店的路线与天气（小雨）：建议 13:30 出发，13:15 提醒，请确认是否设置提醒。"),
        )

        session = await p.sessions.create(who, package="travel-planner", entry="trip-planner")
        turn = await p.sessions.send_message(
            who, session.session_id,
            "今天下午2点30，我需要到半岛酒店，从我现在的位置出发，帮我看下路线和天气并给出发提醒建议",
        )
        events = [event async for event in turn.stream.events()]
        reply_text = "".join(
            str(e.msg.data.get("delta", "")) for e in events if e.msg.kind == "assistant_text"
        )
        assert "上海半岛酒店" in reply_text and "13:30" in reply_text

        # 验证首轮 prompt 为 deferred 渐进披露模式（含 search_skills，未把全部子技能说明平铺进 system prompt）
        first_req = world.models.requests[0]
        tool_names_seen = {t.name for t in first_req.tools}
        assert "search_skills" in tool_names_seen
        assert "read_skill" in tool_names_seen
        assert "call_skill" in tool_names_seen

        # 验证 search_skills、read_skill、真实地图接口与沙盒脚本均成功返回真实数据
        all_outputs = json.dumps(
            [r.model_dump() for r in world.models.requests], ensure_ascii=False, default=str
        )
        assert "[geo]" in all_outputs
        assert "上海半岛酒店" in all_outputs
        assert "2026-11-22T13:15:00+08:00" in all_outputs

        # 4. 验证第二个匠人 driver-dispatch 同样可以复用同一套 common-skills 并调用地图天气接口
        world.models.script(
            step(calls=[("d1", "search_skills", {"query": "environment 天气 预报"})]),
            step(calls=[("d2", "call_skill", {"skill_id": "weather", "args": {"city": "310101"}, "reason": "我需要查询目的地实时天气以评估行车缓冲"})]),
            step(calls=[("d3", "api__amap__getWeather", {"city": "310101", "extensions": "base"})]),
            step(text="上海黄浦区当前小雨，17℃。"),
            step(text="司机发车预案已备好：上海黄浦区小雨，建议司机提前发车并备伞，请确认。"),
        )
        drv_session = await p.sessions.create(who, package="driver-dispatch", entry="driver-assistant")
        drv_turn = await p.sessions.send_message(who, drv_session.session_id, "安排司机下午送客人去半岛酒店")
        drv_events = [event async for event in drv_turn.stream.events()]
        drv_text = "".join(
            str(e.msg.data.get("delta", "")) for e in drv_events if e.msg.kind == "assistant_text"
        )
        assert "小雨" in drv_text and "司机" in drv_text
    finally:
        server.shutdown()


async def test_butler_message_body_accepts_optional_client_location(world: Any) -> None:
    """ButlerMessageBody 支持可选的客户端位置提示，并在传入时带入管家数据载荷。"""
    body = ButlerMessageBody(
        request_id="loc-req-1",
        text="今天下午2点30到半岛酒店，帮我规划路线",
        timezone="Asia/Shanghai",
        location=ClientLocation(latitude=31.223251, longitude=121.445427, accuracy_meters=35.0),
    )
    await world.platform.identity.store.create_user(
        UserRecord(tenant_id="acme", user_id="usr_loc", display_name="林定位", created_at=1.0)
    )
    who = Principal(role="tenant_member", tenant_id="acme", user_id="usr_loc", key_id=None)
    raw = await world.platform.butler._payload(who, body)  # noqa: SLF001 —— 验证内部数据载荷字段
    parsed: dict[str, Any] = json.loads(raw)
    assert parsed["location"]["latitude"] == 31.223251
    assert parsed["location"]["longitude"] == 121.445427
    assert parsed["location"]["accuracy_meters"] == 35.0


async def test_declared_secrets_lists_package_connectors_and_config_state(world: Any) -> None:
    """GET /v1/secrets/declared 返回已配置状态与业务包声明的密钥使用方，供前端下拉选择。"""
    import httpx

    from server.api.app import create_app

    p = world.platform
    await p.packages.publish(world.admin, pack_package(COMMON_SKILLS), activate=True)
    app = create_app(p)
    headers = {"Authorization": f"Bearer {world.admin_key}", "Accept-Language": "zh-CN"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as http:
        before = (await http.get("/v1/secrets/declared", headers=headers)).json()
        assert "amap-key" not in before["names"]
        amap = next(item for item in before["declared"] if item["name"] == "amap-key")
        assert amap["configured"] is False
        assert amap["used_by"][0]["package"] == "common-skills"
        assert amap["used_by"][0]["connector"] == "amap"

        await p.put_secret(world.admin, "amap-key", "test-key")
        after = (await http.get("/v1/secrets/declared", headers=headers)).json()
        assert "amap-key" in after["names"]
        amap_after = next(item for item in after["declared"] if item["name"] == "amap-key")
        assert amap_after["configured"] is True


async def test_web_research_artisan_runs_search_and_fetch_with_sandbox_network_disabled(
    world: Any, step: Any, tmp_path: Path
) -> None:
    """端到端：联网调研匠人复用 web-skills，通过 Bearer HTTP 连接器完成先搜后抓，且脚本沙盒保持断网。"""
    demo_key = "sk-demo-deepseek-key"
    web_server, web_base_url, received = _start_demo_web_server(demo_key)
    try:
        p = world.platform
        await p.put_secret(world.admin, "deepseek-api-key", demo_key)
        await p.packages.publish(
            world.admin, _pack_web_skills_for(web_base_url, tmp_path), activate=True
        )
        await p.packages.publish(world.admin, pack_package(WEB_RESEARCH), activate=True)

        await p.identity.store.create_user(
            UserRecord(tenant_id="acme", user_id="usr_research", display_name="陈研", created_at=1.0)
        )
        who = Principal(role="tenant_member", tenant_id="acme", user_id="usr_research", key_id=None)
        artisans = await p.artisans.available(who)
        assert all(a.ref.package != "web-skills" for a in artisans)
        research = next(a for a in artisans if a.ref.package == "web-research")
        assert research.sandbox_network is False

        world.models.script(
            step(calls=[("r1", "search_skills", {"query": "web 联网 搜索 抓取"})]),
            step(calls=[
                ("r2", "call_skill", {"skill_id": "web-search", "args": {"query": "星澜科技"}, "reason": "检索目标公司公开背景"}),
            ]),
            step(calls=[
                ("r3", "api__web__searchWeb", {"body": {"query": "星澜科技", "count": 5, "need_content": False}}),
            ]),
            step(text="已检索到星澜科技官网（https://example.com/company-profile），总部位于上海。"),
            step(calls=[
                ("r4", "call_skill", {"skill_id": "web-fetch", "args": {"url": "https://example.com/company-profile"}, "reason": "精读官网正文确认核心产品"}),
            ]),
            step(calls=[
                ("r5", "api__web__fetchWebpage", {"body": {"url": "https://example.com/company-profile"}}),
            ]),
            step(text="官网正文显示其核心产品为玄圃企业智能体平台，2026 年开放联网调研生态。"),
            step(text="调研完成：星澜科技成立于 2022 年，总部上海，核心产品为玄圃企业智能体平台。来源：https://example.com/company-profile"),
        )

        session = await p.sessions.create(who, package="web-research", entry="deep-research")
        turn = await p.sessions.send_message(
            who, session.session_id, "帮我联网调研一下星澜科技的公开背景和核心产品"
        )
        events = [event async for event in turn.stream.events()]
        reply = "".join(
            str(e.msg.data.get("delta", "")) for e in events if e.msg.kind == "assistant_text"
        )
        assert "星澜科技" in reply and "https://example.com/company-profile" in reply

        all_outputs = json.dumps(
            [r.model_dump() for r in world.models.requests], ensure_ascii=False, default=str
        )
        assert "玄圃企业智能体平台" in all_outputs
        assert "[web]" in all_outputs
        assert received == [
            ("/v1/tools/search", {"query": "星澜科技", "count": 5, "need_content": False}),
            ("/v1/tools/fetch", {"url": "https://example.com/company-profile"}),
        ]
    finally:
        web_server.shutdown()
