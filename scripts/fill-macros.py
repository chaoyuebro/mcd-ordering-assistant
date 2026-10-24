#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 nutrition-mapping.csv 增量补三大营养素列（protein/fat/carbohydrate/sodium/calcium）。

为什么不直接重跑 build-nutrition-mapping.py：
    重跑会整表重写，把人工核对过的 reviewed 标记和手填的 officialName 一起冲掉。
    本脚本只做「补列」，不动任何已有列。

口径（与热量一致，见 PRD §1.4.4）：
- 只有 source=official（同名精确匹配）的行才填数值，值取自 nutrition-official.csv；
- needs-review 的归一候选不填——它们连热量都不采用，营养素更不能采用；
- 套餐行不填——套餐须按 rounds 组成累加，不能套用单品值；
- 取不到就是空，绝不用估算值冒充官方值。

用法：
    python scripts/fill-macros.py
"""

import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OFFICIAL = os.path.join(ROOT, "data", "nutrition-official.csv")
MAPPING = os.path.join(ROOT, "data", "nutrition-mapping.csv")

MACRO_FIELDS = ["protein", "fat", "carbohydrate", "sodium", "calcium"]


def main() -> None:
    if not os.path.exists(MAPPING):
        sys.exit("缺少 %s，请先运行 build-nutrition-mapping.py" % MAPPING)

    with open(OFFICIAL, encoding="utf-8-sig") as f:
        official = {r["productName"]: r for r in csv.DictReader(f)}

    with open(MAPPING, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        base_fields = list(reader.fieldnames or [])
        rows = list(reader)

    fields = base_fields + [c for c in MACRO_FIELDS if c not in base_fields]

    filled = skipped = 0
    for row in rows:
        for name in MACRO_FIELDS:
            row.setdefault(name, "")
        if row.get("source") != "official":
            skipped += 1
            continue
        src = official.get((row.get("officialName") or "").strip())
        if not src:
            skipped += 1
            continue
        for name in MACRO_FIELDS:
            row[name] = (src.get(name) or "").strip()
        filled += 1

    with open(MAPPING, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    print("已补列：%s" % ", ".join(MACRO_FIELDS))
    print("填充 %d 行（official），跳过 %d 行（非 official / 套餐 / 无匹配）" % (filled, skipped))


if __name__ == "__main__":
    main()
