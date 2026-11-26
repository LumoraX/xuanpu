"""共享地图/定位/天气技能库、出行规划匠人、司机匠人与本地高德兼容服务的校验测试。"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cli.main import EXIT_OK
from cli.main import main as cli_main
from spec import load_openapi, validate_package
from spec.skills import scan_skills

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
CATALOG = Path(__file__).resolve().parents[3] / "lumorax-catalog"
COMMON = CATALOG / "skills" / "common-skills"
WEB = CATALOG / "skills" / "web-skills"
TRAVEL = CATALOG / "artisans" / "travel-planner"
DRIVER = CATALOG / "artisans" / "driver-dispatch"
RESEARCH = CATALOG / "artisans" / "web-research"


def test_common_skills_and_artisans_validate_cleanly() -> None:
    """共享技能库及复用它们的匠人均通过规范校验，且无多余告警。"""
    lib_report = validate_package(COMMON)
    assert lib_report.ok and lib_report.warnings == (), (lib_report.errors, lib_report.warnings)
    assert lib_report.manifest is not None and lib_report.manifest.is_library

    web_report = validate_package(WEB)
    assert web_report.ok and web_report.warnings == (), (web_report.errors, web_report.warnings)
    assert web_report.manifest is not None and web_report.manifest.is_library

    # 验证分类组织（不平铺）
    categories = {s.skill_id: s.category for s in scan_skills(COMMON / "skills")}
    assert categories == {
        "location": "geo",
        "map-poi": "geo",
        "map-route": "geo",
        "weather": "environment",
        "departure-window": "planning",
    }
    web_categories = {s.skill_id: s.category for s in scan_skills(WEB / "skills")}
    assert web_categories == {
        "web-search": "web",
        "web-fetch": "web",
    }

    # 验证高德 OpenAPI 全部 operation 为只读
    api = load_openapi(COMMON / "apis" / "amap.yaml")
    expected_ops = {
        "ipLocate", "geocode", "regeocode", "searchPoi",
        "planDrivingRoute", "planTransitRoute", "planWalkingRoute", "getWeather",
    }
    assert set(api.operations) == expected_ops
    assert all(op.read_only for op in api.operations.values())

    # 验证 DeepSeek Search & Fetch OpenAPI 与连接器只读声明
    web_api = load_openapi(WEB / "apis" / "deepseek_web.yaml")
    assert set(web_api.operations) == {"searchWeb", "fetchWebpage"}
    assert web_api.operations["searchWeb"].method == "post"
    assert web_api.operations["fetchWebpage"].method == "post"
    web_conn = web_report.manifest.spec.connectors[0]
    assert web_conn.name == "web"
    assert web_conn.effect_of("searchWeb") == "read"
    assert web_conn.effect_of("fetchWebpage") == "read"

    travel_report = validate_package(TRAVEL, libraries={"common-skills": COMMON, "web-skills": WEB})
    assert travel_report.ok and travel_report.warnings == (), (travel_report.errors, travel_report.warnings)
    assert cli_main(["validate", str(TRAVEL), "--library", str(COMMON), "--library", str(WEB)]) == EXIT_OK

    driver_report = validate_package(DRIVER, libraries={"common-skills": COMMON})
    assert driver_report.ok and driver_report.warnings == (), (driver_report.errors, driver_report.warnings)
    assert cli_main(["validate", str(DRIVER), "--library", str(COMMON)]) == EXIT_OK

    research_report = validate_package(RESEARCH, libraries={"web-skills": WEB})
    assert research_report.ok and research_report.warnings == (), (research_report.errors, research_report.warnings)
    assert cli_main(["validate", str(RESEARCH), "--library", str(WEB)]) == EXIT_OK


def test_departure_window_script_calculates_exact_buffers() -> None:
    """出发窗口脚本精确计算通行耗时、天气缓冲、方式缓冲与提醒时间，拒绝心算。"""
    script = COMMON / "skills" / "planning" / "departure-window" / "scripts" / "calc_departure.py"

    def run(mode: str, weather: str, duration_s: int, extra: int = 0, now: str = "2026-11-22T12:00:00+08:00") -> dict:
        proc = subprocess.run(  # noqa: S310, S603 —— 测试本仓脚本
            [
                sys.executable, str(script),
                "2026-11-22T14:30:00+08:00", str(duration_s), mode, weather,
                "Asia/Shanghai", now, str(extra), "15",
            ],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return dict(json.loads(proc.stdout))

    # 30 分钟驾车 + 10 停车 + 10 小雨 + 10 安全余量 = 60 分钟 → 13:30 出发，13:15 提醒
    rainy = run("driving", "小雨", 1800)
    assert rainy["status"] == "ok"
    assert rainy["recommended_departure_hm"] == "13:30"
    assert rainy["recommended_reminder_hm"] == "13:15"
    assert rainy["recommended_reminder_iso"] == "2026-11-22T13:15:00+08:00"
    assert rainy["breakdown"]["weather_buffer_minutes"] == 10

    # 专车司机模式：30 分钟 + 15 落位 + 20 暴雨 + 10 安全 + 10 额外 = 85 分钟 → 13:05 发车
    chauffeur = run("chauffeur", "暴雨", 1800, extra=10)
    assert chauffeur["recommended_departure_hm"] == "13:05"
    assert chauffeur["breakdown"]["mode_buffer_minutes"] == 15
    assert chauffeur["breakdown"]["weather_buffer_minutes"] == 20

    # 临近出发与迟到状态识别
    assert run("driving", "晴", 1800, now="2026-11-22T13:50:00+08:00")["status"] == "late"
    assert run("driving", "晴", 1800, now="2026-11-22T13:30:00+08:00")["status"] == "tight"


def test_demo_map_api_answers_amap_endpoints() -> None:
    """本地高德兼容地图服务：校验 key；支持 IP 定位、POI 检索、路线规划与天气。"""
    spec = importlib.util.spec_from_file_location("demo_map", EXAMPLES / "demo-map-api" / "server.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def get(path: str, params: dict[str, str], key: str = module.DEFAULT_KEY) -> dict:
        qs = urllib.parse.urlencode({**params, "key": key})
        with urllib.request.urlopen(f"{base}{path}?{qs}", timeout=5) as resp:  # noqa: S310 —— 本机回环
            return dict(json.loads(resp.read()))

    try:
        with pytest.raises(urllib.error.HTTPError):
            get("/v3/ip", {}, key="wrong-key")

        ip = get("/v3/ip", {})
        assert ip["city"] == "上海市" and ip["adcode"] == "310000"

        poi = get("/v3/place/text", {"keywords": "半岛酒店", "city": "上海市", "citylimit": "true"})
        assert poi["pois"][0]["name"] == "上海半岛酒店"

        driving = get(
            "/v3/direction/driving",
            {"origin": "121.445427,31.223251", "destination": poi["pois"][0]["location"]},
        )
        assert int(driving["route"]["paths"][0]["duration"]) > 0

        transit = get(
            "/v3/direction/transit/integrated",
            {"origin": "121.445427,31.223251", "destination": poi["pois"][0]["location"], "city": "上海市"},
        )
        assert int(transit["route"]["transits"][0]["duration"]) > 0

        weather = get("/v3/weather/weatherInfo", {"city": "310101", "extensions": "base"})
        assert weather["lives"][0]["weather"] == "小雨"
    finally:
        server.shutdown()
