"""热量计算（PRD §1.4.4）。

口径：
- 只有 `source=official`（同名精确匹配）才按官方值计算；
- `needs-review` 的归一候选**不参与计算**，只能作为人工核对提示，避免“薯条套用迷你薯条”这类错值；
- 套餐一律按组成累加，禁止取主单品值（本模块直接拒绝对 combo 取单品热量）；
- 去酱按参数扣减、奶油球按颗数增加，结果始终标注估算来源。
"""

from __future__ import annotations

from ..config import load_estimate_params, load_nutrition_map

# 三大营养素（外加钠、钙），单位：克 / 毫克
MACRO_KEYS = ("protein", "fat", "carbohydrate")
MINERAL_KEYS = ("sodium", "calcium")


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def macros_of(code: str) -> dict | None:
    """单个商品的官方三大营养素（单份）。取不到就返回 None，绝不用估算值顶替。

    口径与热量一致：只有 `source=official`（同名精确匹配）才有值。
    needs-review 候选连热量都不采用，营养素同样不采用；套餐必须按 rounds 组成
    累加，不能套用某个单品值，因此这里对套餐也会返回 None。
    """
    row = load_nutrition_map().get(str(code))
    if not row or row.get("source") != "official":
        return None
    values = {}
    for key in MACRO_KEYS:
        value = _num(row.get(key))
        if value is None:
            return None  # 三项缺一即视为不完整，不展示半张表
        values[key] = value
    for key in MINERAL_KEYS:
        value = _num(row.get(key))
        if value is not None:
            values[key] = value
    return values


def macros_text(macros: dict | None) -> str:
    """给界面用的短文案，如「蛋白 16g · 脂肪 13g · 碳水 35g」。"""
    if not macros:
        return ""
    return "蛋白 %gg · 脂肪 %gg · 碳水 %gg" % (
        round(macros["protein"], 1),
        round(macros["fat"], 1),
        round(macros["carbohydrate"], 1),
    )


def _heuristic_key(kind: str, black_coffee: bool | None, is_combo: bool) -> str:
    if is_combo:
        return "combo"
    if kind == "coffee":
        return "coffee_black" if black_coffee else "coffee_milk"
    return kind


def base_kcal(
    code: str,
    kind: str = "other",
    *,
    black_coffee: bool | None = None,
    is_combo: bool = False,
) -> tuple[float | None, str, str]:
    """返回 (单份热量, 来源, 说明)。来源：official / heuristic / unknown。"""
    row = load_nutrition_map().get(str(code))
    params = load_estimate_params()
    heuristic = (params.get("heuristic_kcal_by_kind") or {}).get(
        _heuristic_key(kind, black_coffee, is_combo)
    )

    if row and row.get("source") == "official":
        kcal = _num(row.get("energyKcal"))
        if kcal is not None:
            return kcal, "official", row.get("officialName") or row.get("name") or ""

    if heuristic is not None:
        note = "无同名官方记录，按大类估算"
        if row and row.get("matchType") == "normalized":
            note += f"（候选官方值 {row.get('officialName')} 待核对，未采用）"
        return float(heuristic), "heuristic", note

    return None, "unknown", "缺少热量数据，需人工补录"


def estimate_item(
    code: str,
    name: str,
    kind: str = "other",
    *,
    quantity: int = 1,
    sauces_removed: int = 0,
    creamer_units: int = 0,
    is_combo: bool = False,
    black_coffee: bool | None = None,
) -> dict:
    """计算单个商品（含定制）的热量。返回结构供 H5/Skill 直接展示。"""
    params = load_estimate_params()
    unit, source, note = base_kcal(
        code, kind, black_coffee=black_coffee, is_combo=is_combo
    )

    lines = []
    if unit is not None:
        lines.append({"label": name, "kcal": round(unit), "source": source})

    if sauces_removed:
        deduct = float(params.get("sauce_kcal_per_serving") or 0) * sauces_removed
        unit = max(0.0, (unit or 0.0) - deduct)
        source = "estimate" if source == "official" else source
        lines.append({"label": f"去除酱料 ×{sauces_removed}", "kcal": -round(deduct), "source": "estimate"})

    if creamer_units:
        extra = float(params.get("creamer_kcal_per_unit") or 0) * creamer_units
        unit = (unit or 0.0) + extra
        source = "estimate" if source == "official" else source
        lines.append({"label": f"奶油球 ×{creamer_units}", "kcal": round(extra), "source": "estimate"})

    unit = unit or 0.0
    total = unit * max(1, quantity)
    if is_combo:
        note = "套餐：此处仅为占位估算，正式计算须按 rounds 组成逐项累加"

    return {
        "code": str(code),
        "name": name,
        "quantity": max(1, quantity),
        "unitKcal": round(unit),
        "totalKcal": round(total),
        "display": f"约 {round(total)} 千卡",
        "source": "mixed" if len({ln['source'] for ln in lines}) > 1 else source,
        "note": note,
        "lines": lines,
        "hasData": source != "unknown",
        # 三大营养素只认官方同名值；去酱只按估算扣热量，营养素不跟着改（无法拆分酱料构成）
        "macros": macros_of(code) if not is_combo else None,
        "macrosNote": "去酱仅按估算扣热量，蛋白质/脂肪/碳水仍为原值" if sauces_removed else "",
    }


def sum_cart(items: list[dict]) -> dict:
    """整餐合计：先合计未取整值再取整（PRD US-02）。"""
    total = 0.0
    unknown = 0
    macros_total = {key: 0.0 for key in MACRO_KEYS}
    covered = 0
    units = 0
    for it in items:
        quantity = max(1, it.get("quantity", 1))
        result = estimate_item(
            it.get("code", ""),
            it.get("name", ""),
            it.get("kind", "other"),
            quantity=quantity,
            sauces_removed=len(it.get("saucesRemoved") or []),
            creamer_units=it.get("creamerUnits", 0),
            is_combo=it.get("isCombo", False),
            black_coffee=it.get("blackCoffee"),
        )
        total += result["totalKcal"]
        units += quantity
        if not result["hasData"]:
            unknown += 1
        macros = result.get("macros")
        if macros:
            covered += quantity
            for key in MACRO_KEYS:
                macros_total[key] += macros[key] * quantity
    return {
        "totalKcal": round(total),
        "display": f"约 {round(total)} 千卡",
        "unknownItems": unknown,
        # 只要有商品缺数据，合计就是「部分商品」的口径，必须让调用方知道
        "macros": {key: round(value, 1) for key, value in macros_total.items()} if covered else None,
        "macrosCovered": covered,
        "macrosUnits": units,
        "macrosComplete": covered == units and units > 0,
    }
