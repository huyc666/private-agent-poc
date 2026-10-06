"""weather-ops 包工具：get_current_weather。

包工具必须完全自包含：不 import server 任何模块
（沙箱隔离子进程里没有 server 包，import 会失败）。
本工具内联了多数据源降级链：
1. Open-Meteo（支持全球城市；内网可配 WEATHER_GEOCODING_URL / WEATHER_API_URL 为内部代理）
2. itboy 免费天气（覆盖国内区县，无需 key，WEATHER_ITBOY_ENABLED=false 可关闭）
"""
import json
import os
import urllib.parse
import urllib.request

_GEOCODING_URL = os.environ.get(
    "WEATHER_GEOCODING_URL", "https://geocoding-api.open-meteo.com/v1/search")
_API_URL = os.environ.get(
    "WEATHER_API_URL", "https://api.open-meteo.com/v1/forecast")
_TIMEOUT = float(os.environ.get("WEATHER_TIMEOUT", "10"))
_ITBOY_ENABLED = os.environ.get("WEATHER_ITBOY_ENABLED", "true").lower() in ("1", "true", "yes")
_ITBOY_CITY_JS = os.environ.get(
    "WEATHER_ITBOY_CITY_URL", "https://j.i8tq.com/weather2020/search/city.js")
_ITBOY_API = os.environ.get(
    "WEATHER_ITBOY_API_URL", "http://t.weather.itboy.net/api/weather/city/{code}")

_WMO_CODES = {
    0: "晴", 1: "大致晴朗", 2: "局部多云", 3: "阴",
    45: "雾", 48: "冻雾",
    51: "小毛毛雨", 53: "毛毛雨", 55: "浓毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "大阵雪",
    95: "雷暴", 96: "雷暴伴冰雹", 99: "强雷暴伴冰雹",
}

_city_tree_cache = None


def _http_get_json(url, params=None):
    full = url if params is None else f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full, headers={"User-Agent": "private-agent-poc/0.1"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _via_open_meteo(city):
    geo = _http_get_json(_GEOCODING_URL, {
        "name": city, "count": 1, "language": "zh", "format": "json"})
    if not geo.get("results"):
        raise LookupError(f"未找到城市「{city}」，请检查名称或换用英文拼写")
    loc = geo["results"][0]
    data = _http_get_json(_API_URL, {
        "latitude": loc["latitude"], "longitude": loc["longitude"],
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m",
        "timezone": "auto"})
    cur = data["current"]
    desc = _WMO_CODES.get(cur.get("weather_code"), f"天气代码 {cur.get('weather_code')}")
    region = "".join(x for x in (loc.get("admin1", ""), loc.get("country", "")) if x)
    return (f"{loc.get('name', city)}（{region}）当前天气：{desc}，"
            f"气温 {cur['temperature_2m']}°C，体感 {cur['apparent_temperature']}°C，"
            f"湿度 {cur['relative_humidity_2m']}%，风速 {cur['wind_speed_10m']} km/h"
            "（数据来源：Open-Meteo）")


def _load_city_tree():
    global _city_tree_cache
    if _city_tree_cache is None:
        req = urllib.request.Request(
            _ITBOY_CITY_JS, headers={"User-Agent": "private-agent-poc/0.1"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            text = resp.read().decode("utf-8")
        _city_tree_cache = json.loads(text[text.index("{"):])
    return _city_tree_cache


def _find_area_id(node, city):
    """在 省→市→区县 三层结构里按名字找 AREAID，同名取先匹配到的（市级优先）。"""
    if isinstance(node, dict):
        if "AREAID" in node:
            return None
        for name, child in node.items():
            key = name.rstrip("市").rstrip("省").rstrip("区")
            if key == city.rstrip("市"):
                if isinstance(child, dict) and "AREAID" in child:
                    return child["AREAID"]
                found = _find_area_id(child, city)
                if found:
                    return found
                if isinstance(child, dict):
                    for sub in child.values():
                        if isinstance(sub, dict) and "AREAID" in sub:
                            return sub["AREAID"]
            else:
                found = _find_area_id(child, city)
                if found:
                    return found
    return None


def _via_itboy(city):
    tree = _load_city_tree()
    code = _find_area_id(tree, city)
    if not code:
        raise LookupError(f"未找到城市「{city}」（兜底数据源仅覆盖国内区县）")
    data = _http_get_json(_ITBOY_API.format(code=code))
    if data.get("status") != 200:
        raise RuntimeError(f"兜底数据源返回异常: {data.get('message')}")
    d = data["data"]
    today = d["forecast"][0]
    return (f"{d.get('city', city)}当前天气：{today.get('type', '未知')}，"
            f"气温 {d.get('wendu', '?')}°C，湿度 {d.get('shidu', '?')}，"
            f"{today.get('fx', '')}{today.get('fl', '')}"
            f"（今日 {today.get('low', '')} ~ {today.get('high', '')}，"
            f"空气质量 {d.get('quality', '未知')}）（数据来源：itboy 免费天气）")


def get_current_weather(city: str) -> str:
    """查询指定城市的当前天气，返回气温、体感温度、湿度、风速和天气状况。city 为城市名，例如 "北京"、"上海"。"""
    errors = []
    not_found = None
    for source in (_via_open_meteo, _via_itboy):
        if source is _via_itboy and not _ITBOY_ENABLED:
            continue
        try:
            return source(city)
        except LookupError as e:
            not_found = str(e)
        except Exception as e:
            errors.append(f"{source.__name__}: {e}")
    if not_found and not errors:
        return not_found
    detail = "；".join(errors + ([not_found] if not_found else []))
    return ("天气查询失败: " + detail + "。"
            "私有化内网环境请将 WEATHER_GEOCODING_URL / WEATHER_API_URL "
            "配置为内部代理地址，或将 WEATHER_ITBOY_ENABLED=true 开启兜底源")
