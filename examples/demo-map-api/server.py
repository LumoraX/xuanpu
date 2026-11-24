"""本地试用的高德地图兼容接口服务（AMap REST API v3 契约）。

配合 ``examples/common-skills``、``examples/travel-planner`` 与 ``examples/driver-dispatch``
做离线试用与端到端测试；生产环境把 ``common-skills`` 的 ``amap`` 连接器保持默认
``https://restapi.amap.com`` 并在租户密钥库写入真实 ``amap-key`` 即可无缝切到高德开放平台。
"""

from __future__ import annotations

import argparse
import json
import math
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

DEFAULT_KEY = "demo-amap-key"

# 预置地点表：支持上海、北京、香港典型商务地标（含半岛酒店）
_POIS: tuple[dict[str, str], ...] = (
    {
        "id": "B00155PEN1",
        "name": "上海半岛酒店",
        "address": "中山东一路32号",
        "location": "121.488296,31.242799",
        "cityname": "上海市",
        "adcode": "310101",
    },
    {
        "id": "B000A8PEN1",
        "name": "北京王府半岛酒店",
        "address": "王府井金鱼胡同8号",
        "location": "116.415241,39.915447",
        "cityname": "北京市",
        "adcode": "110101",
    },
    {
        "id": "B00155LJZ1",
        "name": "陆家嘴金融中心",
        "address": "世纪大道8号",
        "location": "121.501654,31.238068",
        "cityname": "上海市",
        "adcode": "310115",
    },
    {
        "id": "B00155HQA1",
        "name": "上海虹桥国际机场T2航站楼",
        "address": "申达一路1号",
        "location": "121.325782,31.194127",
        "cityname": "上海市",
        "adcode": "310112",
    },
    {
        "id": "B00155JAT1",
        "name": "静安寺",
        "address": "南京西路1686号",
        "location": "121.445427,31.223251",
        "cityname": "上海市",
        "adcode": "310106",
    },
)

_WEATHER: dict[str, dict[str, str]] = {
    "310101": {
        "province": "上海",
        "city": "上海市黄浦区",
        "adcode": "310101",
        "weather": "小雨",
        "temperature": "17",
        "winddirection": "东南",
        "windpower": "≤3",
        "humidity": "78",
    },
    "310000": {
        "province": "上海",
        "city": "上海市",
        "adcode": "310000",
        "weather": "小雨",
        "temperature": "17",
        "winddirection": "东南",
        "windpower": "≤3",
        "humidity": "78",
    },
    "110101": {
        "province": "北京",
        "city": "北京市东城区",
        "adcode": "110101",
        "weather": "晴",
        "temperature": "12",
        "winddirection": "北",
        "windpower": "4",
        "humidity": "32",
    },
}


def _parse_lonlat(raw: str) -> tuple[float, float]:
    """解析 ``lon,lat`` 坐标。"""
    lon_s, lat_s = raw.split(",", 1)
    return float(lon_s), float(lat_s)


def _haversine_meters(a: tuple[float, float], b: tuple[float, float]) -> int:
    """用球面距离估算实际路程（米）。"""
    lon1, lat1 = map(math.radians, a)
    lon2, lat2 = map(math.radians, b)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    straight = 6_371_000 * 2 * math.asin(min(1.0, math.sqrt(h)))
    return max(300, int(straight * 1.28))


def _first(query: dict[str, list[str]], name: str, default: str = "") -> str:
    """取查询参数的第一个值。"""
    values = query.get(name)
    return values[0] if values else default


def _handle_ip() -> dict[str, Any]:
    """默认按上海静安周边返回城市级 IP 定位结果。"""
    return {
        "status": "1",
        "info": "OK",
        "province": "上海市",
        "city": "上海市",
        "adcode": "310000",
        "rectangle": "121.425427,31.203251;121.465427,31.243251",
    }


def _handle_regeo(query: dict[str, list[str]]) -> dict[str, Any]:
    """把经纬度转为可读街道与行政区。"""
    loc = _first(query, "location", "121.445427,31.223251")
    lon, _ = _parse_lonlat(loc)
    if lon < 118.0:
        return {
            "status": "1",
            "info": "OK",
            "regeocode": {
                "formatted_address": "北京市朝阳区建国门外大街1号",
                "addressComponent": {"province": "北京市", "city": "北京市", "adcode": "110101"},
            },
        }
    return {
        "status": "1",
        "info": "OK",
        "regeocode": {
            "formatted_address": "上海市静安区南京西路1686号附近",
            "addressComponent": {"province": "上海市", "city": "上海市", "adcode": "310106"},
        },
    }


def _handle_geo(query: dict[str, list[str]]) -> dict[str, Any]:
    """按地址文本返回匹配坐标。"""
    address = _first(query, "address")
    city = _first(query, "city")
    for poi in _POIS:
        if poi["name"] in address or address in poi["name"]:
            if not city or city in poi["cityname"]:
                return {
                    "status": "1",
                    "info": "OK",
                    "geocodes": [{
                        "formatted_address": poi["cityname"] + poi["address"],
                        "city": poi["cityname"],
                        "adcode": poi["adcode"],
                        "location": poi["location"],
                    }],
                }
    return {
        "status": "1",
        "info": "OK",
        "geocodes": [{
            "formatted_address": f"{city or '上海市'}{address}",
            "city": city or "上海市",
            "adcode": "310106",
            "location": "121.445427,31.223251",
        }],
    }


