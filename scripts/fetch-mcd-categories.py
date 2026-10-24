#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取当前门店菜单分类，生成分类归一表的回填素材。

用法（凭据只从环境变量读取，不写入任何文件）：
    Windows PowerShell:  $env:MCD_TOKEN="<你的 token>"; python scripts/fetch-mcd-categories.py
    Git Bash / macOS:    MCD_TOKEN="<你的 token>" python scripts/fetch-mcd-categories.py

可选参数：
    --store-code <门店编码>   直接指定门店，跳过收藏门店查询
    --be-type 1|2             1=到店自取（默认），2=麦乐送（需 --address-id）
    --address-id <地址ID>     麦乐送场景必填
    --out path                输出路径，默认 data/menu-categories.snapshot.json

输出：门店信息 + categories 原始名称与商品数量 + 每个分类下的商品编码/名称/标签。
本文件只做只读查询，不调用下单、支付、领券、取消等写操作。
"""

import argparse
import json
import os
import sys
import urllib.request

ENDPOINT = "https://mcp.mcd.cn"


def rpc(method, params=None, *, token, session_id=None, notify=False):
    """发一次 JSON-RPC 请求，返回 (解析后的 JSON, 新 session id)。"""
    payload = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    if not notify:
        payload["id"] = 1

    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        new_sid = resp.headers.get("Mcp-Session-Id") or session_id
        ctype = resp.headers.get("Content-Type", "")
        raw = resp.read().decode("utf-8", "replace")

    if "text/event-stream" in ctype:
        data_lines = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
        raw = data_lines[-1] if data_lines else ""
    if not raw.strip():
        return None, new_sid
    return json.loads(raw), new_sid


def _extract_json(text):
    """工具返回的是“说明文字 + JSON + 尾部提示”的混合文本，从中取出第一个完整 JSON 对象。"""
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def call_tool(name, arguments, *, token, session_id):
    resp, sid = rpc(
        "tools/call",
        {"name": name, "arguments": arguments},
        token=token,
        session_id=session_id,
    )
    result = (resp or {}).get("result") or {}
    if result.get("isError"):
        raise RuntimeError("%s 返回错误：%s" % (name, json.dumps(result, ensure_ascii=False)[:400]))
    content = result.get("content") or []
    if content and content[0].get("type") == "text":
        text = content[0]["text"]
        parsed = _extract_json(text)
        if parsed is not None:
            return parsed, sid
        return {"_raw": text}, sid
    return result.get("structuredContent") or result, sid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store-code")
    ap.add_argument("--city")
    ap.add_argument("--keyword")
    ap.add_argument("--be-type", type=int, default=1, choices=[1, 2])
    ap.add_argument("--address-id")
    ap.add_argument("--out", default=os.path.join("data", "menu-categories.snapshot.json"))
    args = ap.parse_args()

    token = os.environ.get("MCD_TOKEN", "").strip()
    if not token:
        sys.exit("缺少 MCD_TOKEN 环境变量。凭据只从环境变量读取，不要写进文件或提交到仓库。")

    _, sid = rpc(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcd-category-check", "version": "1.0"},
        },
        token=token,
    )
    rpc("notifications/initialized", None, token=token, session_id=sid, notify=True)

    store = None
    if args.store_code:
        store = {"storeCode": args.store_code}
    elif args.be_type == 1:
        lookup = {"beType": 1}
        if args.city and args.keyword:
            lookup.update({"searchType": 2, "city": args.city, "keyword": args.keyword})
        else:
            lookup["searchType"] = 1
        data, sid = call_tool("query-nearby-stores", lookup, token=token, session_id=sid)
        stores = (data.get("data") or []) if isinstance(data, dict) else []
        if not stores:
            sys.exit("没有查到门店，请用 --city 和 --keyword 指定位置，或用 --store-code 直接指定门店。")
        for s in stores:
            print("- 门店：%s (%s) 营业=%s" % (s.get("storeName"), s.get("storeCode"), s.get("businessStatus")))
        store = next((s for s in stores if s.get("businessStatus")), stores[0])
    else:
        if not args.address_id:
            sys.exit("麦乐送场景需要 --address-id")
        data, sid = call_tool(
            "delivery-query-stores",
            {"beType": 2, "addressId": args.address_id},
            token=token,
            session_id=sid,
        )
        stores = (data.get("data") or []) if isinstance(data, dict) else []
        if not stores:
            sys.exit("该地址没有可配送门店。")
        store = stores[0]

    store_code = store.get("storeCode") or store.get("code")
    be_code = store.get("beCode")
    if not store_code:
        sys.exit("没有拿到 storeCode，无法继续。")

    meal_args = {"storeCode": store_code, "orderType": 2 if args.be_type == 2 else 1, "beType": args.be_type}
    if be_code:
        meal_args["beCode"] = be_code

    data, sid = call_tool("query-meals", meal_args, token=token, session_id=sid)
    body = data.get("data") or {}
    categories = body.get("categories") or []
    meals_map = body.get("meals") or {}

    snapshot = {
        "fetchedAt": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "store": {
            "storeCode": store_code,
            "storeName": store.get("storeName"),
            "beCode": be_code,
            "beType": args.be_type,
        },
        "categories": [],
        "unmappedMeals": [],
    }

    seen = set()
    for cat in categories:
        entry = {"name": cat.get("name"), "mealCount": 0, "meals": []}
        for m in cat.get("meals") or []:
            code = m.get("code")
            info = meals_map.get(code) or {}
            entry["meals"].append(
                {"code": code, "name": info.get("name"), "tags": m.get("tags") or []}
            )
            entry["mealCount"] += 1
            seen.add(code)
        snapshot["categories"].append(entry)

    for code, info in meals_map.items():
        if code not in seen:
            snapshot["unmappedMeals"].append(
                {"code": code, "name": info.get("name"), "note": "在 meals 映射中但未出现在任何分类"}
            )

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    print("\n分类清单（回填 data/menu-taxonomy.yaml 的 category_map）：")
    for cat in snapshot["categories"]:
        print("  %-14s %3d 个商品" % (cat["name"], cat["mealCount"]))
    if snapshot["unmappedMeals"]:
        print("\n注意：%d 个商品未归入任何分类，见输出文件的 unmappedMeals。" % len(snapshot["unmappedMeals"]))
    print("\n已写入 %s" % args.out)


if __name__ == "__main__":
    main()
