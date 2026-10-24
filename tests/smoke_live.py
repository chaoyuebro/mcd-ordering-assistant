"""真实只读链路冒烟：选店 → 菜单 → 减脂判定 → 热量 → 试算。

只调用查询与试算，不创建订单、不领券、不改账户。
运行：
    Windows:  $env:MCD_TOKEN="<token>"; python tests/smoke_live.py --city 郑州 --keyword 二七广场
    Git Bash: MCD_TOKEN="<token>" python tests/smoke_live.py --city 郑州 --keyword 二七广场
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.config import runtime_config  # noqa: E402
from app.domain import nutrition, taxonomy  # noqa: E402
from app.mcp_client import MCDClient, MCDError, probe_status  # noqa: E402


def flatten(menu_data: dict) -> list[dict]:
    meals_map = menu_data.get("meals") or {}
    items: dict[str, dict] = {}
    for cat in menu_data.get("categories") or []:
        cat_name = cat.get("name") or ""
        for meal in cat.get("meals") or []:
            code = meal.get("code")
            if not code:
                continue
            info = meals_map.get(code) or {}
            entry = items.setdefault(
                code,
                {"code": code, "name": info.get("name") or "", "categories": [], "price": info.get("currentPrice")},
            )
            entry["categories"].append(cat_name)
    return list(items.values())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="郑州")
    ap.add_argument("--keyword", default="二七广场")
    ap.add_argument("--store-code")
    ap.add_argument("--max-detail", type=int, default=12, help="最多查多少个汉堡候选的详情")
    args = ap.parse_args()

    cfg = runtime_config()
    if not cfg["token"]:
        print("缺少 MCD_TOKEN 环境变量")
        return 2

    client = MCDClient(cfg["token"], cfg["endpoint"])
    try:
        if args.store_code:
            store = {"storeCode": args.store_code, "storeName": "(指定门店)", "businessStatus": True}
        else:
            stores = client.data(
                "query-nearby-stores",
                {"beType": 1, "searchType": 2, "city": args.city, "keyword": args.keyword},
            ) or []
            if not stores:
                print("没有查到门店")
                return 1
            store = next((s for s in stores if s.get("businessStatus")), stores[0])
            print(f"门店：{store.get('storeName')} ({store.get('storeCode')}) 共 {len(stores)} 家命中")

        store_code = store["storeCode"]
        menu = client.data("query-meals", {"storeCode": store_code, "orderType": 1, "beType": 1})
        items = flatten(menu or {})
        print(f"菜单：{len(menu.get('categories') or [])} 个分类，{len(items)} 个商品")

        # 分类归一结果概览
        kinds: dict[str, int] = {}
        for it in items:
            kind = taxonomy.classify(it["code"], it["name"], it["categories"]).kind
            kinds[kind] = kinds.get(kind, 0) + 1
        print(f"类目分布：{kinds}")

        # 只有汉堡候选需要详情（咖啡与非餐品可直接定论）
        facts: dict[str, dict] = {}
        candidates = [
            it
            for it in items
            if taxonomy.classify(it["code"], it["name"], it["categories"]).allowed is None
        ]
        print(f"需查详情才能判定的汉堡候选：{len(candidates)} 个，本次查 {min(len(candidates), args.max_detail)} 个")
        for it in candidates[: args.max_detail]:
            detail = client.data(
                "query-meal-detail",
                {"storeCode": store_code, "orderType": 1, "beType": 1, "code": it["code"]},
            ) or {}
            facts[it["code"]] = {
                "rounds": len(detail.get("rounds") or []),
                "supportModify": detail.get("supportModify"),
            }

        results = taxonomy.classify_menu(items, facts)
        allowed = [r for r in results if r["decision"]["allowed"]]
        blocked_combo = [r for r in results if r["decision"]["isCombo"]]

        print(f"\n减脂模式可加购 {len(allowed)} 项：")
        for r in allowed:
            kcal = nutrition.estimate_item(r["code"], r["name"], r["decision"]["kind"])
            print(f"  [{r['decision']['kind']:6}] {r['name']:<16} ¥{r['price']:<6} {kcal['display']:<14} 来源={kcal['source']}")

        print(f"\n被拦截：套餐 {len(blocked_combo)} 项 / 其他类目 {sum(1 for r in results if r['decision']['kind'] == 'other')} 项 / 含奶咖啡等 {sum(1 for r in results if r['decision']['kind'] == 'coffee' and not r['decision']['allowed'])} 项")
        for r in blocked_combo[:3]:
            print(f"  · {r['name']}：{r['decision']['reason']}")

        if allowed:
            target = allowed[0]
            price = client.data(
                "calculate-price",
                {
                    "storeCode": store_code,
                    "orderType": 1,
                    "beType": 1,
                    "items": [{"productCode": target["code"], "quantity": 1}],
                },
            ) or {}
            ways = [(t.get("code"), t.get("title")) for t in (price.get("takeWayList") or [])]
            print(f"\n试算（{target['name']} ×1）：price={price.get('price')} 分，取餐方式={ways}")
        return 0
    except MCDError as exc:
        print(f"失败（{probe_status(exc)}）：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
