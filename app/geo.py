"""把浏览器定位坐标转成麦当劳接口能吃的「城市 + 关键词」。

实测限制：
- query-nearby-stores 只接受 city + keyword 文本，**传经纬度会报“没有查询到该地址”**；
- 关键词可以细到街道/小区（“西大街街道”“前阜民里”都能查到门店，且距离比区中心更贴近）；
- 因此定位后必须做反向地理编码，尽量拿到街道/小区而非只到区县。

反查顺序：Photon（街道/小区级，免 key）→ BigDataCloud（区县级，免 key）→ 失败则手动输入。

隐私：坐标只在本次请求内用于反查，不落盘、不写日志。
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request

PHOTON_URL = "https://photon.komoot.io/reverse"
BIGDATA_URL = "https://api.bigdatacloud.net/data/reverse-geocode-client"
TIMEOUT = 8.0
UA = {"Accept": "application/json", "User-Agent": "mcd-order-assistant/0.1"}

_T2S = str.maketrans(
    {
        "區": "区", "縣": "县", "黃": "黄", "東": "东", "樂": "乐", "陽": "阳",
        "寧": "宁", "廣": "广", "開": "开", "橋": "桥", "鎮": "镇", "門": "门",
        "濱": "滨", "龍": "龙", "華": "华", "興": "兴", "慶": "庆", "雲": "云",
        "臺": "台", "灣": "湾", "閘": "闸", "楊": "杨", "盧": "卢", "莊": "庄",
        "劉": "刘", "陳": "陈", "頭": "头", "裡": "里", "迴": "回",
    }
)


def to_simplified(text: str) -> str:
    return (text or "").translate(_T2S)


def strip_city_suffix(city: str) -> str:
    """'上海市' → '上海'；'郑州' 保持。接口两种都接受，短名更稳。"""
    c = (city or "").strip()
    for suffix in ("市", "地区", "特别行政区"):
        if c.endswith(suffix) and len(c) > len(suffix) + 1:
            return c[: -len(suffix)]
    return c


def keyword_candidates(city: str, locality: str = "", street: str = "", neighborhood: str = "") -> list[str]:
    """按精度从高到低给候选关键词：小区/门牌 → 街道 → 区县 → 城市。"""
    out: list[str] = []
    for raw in (neighborhood, street, locality, city):
        item = to_simplified((raw or "").strip())
        if item and item not in out:
            out.append(item)
    return out


def _label(parts: list[str]) -> str:
    seen: list[str] = []
    for p in parts:
        p = (p or "").strip()
        if p and p not in seen:
            seen.append(p)
    return "".join(seen) or "未知位置"


def reverse_photon(lat: float, lng: float) -> dict:
    qs = urllib.parse.urlencode({"lat": lat, "lon": lng})
    req = urllib.request.Request(f"{PHOTON_URL}?{qs}", headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    features = data.get("features") or []
    if not features:
        raise RuntimeError("Photon 未返回结果")
    p = features[0].get("properties") or {}
    if (p.get("country") or "") not in ("中国", "China", "") and p.get("countrycode") != "CN":
        raise RuntimeError("定位不在中国境内")

    state = p.get("state") or ""
    city_raw = p.get("city") or ""
    district = p.get("district") or p.get("county") or ""
    street = p.get("street") or ""
    name = p.get("name") or ""

    # 直辖市坑：上海的 city 字段返回的是“黄浦区”，真正的城市在 state 里
    if state.endswith("市"):
        city_used = state
        locality_used = city_raw
        street_used = district
    elif city_raw.endswith("市"):
        city_used = city_raw
        locality_used = district
        street_used = ""
    else:
        city_used = city_raw or state
        locality_used = district
        street_used = ""

    if not city_used:
        raise RuntimeError("Photon 未返回城市名")

    # name 与行政级别重名时不当小区名
    if name in {state, city_raw, city_used, district, street_used}:
        name = ""

    city = to_simplified(city_used)
    locality = to_simplified(locality_used)
    street = to_simplified(street or street_used)
    name = to_simplified(name)
    return {
        "provider": "photon",
        "countryCode": "CN",
        "city": city,
        "locality": locality,
        "street": street,
        "neighborhood": name,
        "label": _label([city, locality, street, name]),
    }


def reverse_bigdata(lat: float, lng: float) -> dict:
    qs = urllib.parse.urlencode({"latitude": lat, "longitude": lng, "localityLanguage": "zh"})
    req = urllib.request.Request(f"{BIGDATA_URL}?{qs}", headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    if (data.get("countryCode") or "CN") != "CN":
        raise RuntimeError("定位不在中国境内")

    city = to_simplified(data.get("city") or data.get("principalSubdivision") or "")
    locality = to_simplified(data.get("locality") or "")
    return {
        "provider": "bigdatacloud",
        "countryCode": "CN",
        "city": city,
        "locality": locality,
        "street": "",
        "neighborhood": "",
        "label": _label([city, locality]),
    }


def reverse(lat: float, lng: float) -> dict:
    """先试街道级（Photon），再退回区县级（BigDataCloud）。"""
    errors = []
    for fn in (reverse_photon, reverse_bigdata):
        try:
            return fn(lat, lng)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{fn.__name__}: {exc}")
    raise RuntimeError("反向地理编码失败（" + "；".join(errors) + "）")
