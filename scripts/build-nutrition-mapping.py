#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把官方营养清单与门店菜单商品做初步对齐，产出营养基准表的原始素材。

用法：
    Windows:  $env:MCD_TOKEN="<token>"; python scripts/build-nutrition-mapping.py
    Git Bash: MCD_TOKEN="<token>" python scripts/build-nutrition-mapping.py

输出：
    data/nutrition-official.csv   官方营养清单（160 条，按接口返回原样转换）
    data/nutrition-mapping.csv    商品编码 ↔ 官方条目 的初步映射骨架
                                  matchType: exact（同名）/ normalized（归一后同名）/ none（需人工补估算）

说明：
    官方清单**不带商品编码**，只有商品名，且是通用品类口径（如“中薯条”“麦乐鸡5块”），
    与门店菜单名（“薯条”“麦乐鸡”“XX三件套”）并不一致，因此本脚本只做机械对齐，
    matchType=none 的行必须人工补 officialName 或填估算热量，不能直接按空值算 0。
"""

import csv
import json
import os
import re
import sys
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

SPEC = importlib.util.spec_from_file_location("fetch", os.path.join(HERE, "fetch-mcd-categories.py"))
fetch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch)

SNAPSHOTS = [
    os.path.join(ROOT, "data", "menu-categories.snapshot.json"),
    os.path.join(ROOT, "data", "menu-categories.zhengzhou.json"),
]

# 归一化时剥离的规格/包装词，仅用于提高机械匹配率，不参与最终判定
STRIP_WORDS = ["大杯", "中杯", "小杯", "迷你", "套餐", "三件套", "四件套", "双人餐", "单人餐"]

# 官方清单里除热量外的营养素列，只在 exact 匹配时填充（口径见 PRD §1.4.4）
MACRO_FIELDS = ["protein", "fat", "carbohydrate", "sodium", "calcium"]


def normalize(name: str) -> str:
    s = re.sub(r"[（(].*?[）)]", "", name)
    s = re.sub(r"[\s\r\n]+", "", s)
    for w in STRIP_WORDS:
        s = s.replace(w, "")
    s = re.sub(r"\d+块$", "", s)
    s = re.sub(r"\d+$", "", s)
    return s.strip()


def parse_nutrition(raw: str):
    """官方返回形如 '[160]{a,b,c}:\\n  名称,null,1288,308,...' 的表格文本。"""
    rows = []
    for line in raw.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 9:
            continue
        rows.append(
            {
                "productName": parts[0],
                "nutritionDescription": parts[1],
                "energyKj": parts[2],
                "energyKcal": parts[3],
                "protein": parts[4],
                "fat": parts[5],
                "carbohydrate": parts[6],
                "sodium": parts[7],
                "calcium": parts[8],
            }
        )
    return rows


def collect_menu_items():
    items = {}
    for path in SNAPSHOTS:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)
        for cat in snap.get("categories") or []:
            cat_name = re.sub(r"[\r\n\s]+", "", cat.get("name") or "")
            for meal in cat.get("meals") or []:
                code = meal.get("code")
                if not code or code in items:
                    if code in items and cat_name not in items[code]["categories"]:
                        items[code]["categories"].append(cat_name)
                    continue
                items[code] = {
                    "productCode": code,
                    "name": meal.get("name") or "",
                    "categories": [cat_name],
                }
    return list(items.values())


def main():
    token = os.environ.get("MCD_TOKEN", "").strip()
    if not token:
        sys.exit("缺少 MCD_TOKEN 环境变量。凭据只从环境变量读取。")

    _, sid = fetch.rpc(
        "initialize",
        {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "nutrition", "version": "1"}},
        token=token,
    )
    fetch.rpc("notifications/initialized", None, token=token, session_id=sid, notify=True)

    data, _ = fetch.call_tool("list-nutrition-foods", {}, token=token, session_id=sid)
    raw = data.get("data") if isinstance(data, dict) else None
    if not isinstance(raw, str):
        sys.exit("营养接口返回格式与预期不符：%r" % (str(data)[:200],))

    nutrition = parse_nutrition(raw)
    by_exact = {n["productName"]: n for n in nutrition}
    by_norm = {}
    for n in nutrition:
        by_norm.setdefault(normalize(n["productName"]), n)

    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    off_path = os.path.join(ROOT, "data", "nutrition-official.csv")
    with open(off_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(nutrition[0].keys()))
        w.writeheader()
        w.writerows(nutrition)

    items = collect_menu_items()
    rows = []
    stat = {"exact": 0, "combo": 0, "normalized": 0, "none": 0}
    for it in items:
        name = it["name"]
        is_combo = any(tok in name for tok in ["套餐", "三件套", "四件套", "双人餐", "单人餐", "开心乐园餐"])
        exact = by_exact.get(name)
        cand = exact or by_norm.get(normalize(name))

        if exact:
            kind, source, note = "exact", "official", ""
            official_name, kcal = exact["productName"], exact["energyKcal"]
            macros = {k: exact.get(k, "") for k in MACRO_FIELDS}
        elif is_combo:
            # 套餐绝不能套用单品热量：必须按 rounds 组成逐项合计
            kind, source = "combo", "estimate"
            official_name, kcal = "", ""
            note = "套餐须按 rounds 组成合计，禁止套用单品热量"
            macros = {k: "" for k in MACRO_FIELDS}
        elif cand:
            # 归一匹配可能错配规格（薯条→迷你薯条、玉米杯→大杯玉米杯），只作候选
            kind, source = "normalized", "needs-review"
            official_name, kcal = cand["productName"], cand["energyKcal"]
            note = "自动归一匹配，需人工确认规格后才可标 official"
            # 候选连热量都不采用，营养素更不能采用
            macros = {k: "" for k in MACRO_FIELDS}
        else:
            kind, source = "none", "estimate"
            official_name, kcal = "", ""
            note = "需人工补官方条目或填估算热量"
            macros = {k: "" for k in MACRO_FIELDS}

        stat[kind] += 1
        rows.append(
            {
                "productCode": it["productCode"],
                "name": name,
                "categories": "/".join(it["categories"]),
                "matchType": kind,
                "officialName": official_name,
                "energyKcal": kcal,
                "source": source,
                "reviewed": "no",
                "note": note,
                **macros,
            }
        )

    map_path = os.path.join(ROOT, "data", "nutrition-mapping.csv")
    with open(map_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print("官方营养条目：%d" % len(nutrition))
    print("门店商品条目：%d" % len(items))
    print(
        "exact %d（可直接标 official）/ combo %d（须按组成合计）/ normalized %d（候选待核）/ none %d（需人工补）"
        % (stat["exact"], stat["combo"], stat["normalized"], stat["none"])
    )
    print("已写入：%s" % off_path)
    print("已写入：%s" % map_path)


if __name__ == "__main__":
    main()
