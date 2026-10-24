"""麦当劳点餐助手 · 最小服务（标准库）

职责：托管单文件 H5 + 代理麦当劳 MCP（凭据只在服务端）。
启动：MCD_TOKEN=<token> python app/server.py
     然后手机/浏览器访问 http://127.0.0.1:8000

只读接口：/api/stores /api/menu /api/detail /api/price
不提供下单接口（涉及真实订单，按 PRD 需用户逐次确认后再接）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.config import load_estimate_params, load_taxonomy, runtime_config  # noqa: E402
from app.domain import coupons, nutrition, planner, taxonomy  # noqa: E402
from app import geo  # noqa: E402
from app.mcp_client import MCDAuthError, MCDClient, MCDError, probe_status  # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")
FACTS_FILE = os.path.join(ROOT, "data", "product-facts.json")
MENU_TTL = 300

_lock = threading.Lock()
_menu_cache: dict[tuple, tuple[float, dict]] = {}
_facts: dict[str, dict] = {}


def _load_facts() -> None:
    global _facts
    if os.path.exists(FACTS_FILE):
        try:
            with open(FACTS_FILE, encoding="utf-8") as f:
                _facts = json.load(f)
        except (OSError, json.JSONDecodeError):
            _facts = {}


def _save_facts() -> None:
    tmp = FACTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_facts, f, ensure_ascii=False, indent=1)
    os.replace(tmp, FACTS_FILE)


def normalize_category(name: str) -> str:
    return re.sub(r"[\r\n\s]+", "", name or "")


def lean_categories(categories: list[dict], out_items: list[dict]) -> list[dict]:
    """减脂模式的分类栏：按商品实际类别归位，而不是照抄门店原始分类。

    门店会把黑咖啡挂在“饮品”这类其他分类下（实测 3560147：鲜萃咖啡只出现在
    “饮品”里）。照抄的话前端会把“其他”分类隐藏，减脂用户就完全看不到咖啡了。
    所以：汉堡/咖啡分类原样保留（只留有可选商品的），其他分类里放行的商品按
    decision.kind 搬回同类分类；门店连该类分类都没有时新建一个。
    “人气热卖”这类重复分类不重复搬运，避免同一个汉堡出现两次。
    """
    kept_codes = {i["code"] for i in out_items}
    kind_of = {i["code"]: (i.get("decision") or {}).get("kind") for i in out_items}

    kept_cats: list[dict] = []
    main_codes: set[str] = set()
    for cat in categories:
        if cat.get("kind") not in ("burger", "coffee"):
            continue
        codes = [c for c in cat.get("codes") or [] if c in kept_codes]
        if codes:
            kept_cats.append({**cat, "codes": codes})
            main_codes.update(codes)

    moved: dict[str, list[str]] = {"burger": [], "coffee": []}
    for cat in categories:
        if cat.get("kind") in ("burger", "coffee"):
            continue
        for code in cat.get("codes") or []:
            if code not in kept_codes or code in main_codes:
                continue
            kind = kind_of.get(code)
            if kind in moved and code not in moved[kind]:
                moved[kind].append(code)

    for kind, codes in moved.items():
        if not codes:
            continue
        target = next((c for c in kept_cats if c.get("kind") == kind), None)
        if target:
            target["codes"].extend(codes)
        else:
            kept_cats.append({"name": "咖啡" if kind == "coffee" else "汉堡", "kind": kind, "codes": codes})

    order = {"burger": 0, "coffee": 1}
    kept_cats.sort(key=lambda c: order.get(c.get("kind"), 2))
    return kept_cats


def store_out(s: dict) -> dict:
    return {
        "storeCode": s.get("storeCode"),
        "storeName": s.get("storeName"),
        "address": s.get("address"),
        "businessStatus": s.get("businessStatus"),
        "distance": s.get("distance"),
        "businessHours": f"{s.get('businessStartTime')}-{s.get('businessEndTime')}",
    }


class App:
    def __init__(self) -> None:
        cfg = runtime_config()
        self.cfg = cfg
        self.client = MCDClient(cfg["token"], cfg["endpoint"]) if cfg["token"] else None

    # ---------- 数据获取 ----------

    def require_client(self) -> MCDClient:
        if not self.client:
            raise MCDAuthError("config", "no-token", "服务端未配置 MCD_TOKEN")
        return self.client

    def facts_for(self, store_code: str, code: str) -> dict:
        if code in _facts:
            return _facts[code]
        last_error: MCDError | None = None
        detail = None
        for attempt in range(2):  # 详情接口偶发抖动，重试一次避免商品被误判
            try:
                detail = self.require_client().data(
                    "query-meal-detail",
                    {"storeCode": store_code, "orderType": 1, "beType": 1, "code": code},
                ) or {}
                break
            except MCDAuthError:
                raise
            except MCDError as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.5)
        if detail is None:
            raise last_error or MCDError("query-meal-detail", "unknown", "详情获取失败")
        values = []
        for group in ((detail.get("modification") or {}).get("items") or []):
            for v in group.get("values") or []:
                values.append(
                    {
                        "code": v.get("code"),
                        "name": v.get("name"),
                        "price": v.get("price"),
                        "selectedKey": v.get("selectedKey"),
                        "unselectedKey": v.get("unselectedKey"),
                        "selectedQuantity": v.get("selectedQuantity"),
                    }
                )
        fact = {
            "rounds": len(detail.get("rounds") or []),
            "roundNames": [r.get("name") for r in (detail.get("rounds") or [])],
            "supportModify": detail.get("supportModify"),
            "values": values,
            "fetchedAt": int(time.time()),
        }
        with _lock:
            _facts[code] = fact
            _save_facts()
        return fact

    def menu(self, store_code: str, mode: str) -> dict:
        key = (store_code, mode)
        hit = _menu_cache.get(key)
        if hit and time.time() - hit[0] < MENU_TTL:
            return hit[1]

        data = self.require_client().data(
            "query-meals", {"storeCode": store_code, "orderType": 1, "beType": 1}
        ) or {}
        meals_map = data.get("meals") or {}
        categories = []
        items: dict[str, dict] = {}

        for cat in data.get("categories") or []:
            raw = cat.get("name") or ""
            kind = load_taxonomy().category_map.get(normalize_category(raw), "other")
            codes = []
            for meal in cat.get("meals") or []:
                code = meal.get("code")
                if not code:
                    continue
                codes.append(code)
                info = meals_map.get(code) or {}
                entry = items.setdefault(
                    code,
                    {
                        "code": code,
                        "name": info.get("name") or "",
                        "price": info.get("currentPrice"),
                        "image": info.get("image"),
                        "categories": [],
                        "kinds": [],
                    },
                )
                if raw not in entry["categories"]:
                    entry["categories"].append(raw)
                if kind not in entry["kinds"]:
                    entry["kinds"].append(kind)
            categories.append({"name": raw, "kind": kind, "codes": codes})

        # 减脂候选：汉堡/咖啡分类下的商品 + 清单里显式允许但放在其他分类的商品
        if mode == "lean":
            allowed_codes = {
                c for cat in categories if cat["kind"] in ("burger", "coffee") for c in cat["codes"]
            }
            for code, override in load_taxonomy().overrides.items():
                if code in items and override.get("category") in ("burger", "coffee"):
                    allowed_codes.add(code)
        else:
            allowed_codes = set(items.keys())

        out_items = []
        for code in allowed_codes:
            entry = items.get(code)
            if not entry:
                continue
            facts = None
            decision = taxonomy.classify(code, entry["name"], entry["categories"], None)
            if mode == "lean" and decision.allowed is None:
                try:
                    facts = self.facts_for(store_code, code)
                    decision = taxonomy.classify(code, entry["name"], entry["categories"], facts)
                except MCDError as exc:
                    sys.stderr.write(f"[detail-fail] {code} {entry['name']}: {exc}\n")
                    decision = taxonomy.classify(code, entry["name"], entry["categories"], None)
            elif mode != "lean":
                # 普通模式全部可加购，但保留类目与黑咖啡标记供热量估算使用
                decision.allowed = True

            if mode == "lean" and decision.allowed is not True:
                continue  # 减脂模式下不可选商品在三处入口都不可见（PRD §1.4.5）

            has_sauces = bool(facts and any("酱" in (v.get("name") or "") for v in facts.get("values") or []))
            # 减脂模式：汉堡强制去酱且不可取消（PRD US-01 规则 4），热量按去酱后展示
            auto_sauce_off = 1 if (mode == "lean" and decision.kind == "burger" and has_sauces) else 0

            kcal = nutrition.estimate_item(
                code,
                entry["name"],
                decision.kind,
                is_combo=decision.is_combo,
                black_coffee=decision.black_coffee,
                sauces_removed=auto_sauce_off,
            )
            out_items.append(
                {
                    **entry,
                    "decision": decision.as_dict(),
                    "kcal": {"unit": kcal["unitKcal"], "display": kcal["display"], "source": kcal["source"], "note": kcal["note"]},
                    # 三大营养素：只有官方同名数据才有值，取不到就为 null（不估算、不顶替）
                    "macros": kcal.get("macros"),
                    "macrosNote": kcal.get("macrosNote") or "",
                    "hasSauces": has_sauces,
                    "autoSauceRemoved": bool(auto_sauce_off),
                }
            )

        if mode == "lean":
            # 分类按商品实际类别归位：见 lean_categories 的说明（修“减脂菜单没有咖啡”）
            categories = lean_categories(categories, out_items)

        payload = {
            "storeCode": store_code,
            "mode": mode,
            "categories": categories,
            "items": out_items,
            "taxonomyVersion": load_taxonomy().version,
            "estimateParams": {k: v for k, v in load_estimate_params().items() if k in ("sauce_kcal_per_serving", "creamer_kcal_per_unit", "reviewed")},
        }
        _menu_cache[key] = (time.time(), payload)
        return payload

    def detail(self, store_code: str, code: str) -> dict:
        fact = self.facts_for(store_code, code)
        return {
            "code": code,
            "rounds": fact["rounds"],
            "roundNames": fact.get("roundNames") or [],
            "supportModify": fact.get("supportModify"),
            "options": fact.get("values") or [],
        }

    def price(self, body: dict) -> dict:
        store_code = body["storeCode"]
        items = []
        for it in body.get("items") or []:
            code = str(it.get("code"))
            qty = max(1, int(it.get("quantity") or 1))
            entry = {"productCode": code, "quantity": qty}
            removed = [str(x) for x in (it.get("saucesRemoved") or [])]
            if it.get("removeSauces") or removed:
                fact = self.facts_for(store_code, code)
                if it.get("removeSauces"):
                    # 前端只传“要什么效果”，酱料编码由服务端从详情里取，避免前端拼错
                    removed = [
                        v["code"]
                        for v in fact.get("values") or []
                        if "酱" in (v.get("name") or "") and v.get("unselectedKey")
                    ]
                values = []
                for v in fact.get("values") or []:
                    if not v.get("unselectedKey"):
                        continue
                    off = v["code"] in removed
                    values.append(
                        {
                            "code": v["code"],
                            "key": v["unselectedKey"] if off else v["selectedKey"],
                            "quantity": 0 if off else (v.get("selectedQuantity") or 1),
                        }
                    )
                if values:
                    entry["modification"] = {"values": values}
            items.append(entry)

        result = coupons.select_best(
            self.require_client(),
            store_code=store_code,
            items=items,
            need_tableware=bool(body.get("needTableware", True)),
        )
        data = result.get("data") or {}
        ways = [(w.get("code"), w.get("title")) for w in (data.get("takeWayList") or [])]
        return {
            "cents": result["cents"],
            "yuan": round((result["cents"] or 0) / 100, 2),
            "coupon": result.get("couponTitle"),
            "note": result.get("note"),
            "couponError": result.get("couponError"),
            "breakdown": {
                "productOriginal": data.get("productOriginalPrice"),
                "product": data.get("productPrice"),
                "delivery": data.get("deliveryPrice"),
                "packing": data.get("packingPrice"),
                "tableware": data.get("tablewarePrice"),
                "discount": data.get("discount"),
            },
            "takeWayList": ways,
        }

    def locate(self, lat: float, lng: float) -> dict:
        """浏览器定位 → 城市 + 区县 → 查附近门店（接口不吃经纬度，见 app/geo.py）。"""
        info = geo.reverse(lat, lng)
        if (info.get("countryCode") or "CN") != "CN":
            return {
                "ok": False,
                "reason": "out-of-china",
                "label": info.get("label"),
                "message": "当前定位不在中国境内，暂无可用门店",
            }

        city = geo.strip_city_suffix(info["city"])
        candidates = geo.keyword_candidates(
            info["city"],
            info.get("locality") or "",
            info.get("street") or "",
            info.get("neighborhood") or "",
        )
        last_error = None

        for kw in candidates:
            try:
                stores = self.require_client().data(
                    "query-nearby-stores",
                    {"beType": 1, "searchType": 2, "city": city, "keyword": kw},
                ) or []
            except MCDError as exc:
                last_error = exc
                continue
            if stores:
                return {
                    "ok": True,
                    "city": city,
                    "keyword": kw,
                    "label": info["label"],
                    "provider": info.get("provider"),
                    "stores": [store_out(s) for s in stores],
                }

        # 兜底：收藏门店（部分账户在某个区县下查不到结果）
        try:
            fav = self.require_client().data(
                "query-nearby-stores", {"beType": 1, "searchType": 1}
            ) or []
            if fav:
                return {
                    "ok": True,
                    "city": city,
                    "keyword": "",
                    "label": info["label"],
                    "provider": info.get("provider"),
                    "stores": [store_out(s) for s in fav],
                    "fallback": "favorites",
                }
        except MCDError as exc:
            last_error = last_error or exc

        if last_error:
            raise last_error
        return {"ok": True, "city": city, "keyword": candidates[0] if candidates else "", "label": info["label"], "stores": []}


APP = App()


class Handler(BaseHTTPRequestHandler):
    server_version = "McdOrder/0.1"

    def log_message(self, fmt, *args):  # 精简日志
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---------- 输出 ----------

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, exc: Exception) -> None:
        status = 401 if isinstance(exc, MCDAuthError) else 502
        self._json(
            status,
            {
                "ok": False,
                "state": probe_status(exc),
                "message": str(exc),
            },
        )

    def _static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            self._json(404, {"ok": False, "message": "not found"})
            return
        ctype = "text/html; charset=utf-8" if full.endswith(".html") else "application/octet-stream"
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------- 路由 ----------

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/api/health":
                self._json(
                    200,
                    {
                        "ok": True,
                        "hasToken": bool(APP.cfg["token"]),
                        "taxonomyVersion": load_taxonomy().version,
                        "estimateReviewed": bool(load_estimate_params().get("reviewed")),
                        "factsCached": len(_facts),
                    },
                )
                return
            if parsed.path == "/api/stores":
                city = (qs.get("city") or [""])[0]
                keyword = (qs.get("keyword") or [""])[0]
                args = {"beType": 1, "searchType": 2 if (city and keyword) else 1}
                if city and keyword:
                    args.update({"city": city, "keyword": keyword})
                stores = APP.require_client().data("query-nearby-stores", args) or []
                self._json(200, {"ok": True, "stores": [store_out(s) for s in stores]})
                return
            if parsed.path == "/api/menu":
                store_code = (qs.get("storeCode") or [""])[0]
                mode = (qs.get("mode") or ["normal"])[0]
                if not store_code:
                    self._json(400, {"ok": False, "message": "缺少 storeCode"})
                    return
                self._json(200, {"ok": True, **APP.menu(store_code, mode)})
                return
            if parsed.path == "/api/detail":
                store_code = (qs.get("storeCode") or [""])[0]
                code = (qs.get("code") or [""])[0]
                self._json(200, {"ok": True, **APP.detail(store_code, code)})
                return
            self._static(parsed.path)
        except (MCDError, KeyError, ValueError) as exc:
            self._error(exc)

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "message": "请求体不是合法 JSON"})
            return
        try:
            if parsed.path == "/api/price":
                self._json(200, {"ok": True, **APP.price(body)})
                return
            if parsed.path == "/api/locate":
                lat = float(body.get("lat"))
                lng = float(body.get("lng"))
                self._json(200, APP.locate(lat, lng))
                return
            if parsed.path == "/api/plan":
                store_code = str(body.get("storeCode") or "")
                if not store_code:
                    self._json(400, {"ok": False, "message": "缺少 storeCode"})
                    return
                self._json(
                    200,
                    planner.build_plans(
                        APP,
                        store_code=store_code,
                        mode=str(body.get("mode") or "normal"),
                        text=str(body.get("text") or ""),
                        cart=body.get("cart") or [],
                        skip=body.get("skip") or [],
                        need_tableware=bool(body.get("needTableware", True)),
                    ),
                )
                return
            self._json(404, {"ok": False, "message": "not found"})
        except (MCDError, KeyError, ValueError, RuntimeError) as exc:
            self._error(exc)


def main() -> None:
    _load_facts()
    cfg = runtime_config()
    if not cfg["token"]:
        print("警告：未设置 MCD_TOKEN，/api/* 将返回 401（连接失效）。H5 仍可打开。")
    addr = (cfg["host"], cfg["port"])
    print(f"H5: http://{cfg['host']}:{cfg['port']}  （手机同网段可用 http://<本机IP>:{cfg['port']}）")
    ThreadingHTTPServer(addr, Handler).serve_forever()


if __name__ == "__main__":
    main()