def _handle_poi(query: dict[str, list[str]]) -> dict[str, Any]:
    """按关键词和城市偏置检索 POI。"""
    keywords = _first(query, "keywords")
    city = _first(query, "city")
    citylimit = _first(query, "citylimit").lower() == "true"
    matches = [p for p in _POIS if keywords in p["name"] or keywords in p["address"]]
    if city:
        same_city = [p for p in matches if city in p["cityname"] or city == p["adcode"]]
        if same_city or citylimit:
            matches = same_city
    return {"status": "1", "info": "OK", "count": str(len(matches)), "pois": matches}


def _handle_route(path: str, query: dict[str, list[str]]) -> dict[str, Any]:
    """按起终点坐标计算驾车、公交地铁或步行路线。"""
    origin = _parse_lonlat(_first(query, "origin", "121.445427,31.223251"))
    dest = _parse_lonlat(_first(query, "destination", "121.488296,31.242799"))
    distance = _haversine_meters(origin, dest)
    if path.endswith("/driving"):
        duration = max(480, int(distance / 5.2))
        return {
            "status": "1",
            "info": "OK",
            "route": {
                "origin": _first(query, "origin"),
                "destination": _first(query, "destination"),
                "taxi_cost": str(max(16, int(14 + distance / 1000 * 2.7))),
                "paths": [{
                    "distance": str(distance),
                    "duration": str(duration),
                    "strategy": "躲避拥堵（延安高架路→外滩）",
                    "tolls": "0",
                    "traffic_lights": "11",
                }],
            },
        }
    if path.endswith("/transit/integrated"):
        duration = max(600, int(distance / 4.4) + 360)
        return {
            "status": "1",
            "info": "OK",
            "route": {
                "origin": _first(query, "origin"),
                "destination": _first(query, "destination"),
                "transits": [{
                    "cost": "4.0",
                    "duration": str(duration),
                    "walking_distance": "680",
                    "segments": [{"line": "地铁2号线（静安寺→南京东路）"}],
                }],
            },
        }
    duration = max(300, int(distance / 1.25))
    return {
        "status": "1",
        "info": "OK",
        "route": {
            "origin": _first(query, "origin"),
            "destination": _first(query, "destination"),
            "paths": [{"distance": str(distance), "duration": str(duration)}],
        },
    }


def _handle_weather(query: dict[str, list[str]]) -> dict[str, Any]:
    """返回实况或预报天气。"""
    city = _first(query, "city", "310101")
    ext = _first(query, "extensions", "base")
    base = _WEATHER.get(city) or (
        _WEATHER["110101"] if "北京" in city or city.startswith("11") else _WEATHER["310101"]
    )
    if ext == "all":
        return {
            "status": "1",
            "info": "OK",
            "forecasts": [{
                "city": base["city"],
                "adcode": base["adcode"],
                "casts": [{
                    "dayweather": base["weather"],
                    "nightweather": "阴",
                    "daytemp": base["temperature"],
                    "nighttemp": "13",
                    "daywind": base["winddirection"],
                    "daypower": base["windpower"],
                }],
            }],
        }
    return {"status": "1", "info": "OK", "lives": [base]}


class Handler(BaseHTTPRequestHandler):
    """兼容高德 REST v3 的本地试用处理器；支持按 query 参数 ``key`` 校验凭据。"""

    expected_key: str = DEFAULT_KEY

    def log_message(self, format: str, *args: object) -> None:
        """测试与试用时不向 stderr 刷屏。"""

    def _send(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        """分派高德 v3 各只读地图与天气接口。"""
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        if _first(query, "key") != self.expected_key:
            self._send(HTTPStatus.UNAUTHORIZED, {"status": "0", "info": "INVALID_USER_KEY"})
            return
        path = parsed.path
        if path == "/v3/ip":
            self._send(HTTPStatus.OK, _handle_ip())
        elif path == "/v3/geocode/regeo":
            self._send(HTTPStatus.OK, _handle_regeo(query))
        elif path == "/v3/geocode/geo":
            self._send(HTTPStatus.OK, _handle_geo(query))
        elif path == "/v3/place/text":
            self._send(HTTPStatus.OK, _handle_poi(query))
        elif path in {"/v3/direction/driving", "/v3/direction/transit/integrated", "/v3/direction/walking"}:
            self._send(HTTPStatus.OK, _handle_route(path, query))
        elif path == "/v3/weather/weatherInfo":
            self._send(HTTPStatus.OK, _handle_weather(query))
        else:
            self._send(HTTPStatus.NOT_FOUND, {"status": "0", "info": "NOT_FOUND"})


def main() -> None:
    """启动本地高德兼容地图试用服务。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8433)
    parser.add_argument("--key", default=DEFAULT_KEY)
    args = parser.parse_args()
    Handler.expected_key = args.key
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"本地地图试用服务已监听 http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
