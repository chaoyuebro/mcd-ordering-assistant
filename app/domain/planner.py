"""智能配餐（PRD US-03）。

三件事，按 PRD 的顺序：
1. **解析需求**：只认文本里真实出现过的条件，没提的绝不自作主张补上限（规则 2）。
   临界值语义严格区分：「不超过/以内/以下」含上限，「低于/小于」不含（规则 3）。
2. **生成组合**：在模式允许的候选里做有界枚举，先满足必选项/热量/预算，再按偏好排序（规则 4、5）。
3. **真实试算**：最终方案必须调 calculate-price 拿优惠后应付金额，估算价只用于初筛（规则 5、场景五）。

不在这里做的事：不创建订单、不静默放宽条件、不把未试算的价格标成应付价。
"""

from __future__ import annotations

import itertools
import os
import re
from typing import Any

from .. import llm
from . import coupons, nutrition

# 展示方案上限：条件宽松（可组合的候选多）时给到 6 个，条件紧时保持 3 个，
# 避免为了凑数量做无谓的试算，也让手机上的列表不至于太长（PRD 规则 5）
MAX_PLANS = 6
NARROW_PLANS = 3
# 通过初筛的候选超过这个数，说明约束比较松，值得多给几个方案
WIDE_CANDIDATE_THRESHOLD = 12
# 组合枚举上限，防止门店菜单特别大时炸掉
MAX_CANDIDATES = 600
# 初筛后最多试算几个候选（每个候选还会内部比较券，见 coupons.MAX_TRIALS）
MAX_TRIAL_CANDIDATES = 16
# 无解时为了给出可信的最低实价，最多多试几组（列表价不可信）
BLOCKER_TRIALS = 6
# 单个候选的试算预算（含无券基线），控制整个配餐请求的耗时
TRIAL_BUDGET = 4

CN_NUM = {
    "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

KCAL_UNIT = r"(?:千卡|大卡|kcal|Kcal|KCAL|卡路里|卡)"
MONEY_UNIT = r"(?:元钱|元|块|RMB|rmb|¥)"
# 「含上限」的措辞
INCL_WORDS = r"(?:不超过|不得超过|以内|以下|之内|最多|至多|控制在|控制到|限制在|限|别超过)"
# 「不含上限」的措辞
EXCL_WORDS = r"(?:低于|小于|少于|不到|不高于)"

# 主餐 / 饮品 / 小食的类目划分
MAIN_KINDS = ("burger", "combo")
DRINK_KINDS = ("coffee", "drink")
SIDE_KINDS = ("side", "dessert")
STRUCT_KINDS = {"burger": "主餐", "combo": "套餐", "coffee": "咖啡", "drink": "饮品", "side": "小食", "dessert": "甜品"}


def _cn2int(text: str) -> int:
    if text.isdigit():
        return int(text)
    return CN_NUM.get(text, 0)


def _clean_name(name: str) -> str:
    return re.sub(r"[™®\s（）()·\-—_]", "", name or "")


def _name_hit(name: str, text: str) -> bool:
    """商品名是否被用户提到。允许少写「汉堡/卷/咖啡」这类后缀。

    短名（薯条、可乐等 2 字）也要认，否则「要一份薯条」永远识别不出来；
    这里要求的是**完整名称**原样出现，误伤风险很低。
    """
    full = _clean_name(name)
    if len(full) >= 2 and full in text:
        return True
    for suffix in ("汉堡", "咖啡", "卷", "堡", "茶"):
        if full.endswith(suffix) and len(full) - len(suffix) >= 2:
            stem = full[: -len(suffix)]
            if stem in text:
                return True
    # 用户可能只写了名字中间一段（≥4 字较安全）
    return len(full) >= 5 and full[:4] in text


# ---------- 1. 解析需求 ----------

# 三大营养素的自然语言别名（长词在前，避免「碳水」抢走「碳水化合物」）
MACRO_ALIASES = (
    ("protein", r"(?:蛋白质|蛋白)"),
    ("fat", r"(?:脂肪)"),
    ("carbohydrate", r"(?:碳水化合物|碳水)"),
)
MACRO_LABEL = {"protein": "蛋白质", "fat": "脂肪", "carbohydrate": "碳水"}

# 下限措辞 / 上限措辞；标记为 exclusive 的不含边界（与热量口径一致，PRD 规则 3）
MIN_OPS = r"(?:≥|>=|不低于|不少于|至少|超过|高于|多于)"
MAX_OPS = r"(?:≤|<=|不超过|不高于|不多于|最多|至多|低于|少于|小于)"
EXCLUSIVE_OPS = ("超过", "高于", "多于", "低于", "少于", "小于")
SUFFIX_MIN = r"(?:以上|及以上)"
SUFFIX_MAX = r"(?:以内|以下|之内|以里)"


def _grab_macro(text: str, word_re: str) -> dict | None:
    """抓「蛋白质30g以上 / 脂肪不超过20g / 碳水低于50g」这类限定。

    两种语序都认（操作符在前、量词后缀在后），方向和含不含边界都要判：
    蛋白质通常是下限、脂肪碳水通常是上限，但都按用户原文来，不预设。
    """
    m = re.search(
        rf"{word_re}\s*(?P<op>{MIN_OPS}|{MAX_OPS})?\s*(?P<v>\d+(?:\.\d+)?)\s*(?:g|G|克)",
        text,
    )
    if not m:
        return None

    op = (m.group("op") or "").strip()
    value = float(m.group("v"))
    tail = text[m.end(): m.end() + 3]

    if op:
        is_min = bool(re.fullmatch(MIN_OPS, op))
        inclusive = op not in EXCLUSIVE_OPS
    elif re.match(SUFFIX_MAX, tail):
        is_min, inclusive = False, True
    elif re.match(SUFFIX_MIN, tail):
        is_min, inclusive = True, True
    else:
        # 只写了数字（「蛋白质 30g」）：按“至少”理解，具体口径在标签里写明
        is_min, inclusive = True, True

    suffix = re.match(rf"(?:{SUFFIX_MIN}|{SUFFIX_MAX})?", tail).group(0)
    return {
        "value": value,
        "min": is_min,
        "inclusive": inclusive,
        "text": text[m.start(): m.end() + len(suffix)],
    }


def _grab_limit(text: str, unit_re: str, extra_patterns: tuple[str, ...] = ()) -> dict | None:
    """抓一个数值上限，并判断是否包含边界。

    语序两种都认：「不超过 1000 千卡」「1000 千卡以内」。
    只有带明确措辞的才判断含不含边界，裸数字按含边界处理（PRD 规则 3 只约束前者）。
    """
    patterns = (
        rf"(?P<w>{INCL_WORDS}|{EXCL_WORDS})\s*(?P<v>\d+(?:\.\d+)?)\s*{unit_re}",
        rf"(?P<v>\d+(?:\.\d+)?)\s*{unit_re}\s*(?P<p>以内|以下|之内|以里|内)",
        rf"(?P<v>\d+(?:\.\d+)?)\s*{unit_re}",
        *extra_patterns,  # 没有单位的写法放最后兜底（如「预算40」）
    )
    for pat in patterns:
        m = re.search(pat, text)
        if not m:
            continue
        word = m.groupdict().get("w") or ""
        if m.groupdict().get("p"):
            inclusive = True
        elif word:
            inclusive = bool(re.search(INCL_WORDS, word)) and not re.search(EXCL_WORDS, word)
        else:
            inclusive = True
        return {"value": float(m.group("v")), "inclusive": inclusive, "text": m.group(0)}
    return None


def parse_request(text: str, items: list[dict], mode: str) -> dict:
    """把自然语言拆成结构化条件。返回的每一条都带原文依据，便于前端展示核对。"""
    raw = (text or "").strip()
    t = raw.replace(" ", "")
    recognized: list[dict] = []
    unresolved: list[str] = []

    # --- 热量上限：产品口径已改为「不按热量筛选」，这里只提示、不作为条件 ---
    kcal_limit = None
    got = _grab_limit(t, KCAL_UNIT)
    if got:
        unresolved.append(f"「{got['text']}」未生效：配餐只按人数、预算与口味搭配，不按热量筛")

    # --- 预算上限 ---
    budget = None
    got = _grab_limit(t, MONEY_UNIT, extra_patterns=(rf"(?:预算|花费|总价)(?:是|大概|约)?\s*(?P<v>\d+(?:\.\d+)?)",))
    if got:
        budget = {"value": got["value"], "inclusive": got["inclusive"], "text": got["text"]}
        recognized.append(
            {
                "type": "budget",
                "value": budget["value"],
                "inclusive": budget["inclusive"],
                "label": f"预算 {budget['value']:g} 元（{'含' if budget['inclusive'] else '不含'}上限）",
                "source": got["text"],
            }
        )

    # --- 排除项：先抓「不要X」，再把 X 反查成具体商品 ---
    excluded: list[str] = []
    for m in re.finditer(r"(?:不要|不吃|别要|去掉|不想吃|排除)\s*([^，,。;；、\s]{2,10})", t):
        frag = _clean_name(m.group(1))
        if len(frag) < 2:
            continue
        hits = [it for it in items if frag in _clean_name(it["name"])]
        for it in hits:
            if it["code"] not in excluded:
                excluded.append(it["code"])
                recognized.append(
                    {"type": "exclude", "value": it["code"], "label": f"排除：{it['name']}", "source": m.group(0)}
                )
        if not hits:
            unresolved.append(f"「{m.group(1)}」在当前门店菜单里没找到，未作为排除项加入")

    # --- 必选项：文本里点名的商品 ---
    must: list[str] = []
    for it in items:
        if it["code"] in excluded:
            continue
        if _name_hit(it["name"], t):
            must.append(it["code"])
            recognized.append(
                {"type": "must", "value": it["code"], "label": f"必选：{it['name']}", "source": it["name"]}
            )

    # 同名前缀可能命中多个商品（如「巨无霸」命中普通装与套餐），保留全部但不重复提示
    must = list(dict.fromkeys(must))

    # --- 偏好 ---
    prefs: list[str] = []
    if re.search(r"(便宜|省钱|实惠|划算|低成本|少花钱)", t):
        prefs.append("cheap")
        recognized.append({"type": "pref", "value": "cheap", "label": "偏好：更便宜", "source": "便宜/省钱"})
    if re.search(r"(吃饱|管饱|够吃|顶饱|分量足|吃爽)", t):
        prefs.append("full")
        recognized.append({"type": "pref", "value": "full", "label": "偏好：尽量吃饱", "source": "吃饱/管饱"})

    # --- 数量约束 ---
    counts = {"main": 1, "drink": 1, "side": 0}
    # explicit_counts 只表示「用户自己说了几份」。说不喝咖啡不是份数约束，
    # 否则会挡掉「两个人」这类人数信息（实测：说「两个人…不要咖啡」只出 1 个汉堡）。
    explicit_counts = False
    drink_off = False
    for m in re.finditer(r"(\d+|[一两二三四五六七八九十])\s*(?:个|份|杯|块|只)?\s*(汉堡|堡|主餐|套餐|咖啡|饮品|饮料|小食|薯条|鸡翅|可乐)", t):
        n = _cn2int(m.group(1))
        if n <= 0:
            continue
        word = m.group(2)
        if word in ("汉堡", "堡", "主餐", "套餐"):
            counts["main"] = min(n, 3)
        elif word in ("咖啡", "饮品", "饮料", "可乐"):
            counts["drink"] = min(n, 3)
        elif word in ("小食", "薯条", "鸡翅"):
            counts["side"] = min(n, 3)
        explicit_counts = True
        recognized.append(
            {"type": "count", "value": {word: n}, "label": f"{word} ×{n}", "source": m.group(0)}
        )

    # 「不用饮料」这类只关掉饮品，不动主餐份数
    if re.search(r"(不要|不用|不加|别要)\s*(?:饮料|饮品|咖啡|喝的)", t):
        counts["drink"] = 0
        drink_off = True
        recognized.append({"type": "count", "value": {"drink": 0}, "label": "不需要饮品", "source": "不要饮料"})

    # 减脂模式：至少要有主餐，且默认配一杯黑咖啡（这是该模式的常见形态，明示给用户）
    if mode == "lean" and not explicit_counts and not drink_off:
        recognized.append(
            {
                "type": "default",
                "value": "lean-structure",
                "label": "默认搭配：主餐 1 + 咖啡 1（未指定结构时按此生成，可修改）",
                "source": "默认",
            }
        )

    # --- 三大营养素：同样不再作为筛选条件（健身向选项已从产品下线）---
    macro_limits: dict[str, dict] = {}
    for key, word_re in MACRO_ALIASES:
        got = _grab_macro(t, word_re)
        if not got:
            continue
        unresolved.append(f"「{got['text']}」未生效：配餐只按人数、预算与口味搭配，不按营养素筛")

    return {
        "text": raw,
        "kcalLimit": kcal_limit,
        "budget": budget,
        "macroLimits": macro_limits,
        "must": must,
        "exclude": excluded,
        "prefs": prefs,
        "counts": counts,
        "explicitCounts": explicit_counts,
        "drinkOff": drink_off,
        "recognized": recognized,
        "unresolved": unresolved,
    }


# ---------- 1.5 合并模型解析结果 ----------


def merge_llm(
    parsed: dict,
    llm_data: dict | None,
    items: list[dict],
    text: str = "",
) -> tuple[dict, list[str]]:
    """把模型解析出的条件合并进正则结果，返回 (parsed, 本次新增的条件说明)。

    合并规则：**正则优先，模型只补空**。正则在数字与边界语义上更可靠（「不超过」含上限、
    「低于」不含），模型的价值在于人数、口味、饮食偏好这些正则覆盖不到的表述。

    由模型带入的每一条都会写进 `recognized` 并标 `source=AI 解析`，用户看得见、能纠正。
    模型不产出任何价格/热量/营养素数值——那些仍由菜单与真实试算决定。

    `text` 是用户原话，用于给模型的必选关键词「接地」：模型不能替用户点名他没说过的商品
    （实测：模型把「一个汉堡一杯咖啡」脑补成 8 个必选套餐，每个方案都塞进一整排，直接超预算）。
    """
    if not llm_data:
        return parsed, []

    added: list[str] = []
    grouped = parsed.setdefault("recognized", [])

    def add(kind: str, label: str, value: Any = None) -> None:
        grouped.append({"type": kind, "value": value, "label": label, "source": "AI 解析"})
        added.append(label)

    # 预算：正则没抓到才用模型的
    budget = llm_data.get("budget")
    if parsed.get("budget") is None and budget:
        value = float(budget)
        parsed["budget"] = {"value": value, "inclusive": True, "text": "模型解析"}
        add("budget", f"预算 {value:g} 元（含上限）", value)

    # 份数：人数按「每人 1 份主餐 + 1 杯饮品」折算，这是产品的默认结构，不是数据推断。
    # 用户已说「不要咖啡」时只折算主餐，饮品保持 0——否则会把「两个人」直接吞掉。
    people = llm_data.get("people")
    if people:
        # 人数是「人均热量」换算的依据，即使份数由用户自己指定也要记下来
        parsed["people"] = min(max(int(people), 1), 5)
    if not parsed.get("explicitCounts"):
        counts = llm_data.get("counts") or {}
        drink_off = bool(parsed.get("drinkOff"))
        if people:
            n = parsed["people"]
            drink = 0 if drink_off else n
            parsed["counts"] = {"main": n, "drink": drink, "side": 0}
            parsed["explicitCounts"] = True
            # 只有「按人数折算」的份数才允许上浮：人均 1 个汉堡往往不够吃，
            # 由 generate 在预算/热量允许时多配 1~2 个。用户自己说了份数则不浮动。
            parsed["mainElastic"] = True
            label = f"{n} 人份：主餐 ×{n}" + ("、不含饮品" if drink == 0 else f"、饮品 ×{n}")
            add("count", label, {"main": n})
        elif any(counts.get(k) for k in ("main", "drink", "side")):
            base = dict(parsed.get("counts") or {})
            merged = {
                k: (min(int(counts[k]), 3) if counts.get(k) else int(base.get(k) or 0))
                for k in ("main", "drink", "side")
            }
            if drink_off:
                merged["drink"] = 0
            parsed["counts"] = merged
            parsed["explicitCounts"] = True
            label = "、".join(
                f"{name} ×{merged[key]}"
                for key, name in (("main", "主餐"), ("drink", "饮品"), ("side", "小食"))
                if merged[key]
            )
            add("count", label, merged)

    # 热量与营养素不再是筛选条件（产品口径：只按人数、预算、口味搭配）。
    # 模型即使返回了 kcalMax / macroMin 也在这里丢弃，避免悄悄加限制。

    # 关键词 → 商品编码（模型只给关键词，编码由这里按真实菜单落）
    def match(keyword: str) -> list[dict]:
        clean = _clean_name(keyword)
        if not clean:
            return []
        return [it for it in items if clean in _clean_name(it["name"])]

    excluded: set[str] = set(parsed.get("exclude") or [])
    for keyword in llm_data.get("excludeKeywords") or []:
        for it in match(keyword):
            if it["code"] not in excluded:
                excluded.add(it["code"])
                add("exclude", f"排除：{it['name']}（{keyword}）", it["code"])
    parsed["exclude"] = sorted(excluded)

    # mustKeywords 落地三重防护（挡住模型脑补必选商品）：
    # 1) 接地：关键词必须是用户原话里出现过的片段——模型不能替用户点名；
    # 2) 展开：全名命中才必选；只命中一件也算说清了；命中多件的是大类词
    #    （「汉堡」「咖啡」），当成必选会让每个方案都带上全部同名商品，
    #    降级为口味偏好（让这类组合排前面），份数仍按用户说的算；
    # 3) 同类必选总数不超过用户要的份数，见 generate()。
    user_text = _clean_name(text or "")
    must: list[str] = list(parsed.get("must") or [])
    deferred_prefs: list[str] = []
    for keyword in llm_data.get("mustKeywords") or []:
        clean = _clean_name(keyword)
        if not clean:
            continue
        if user_text and clean not in user_text:
            continue  # 用户没点名过的商品，模型不能替他加必选
        exact = [it for it in items if _clean_name(it["name"]) == clean]
        partial = [it for it in items if clean in _clean_name(it["name"])]
        if exact:
            hits = exact
        elif len(partial) == 1:
            hits = partial  # 只匹配到一件，也算用户说清楚了
        else:
            deferred_prefs.append(keyword)
            continue
        for it in hits:
            if it["code"] in excluded or it["code"] in must:
                continue
            must.append(it["code"])
            add("must", f"必选：{it['name']}", it["code"])
    parsed["must"] = must

    prefs: list[str] = list(parsed.get("prefs") or [])
    for pref in llm_data.get("prefs") or []:
        if pref in prefs:
            continue
        prefs.append(pref)
        add("pref", "偏好：更便宜" if pref == "cheap" else "偏好：尽量吃饱", pref)
    parsed["prefs"] = prefs

    prefer: list[str] = list(parsed.get("preferKeywords") or [])
    # 大类词型的 mustKeywords 降级到这里：不强制必选，但让含这类商品的组合排前面
    for keyword in deferred_prefs + (llm_data.get("preferKeywords") or []):
        if keyword not in prefer:
            prefer.append(keyword)
    if prefer:
        parsed["preferKeywords"] = prefer
        add("pref", "口味偏好：" + "、".join(prefer), prefer)

    summary = (llm_data.get("summary") or "").strip()
    if summary:
        parsed["aiSummary"] = summary
    return parsed, added


# ---------- 2. 组合生成 ----------


def _unit_kcal(item: dict, sauce_off: bool) -> float:
    """单份热量。菜单项里的 kcal.unit 已是该模式下的最终值（减脂汉堡已扣去酱）。"""
    unit = float((item.get("kcal") or {}).get("unit") or 0)
    auto_off = bool(item.get("autoSauceRemoved"))
    if sauce_off and not auto_off and item.get("hasSauces"):
        params = nutrition.load_estimate_params()
        unit = max(0.0, unit - float(params.get("sauce_kcal_per_serving") or 0))
    return unit


def _unit_price(item: dict) -> float:
    try:
        return float(item.get("price") or 0)
    except (TypeError, ValueError):
        return 0.0


# 每克营养素的上限产能（蛋白/碳水 4 kcal，脂肪 9 kcal）。
# 用它把「只有热量、没有营养素」的商品换算成一个严格上界，而不是当 0 或瞎填。
KCAL_PER_GRAM = {"protein": 4.0, "fat": 9.0, "carbohydrate": 4.0}


def _macro_bounds(rows: list[dict]) -> tuple[dict, dict, bool, bool]:
    """返回 (下界, 上界, 是否全部有官方营养素, 是否有商品连热量都没有)。

    有官方值的商品：上下界相同，都是精确值。
    缺官方值的商品：
      - 下界按 0（不知道就是不知道，不能往上加）；
      - 上界按热量反推——1g 脂肪最多提供 9 kcal，所以脂肪 ≤ 热量/9；蛋白、碳水各自 ≤ 热量/4。
        这是能量守恒给出的硬上界，不是估算值，也不需要编数据。
      - 若连热量都没有，上界推不出来，这条候选只能算「无法确认」。
    因此：下限条件用下界判「确定达标」，上限条件用上界判「确定不超标」。
    去酱只扣热量不扣营养素，属于保守方向（营养素偏高），不影响上界的用法。
    """
    low = {key: 0.0 for key in KCAL_PER_GRAM}
    high = {key: 0.0 for key in KCAL_PER_GRAM}
    complete = True
    unknown_energy = False
    for row in rows:
        quantity = int(row.get("quantity") or 1)
        macros = row.get("macros")
        if macros:
            for key in low:
                value = float(macros.get(key) or 0) * quantity
                low[key] += value
                high[key] += value
            continue
        complete = False
        kcal = float(row.get("kcal") or 0) * quantity
        if kcal <= 0:
            unknown_energy = True
        for key, per_gram in KCAL_PER_GRAM.items():
            high[key] += kcal / per_gram
    return low, high, complete, unknown_energy


def _pool_by_kind(items: list[dict], mode: str) -> dict[str, list[dict]]:
    pool: dict[str, list[dict]] = {}
    for it in items:
        kind = (it.get("decision") or {}).get("kind") or "other"
        if mode == "lean" and kind not in ("burger", "coffee"):
            continue  # 减脂模式只用菜单已过滤的结果，这里再兜一层
        pool.setdefault(kind, []).append(it)
    return pool


def generate(
    items: list[dict],
    parsed: dict,
    *,
    mode: str,
    skip: list[list[str]] | None = None,
    locked: list[dict] | None = None,
) -> dict:
    """生成候选组合（未试算）。

    locked 只用于「购物车里已有的商品不再重复推荐」，**不参与**方案的热量与金额累加：
    配餐展示的价格和卡路里只反映方案自身，避免用户把方案数值读成整车数值。
    """
    skip_sets = {frozenset(s) for s in (skip or [])}
    locked = locked or []
    locked_codes = {c["code"] for c in locked}

    pool = _pool_by_kind(items, mode)
    by_code = {it["code"]: it for it in items}
    excluded = set(parsed["exclude"]) | locked_codes  # 已在购物车里的不再推荐一遍
    must_codes = [c for c in parsed["must"] if c not in excluded]
    counts = parsed["counts"]

    mains = [it for k in MAIN_KINDS for it in pool.get(k, []) if it["code"] not in excluded]
    drinks = [it for k in DRINK_KINDS for it in pool.get(k, []) if it["code"] not in excluded]
    sides = [it for k in SIDE_KINDS for it in pool.get(k, []) if it["code"] not in excluded]

    # 必选项如果本身是主餐/饮品，先从对应池子里拿掉，避免重复计数。
    # 先按份数裁剪（must_items 定稿）再动池子：被砍掉的项要回到池子里当普通候选，
    # 否则「要一个汉堡」遇到两个必选时，砍掉一个就连普通汉堡也没得选了。
    must_items = [by_code[c] for c in must_codes if c in by_code]

    n_main = max(0, int(counts.get("main") or 0))
    n_drink = max(0, int(counts.get("drink") or 0))
    n_side = max(0, int(counts.get("side") or 0))
    # 兜底：同类必选可能比用户要的份数还多（模型脑补或同名前缀命中多个套餐）。
    # 必选是「占掉用户要的份数」，不是「再加量」——超出的部分砍掉，否则每个方案
    # 都会被塞成一整排同名商品（实测：「一个汉堡一杯咖啡」被脑补成 8 个必选）。
    # 用户没要这类时（份数为 0）不砍：点名就要，几个都认。
    _kind_slot = {"main": MAIN_KINDS, "drink": DRINK_KINDS, "side": SIDE_KINDS}
    _caps = {"main": n_main, "drink": n_drink, "side": n_side}
    _used = {"main": 0, "drink": 0, "side": 0}
    _kept: list[dict] = []
    for it in must_items:
        _kind = (it.get("decision") or {}).get("kind")
        _slot = next((s for s, kinds in _kind_slot.items() if _kind in kinds), None)
        if _slot and _caps[_slot] > 0:
            if _used[_slot] >= _caps[_slot]:
                continue
            _used[_slot] += 1
        _kept.append(it)
    must_items = _kept

    must_codes_set = {it["code"] for it in must_items}
    mains = [it for it in mains if it["code"] not in must_codes_set]
    drinks = [it for it in drinks if it["code"] not in must_codes_set]
    sides = [it for it in sides if it["code"] not in must_codes_set]
    # 必选项已经占掉了主餐/饮品的位置
    buy_main = max(0, n_main - len([it for it in must_items if (it.get("decision") or {}).get("kind") in MAIN_KINDS]))
    buy_drink = max(0, n_drink - len([it for it in must_items if (it.get("decision") or {}).get("kind") in DRINK_KINDS]))

    # 人均 1 个汉堡常常不够吃（用户实测反馈：两个人各配 1 个太少了）。
    # 只有在「份数由人数折算」时才允许上浮 1~2 个，多出来的部分由热量与预算自然裁剪；
    # 用户自己说了「两个汉堡」就严格照做，不擅自加量。
    if parsed.get("mainElastic") and buy_main:
        main_counts = [buy_main + i for i in range(0, 3)]
    else:
        main_counts = [buy_main]
    main_groups: list[list[dict]] = []
    for k in main_counts:
        # 每个份数各自限量，避免 4 个的笛卡尔积把 2、3 个的候选挤出上限
        main_groups.extend(_combo_pool(mains, k, max_out=200))

    drink_groups = _combo_pool(drinks, buy_drink)
    side_groups = _combo_pool(sides, n_side) if n_side else [[]]

    # 默认结构（用户没提数量）遇到门店没有该类候选时，降级为不配这一类并说明；
    # 用户明确要求过数量的，不擅自降级，直接报阻碍（PRD 规则 10）。
    downgrades: list[str] = []
    if buy_main and not main_groups:
        if parsed["explicitCounts"]:
            return {
                "plans": [],
                "blocker": {"code": "no-main", "message": "你要求了主餐，但当前门店在%s下没有可用的主餐候选" % ("减脂模式" if mode == "lean" else "全部餐品")},
            }
        buy_main, main_groups = 0, [[]]
        downgrades.append("门店没有主餐候选，本次只配了饮品")
    if buy_drink and not drink_groups:
        if parsed["explicitCounts"]:
            return {
                "plans": [],
                "blocker": {"code": "no-drink", "message": "你要求了饮品，但当前门店没有可用的饮品候选"},
            }
        buy_drink, drink_groups = 0, [[]]
        downgrades.append(
            "本店没有符合减脂口径的黑咖啡，本次只配了主餐"
            if mode == "lean"
            else "门店没有饮品候选，本次只配了主餐"
        )

    # V1.20 起热量与营养素不再作为筛选条件（产品口径：只按人数、预算与口味搭配），
    # 方案自身的热量与营养素仍照实计算，用于方案卡展示。
    budget = parsed["budget"]
    prefs = parsed["prefs"]

    all_candidates: list[dict] = []
    skipped = 0  # 被「换一套」排除的组合数（只数估算价没过预算线的，用于「看完了」提示）
    for mg, dg, sg in itertools.product(main_groups, drink_groups, side_groups):
        chosen = list(must_items) + list(mg) + list(dg) + list(sg)
        if not chosen:
            continue
        codes = [it["code"] for it in chosen] + [c["code"] for c in locked]
        # 预算先用估算价粗筛（留 15% 弹性，券后价可能更低）；换一套排除要在粗筛之后数，
        # 否则「符合条件的 N 组都看过了」会把超预算的组合也数进去。
        # 这里只算方案自身，不含购物车锁定项（配餐口径：价格与热量只算方案自身）。
        raw_price = sum(_unit_price(by_code[it["code"]]) for it in chosen)
        over_budget = budget is not None and raw_price > float(budget["value"]) * 1.15
        if frozenset(codes) in skip_sets:
            if not over_budget:
                skipped += 1
            continue
        # 同款出现多次要合并成数量，不能当成重复项丢掉
        qty_map: dict[str, int] = {}
        for it in chosen:
            qty_map[it["code"]] = qty_map.get(it["code"], 0) + 1

        rows = []
        for code, qty in qty_map.items():
            it = by_code[code]
            sauce_off = mode == "lean" and (it.get("decision") or {}).get("kind") == "burger" and bool(it.get("hasSauces"))
            rows.append(
                {
                    "code": it["code"],
                    "name": it["name"],
                    "price": _unit_price(it),
                    "quantity": qty,
                    "kcal": _unit_kcal(it, sauce_off),
                    "kcalSource": (it.get("kcal") or {}).get("source") or "unknown",
                    # 三大营养素只认官方同名值，缺了就是 None（不估算）
                    "macros": it.get("macros"),
                    "sauceOff": sauce_off,
                    "kind": (it.get("decision") or {}).get("kind") or "other",
                    "locked": False,
                }
            )

        # 配餐口径：方案自身的热量与金额独立计算，不叠加购物车已有商品
        raw_kcal = sum(r["kcal"] * r["quantity"] for r in rows)

        macro_low, _macro_high, macro_complete, _macro_unknown = _macro_bounds(rows)

        all_candidates.append(
            {
                "rows": rows,
                "rawKcal": raw_kcal,
                "rawPrice": raw_price,
                "count": sum(r["quantity"] for r in rows),
                "overBudget": over_budget,
                "macros": macro_low,
                "macroIncomplete": not macro_complete,
            }
        )
        if len(all_candidates) >= MAX_CANDIDATES:
            break

    passed = [c for c in all_candidates if not c["overBudget"]]

    # 口味偏好（如「想吃牛肉」「喜欢鸡肉」）：命中的方案排前面，
    # 但不越过预算这个硬条件——偏好只调整顺序，不放宽筛选。
    prefer = parsed.get("preferKeywords") or []

    def prefer_rank(cand: dict) -> int:
        if not prefer:
            return 0
        hits = sum(1 for r in cand["rows"] if any(k in _clean_name(r["name"]) for k in prefer))
        return -hits

    # 排序：口味偏好 → 用户偏好 → 估算价 → 热量 → 件数（PRD 规则 5 的并列取舍）
    if "full" in prefs:
        passed.sort(key=lambda c: (prefer_rank(c), -c["rawKcal"], c["rawPrice"], c["count"]))
    else:
        passed.sort(key=lambda c: (prefer_rank(c), c["rawPrice"], c["rawKcal"], c["count"]))

    if not passed:
        return {
            "plans": [],
            # 用真实存在过的候选说明阻碍，避免"最轻单品"式的错解释
            "cheapest": min(all_candidates, key=lambda c: (c["rawPrice"], c["rawKcal"])) if all_candidates else None,
            # 供上层多试几组真实价格：列表价不可信，只试「列表价最低」会得出错误结论
            "candidates": all_candidates,
            "totalCandidates": len(all_candidates),
            "downgrades": downgrades,
            # 候选被「换一套」全部排除时为 0，skipped 记着看过几组，用于「看完了」提示
            "skipped": skipped,
        }

    return {
        "plans": passed,
        "totalCandidates": len(passed),
        "downgrades": downgrades,
        "skipped": skipped,
    }


def _combo_pool(pool: list[dict], n: int, max_out: int = MAX_CANDIDATES) -> list[list[dict]]:
    """按份数取组合：n=0 → 空；n=1 → 每个单品；n≥2 → 可重复组合（同款 ×2 也算）。

    大份数（3、4 个汉堡）的组合数会爆炸，必须截断；但**不能按菜单原顺序截断**：
    门店列表里前几个往往是芝士安格斯（696 千卡）、四件套（750 兜底）这类高热量的，
    截断后 4 个的组合全在 2200 千卡以上。所以先按热量升序排，让清淡实惠的组合
    优先进入候选（V1.20 起热量不再筛选，升序只影响截断时谁进候选）。
    """
    if n <= 0:
        return [[]]
    if n == 1:
        return [[it] for it in pool]
    ordered = sorted(pool, key=lambda it: float((it.get("kcal") or {}).get("unit") or 0))
    out = []
    for combo in itertools.combinations_with_replacement(ordered, n):
        out.append(list(combo))
        if len(out) >= max_out:
            break
    return out


def _main_key(cand: dict) -> str:
    """候选里的主餐编码，用来判断「换了一道不同的主食」。"""
    return next((r["code"] for r in cand["rows"] if r["kind"] in MAIN_KINDS), "")


def _trial_order(candidates: list[dict], limit: int) -> list[dict]:
    """挑出要送去试算的候选，优先让不同主餐都有机会。

    为什么不能直接取排序后的前几个：菜单列表价偶尔并不可信（燕麦奶铁列表
    ¥9.9 实际试算 ¥27、板烧四件套列表 ¥28 实际 ¥87），一旦用户说「要吃饱」，
    排序会按热量降序把一堆 750 兜底的套餐顶到最前，试算后全部超预算，
    真正的便宜组合（如牛肉堡单品精选 + 鲜萃咖啡 ¥25.00）根本轮不到试算，
    结果是「明明有解却报无解」。

    为什么不能按饮品分组各取 N 个：多数门店的可选饮品就一两种，分组后每个
    分组只取 2 个，等于把整个候选池压缩成 2 个——曾出现「14 组都符合条件却
    只出 2 个方案」。

    做法：按**主餐**分组，每组先取列表价最便宜的 1 个（保证三条方案不会是
    同一个汉堡换三种咖啡），名额没满时再按列表价升序补齐（主餐种类少的门店
    这时才靠饮品发散）。
    """
    groups: dict[str, list[dict]] = {}
    for c in candidates:
        groups.setdefault(_main_key(c), []).append(c)
    for lst in groups.values():
        lst.sort(key=lambda c: (c["rawPrice"], c["rawKcal"]))

    out: list[dict] = []
    seen: set[tuple] = set()

    def take(c: dict) -> None:
        # 去重必须带数量：同款「×1」与「×2」是不同的方案，
        # 只按商品编码去重会把 3 个、4 个汉堡的组合当成 2 个的重复项丢掉。
        sig = tuple(sorted((r["code"], int(r.get("quantity") or 1)) for r in c["rows"]))
        if sig in seen:
            return
        seen.add(sig)
        out.append(c)

    # 0) 先按「总件数」分组，每组取列表价最低的两个：让「2 个汉堡」「3 个」「4 个」
    #    都有机会被试算，且不止一个名额（否则补位阶段会被便宜的 2 个汉堡占满）。
    #    份数没有弹性时这里只有一组，等于不做额外挑选。
    by_count: dict[int, list[dict]] = {}
    for c in candidates:
        n_items = c.get("count") or sum(int(r.get("quantity") or 1) for r in c["rows"])
        by_count.setdefault(n_items, []).append(c)
    for k in sorted(by_count):
        cheapest = sorted(by_count[k], key=lambda c: (c["rawPrice"], c["rawKcal"]))[:2]
        for c in cheapest:
            if len(out) >= limit:
                break
            take(c)

    # 1) 每种主餐各占 1 个名额，按该主餐最便宜的列表价升序
    for key in sorted(groups, key=lambda k: (groups[k][0]["rawPrice"], groups[k][0]["rawKcal"])):
        if len(out) >= limit:
            break
        take(groups[key][0])

    # 2) 剩余名额按列表价升序填满（主餐种类少时，这一段补充不同的饮品搭配）
    for c in sorted(candidates, key=lambda c: (c["rawPrice"], c["rawKcal"])):
        if len(out) >= limit:
            break
        take(c)
    return out


def _rows_text(rows: list[dict]) -> str:
    """只描述本次新增的餐品，不提「已选」，避免与购物车已有商品混为一谈。"""
    return " + ".join("%s×%d" % (r["name"], r["quantity"]) for r in rows)


def _macros_text(macros: dict | None, incomplete: bool = False) -> str:
    """给界面用的短文案，如「蛋白 32g · 脂肪 30g · 碳水 77g」。

    有商品缺官方数据时，缺失部分按 0 计入，得到的是下界，所以加「≥」——
    不能把「只统计了几件」的数字说成整餐精确值。
    """
    if not macros:
        return ""
    values = {key: round(float(macros.get(key) or 0), 1) for key in MACRO_LABEL}
    if not any(values.values()):
        # 一件都没有官方营养素时，下界全是 0，写「蛋白 ≥0g」只会像故障；留空更诚实
        return ""
    sign = "≥" if incomplete else ""
    return "蛋白 %s%gg · 脂肪 %s%gg · 碳水 %s%gg" % (
        sign,
        values["protein"],
        sign,
        values["fat"],
        sign,
        values["carbohydrate"],
    )


def _explain_blocker(
    items: list[dict],
    parsed: dict,
    mode: str,
    *,
    cheapest: dict | None = None,
    real_yuan: float | None = None,
) -> dict:
    """无可行组合时说清主要阻碍，不擅自放宽（PRD 规则 10、场景六）。

    只用真实生成过的候选说话：之前拿"单个最轻商品"当解释，会出现
    「最低 10.50 元，超出 5 元预算」这种自相矛盾的文案。
    数字口径与方案一致——只讲方案自身，不叠加购物车已有商品。

    V1.20 起热量与营养素不再作为筛选条件，阻碍只剩预算与搭配两类。
    """
    budget = parsed["budget"]
    usable = [it for it in items if (it.get("decision") or {}).get("allowed") is not False]
    if not usable:
        return {"code": "no-items", "message": "当前门店在该模式下没有可选餐品，请切换模式或换门店", "suggestions": []}

    parts: list[str] = []
    budget_blocked = budget is not None and cheapest is not None
    if budget_blocked:
        limit = float(budget["value"])
        total_yuan = real_yuan if real_yuan is not None else cheapest["rawPrice"]
        if real_yuan is not None:
            parts.append(
                "最便宜的一组「%s」试算应付 %.2f 元，超出 %g 元预算"
                % (_rows_text(cheapest["rows"]), total_yuan, limit)
            )
        else:
            parts.append(
                "最便宜的一组「%s」估算 %.2f 元，超出 %g 元预算"
                % (_rows_text(cheapest["rows"]), total_yuan, limit)
            )

    if not parts:
        parts.append("按当前条件没有找到同时满足的组合")

    # 建议只给真正构成阻碍的放宽方向
    suggestions = [
        "把预算放宽到 %.2f 元以上" % (real_yuan if real_yuan is not None else (cheapest or {}).get("rawPrice", 0))
        if budget_blocked
        else None,
        "减少必选餐品或去掉排除项",
        "切换普通模式（减脂模式只保留汉堡与黑咖啡）" if mode == "lean" else None,
    ]
    return {"code": "no-match", "message": "；".join(parts), "suggestions": [s for s in suggestions if s]}


# ---------- 3. 试算与装配 ----------


def build_plans(
    app: Any,
    *,
    store_code: str,
    mode: str,
    text: str,
    cart: list[dict] | None = None,
    skip: list[list[str]] | None = None,
    need_tableware: bool = True,
    use_llm: bool = True,
) -> dict:
    """对外入口：解析（正则 + 可选模型）→ 生成 → 逐个真实试算 → 输出最多 6 个方案（条件宽松时多给几个，条件紧时 3 个）。

    模型只负责把口语翻成条件，翻出来的每一条都会回显给用户；价格与热量仍由
    菜单和真实试算决定。模型不可用（未配置 key / 超时 / 报错）时自动回退正则，
    不阻塞配餐。
    """
    menu = app.menu(store_code, mode)
    items = menu["items"]
    parsed = parse_request(text, items, mode)

    ai_info: dict[str, Any] = {"used": False, "model": None, "summary": "", "error": None}
    if use_llm and (text or "").strip():
        raw_llm, llm_error = llm.parse_request(text, items)
        if raw_llm:
            parsed, _added = merge_llm(parsed, llm.normalize(raw_llm), items, text)
            ai_info.update(
                {
                    "used": True,
                    "model": os.environ.get("MINIMAX_MODEL", llm.DEFAULT_MODEL),
                    "summary": parsed.get("aiSummary") or "",
                }
            )
        elif llm_error:
            ai_info["error"] = llm_error

    locked = []
    for row in cart or []:
        code = str(row.get("code") or "")
        qty = max(1, int(row.get("quantity") or 1))
        match = next((it for it in items if it["code"] == code), None)
        if not match:
            continue
        sauce_off = bool(row.get("sauceOff"))
        kcal_obj = match.get("kcal") or {}
        locked.append(
            {
                "code": code,
                "name": match["name"],
                "price": _unit_price(match),
                "quantity": qty,
                "kcal": _unit_kcal(match, sauce_off),
                "sauceOff": sauce_off,
                "kind": (match.get("decision") or {}).get("kind") or "other",
                "locked": True,
                # 套餐/无官方数据的商品用的是兜底值，不能让它冒充精确热量
                "estimated": kcal_obj.get("source") != "official",
            }
        )
    # 仅用于提示「购物车已有几件」，不参与方案的金额与热量计算
    locked_kcal = sum(r["kcal"] * r["quantity"] for r in locked)
    locked_price = sum(r["price"] * r["quantity"] for r in locked)
    budget = parsed["budget"]

    gen = generate(items, parsed, mode=mode, skip=skip, locked=locked)
    if not gen.get("plans"):
        # 「换一套」把候选全部看过的场景：不是没解，是看完了。说清楚，别让人误改条件
        # （曾显示「没有找到符合条件的方案」，用户以为条件太严，其实只是看遍了）。
        if skip and not gen.get("candidates") and int(gen.get("skipped") or 0) > 0:
            notes = list(gen.get("downgrades") or []) + list(parsed["unresolved"])
            return {
                "ok": True,
                "mode": mode,
                "recognized": parsed["recognized"],
                "unresolved": parsed["unresolved"],
                "cartItems": locked,
                "cartKcal": round(locked_kcal),
                "cartPrice": round(locked_price, 2),
                "cartEstimated": sum(1 for r in locked if r.get("estimated")),
                "plans": [],
                "blocker": {
                    "code": "exhausted",
                    "message": "符合条件的 %d 组搭配都已经看过了，换个条件再试，或重新生成" % int(gen["skipped"]),
                    "suggestions": ["点「修改」调整已识别条件", "重新生成，从头再看一遍"],
                },
                "note": "；".join(notes) if notes else None,
                "estimatedCandidates": 0,
                "ai": ai_info,
            }
        # 无可行组合时，把几组候选真算一遍，用实价解释阻碍（不拿列表价/估算价当结论）。
        # 不能只试「列表价最低」那一组：列表价不可信（燕麦奶铁 ¥9.9 实际 ¥27），
        # 只试一组会得出「最便宜也要 41.50 元」，而实际另有 25.00 元的组合。
        cheapest = gen.get("cheapest")
        real_yuan = None
        probes = _trial_order(gen.get("candidates") or [], BLOCKER_TRIALS)
        trials: list[tuple[dict, float]] = []
        for cand in probes:
            # 配餐独立计价：只试算方案自身，不把购物车已有商品并进去
            probe = [{"productCode": r["code"], "quantity": r["quantity"]} for r in cand["rows"]]
            try:
                res = coupons.select_best(
                    app.require_client(),
                    store_code=store_code,
                    items=probe,
                    need_tableware=need_tableware,
                    budget=TRIAL_BUDGET,
                )
                cents = res.get("cents")
                if isinstance(cents, int):
                    trials.append((cand, round(cents / 100, 2)))
            except Exception:  # noqa: BLE001 - 探价失败就跳过这组
                continue
        if trials:
            cheapest, real_yuan = min(trials, key=lambda x: x[1])
        blocker = _explain_blocker(
            items,
            parsed,
            mode,
            cheapest=cheapest,
            real_yuan=real_yuan,
        )
        notes = list(gen.get("downgrades") or []) + list(parsed["unresolved"])
        return {
            "ok": True,
            "mode": mode,
            "recognized": parsed["recognized"],
            "unresolved": parsed["unresolved"],
            "cartItems": locked,
            "cartKcal": round(locked_kcal),
            "cartPrice": round(locked_price, 2),
            "cartEstimated": sum(1 for r in locked if r.get("estimated")),
            "plans": [],
            "blocker": blocker,
            "note": "；".join(notes) if notes else None,
            "estimatedCandidates": gen.get("totalCandidates"),
            "ai": ai_info,
        }

    plans: list[dict] = []
    trial_notes: list[str] = []
    failed_trials: list[tuple[dict, float]] = []
    seen_signatures: set[tuple] = set()

    # 条件宽松（候选多）时多给几个方案；条件紧时保持 3 个，不做无谓的试算
    plan_cap = MAX_PLANS if (gen.get("totalCandidates") or 0) >= WIDE_CANDIDATE_THRESHOLD else NARROW_PLANS
    for cand in _trial_order(gen["plans"], MAX_TRIAL_CANDIDATES):
        if len(plans) >= plan_cap:
            break
        # 同款不同份数（×1 与 ×2）是两个方案，去重必须带数量
        signature = tuple(sorted((r["code"], int(r.get("quantity") or 1)) for r in cand["rows"]))
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)

        # 配餐独立计价：只把方案自身的餐品送去试算，不并入购物车已有商品
        price_items = [{"productCode": r["code"], "quantity": r["quantity"]} for r in cand["rows"]]
        # 减脂汉堡去酱要作为定制提交（PRD 已实测支持）
        for idx, row in enumerate(cand["rows"]):
            if row["sauceOff"]:
                try:
                    fact = app.facts_for(store_code, row["code"])
                except Exception:  # noqa: BLE001 - 详情失败不该让整个配餐失败
                    continue
                values = []
                for v in fact.get("values") or []:
                    if not v.get("unselectedKey"):
                        continue
                    off = "酱" in (v.get("name") or "")
                    values.append(
                        {
                            "code": v["code"],
                            "key": v["unselectedKey"] if off else v.get("selectedKey"),
                            "quantity": 0 if off else (v.get("selectedQuantity") or 1),
                        }
                    )
                if values:
                    price_items[idx]["modification"] = {"values": values}

        try:
            result = coupons.select_best(
                app.require_client(),
                store_code=store_code,
                items=price_items,
                need_tableware=need_tableware,
                budget=TRIAL_BUDGET,
            )
        except Exception as exc:  # noqa: BLE001 - 试算失败要保留其他方案并说明
            trial_notes.append(f"有一组方案试算失败：{exc}")
            continue

        cents = result.get("cents")
        if not isinstance(cents, int):
            trial_notes.append("有一组方案未拿到有效价格，已跳过")
            continue
        yuan = round(cents / 100, 2)
        if result.get("note"):
            trial_notes.append(result["note"])

        if budget is not None:
            limit = float(budget["value"])
            if (budget["inclusive"] and yuan > limit) or (not budget["inclusive"] and yuan >= limit):
                failed_trials.append((cand, yuan))  # 券后仍超预算，不展示；留作阻碍说明的依据
                continue

        # 方案自身热量，不含购物车
        total_kcal = sum(r["kcal"] * r["quantity"] for r in cand["rows"])
        plans.append(
            {
                "id": len(plans) + 1,
                "items": cand["rows"],
                "kcal": round(total_kcal),
                "kcalText": f"约 {round(total_kcal)} 千卡",
                # 三大营养素：缺数据的商品按 0 计入，所以是**下界**（部分商品无官方数据时标 ≥）
                "macros": cand.get("macros"),
                "macrosIncomplete": bool(cand.get("macroIncomplete")),
                "macrosText": _macros_text(cand.get("macros"), bool(cand.get("macroIncomplete"))),
                "cents": cents,
                "yuan": yuan,
                "coupon": result.get("couponTitle"),
                "couponNote": result.get("note"),
                "reason": _reason(cand["rows"], parsed, mode, result.get("couponTitle")),
                "signature": [r["code"] for r in cand["rows"]],
                # 有兜底估算项时明说，别让估算值看起来像官方数据（PRD US-02）
                "hasEstimate": any(r.get("kcalSource") in ("heuristic", "unknown") for r in cand["rows"]),
            }
        )

    # 券后价重排（PRD 规则 5：金额优先，其次热量，再次件数）
    if "full" not in parsed["prefs"]:
        plans.sort(key=lambda p: (p["yuan"], p["kcal"], len(p["items"])))
    else:
        plans.sort(key=lambda p: (-p["kcal"], p["yuan"], len(p["items"])))
    for i, p in enumerate(plans, 1):
        p["id"] = i

    blocker = None
    if not plans:
        if failed_trials:
            # 有候选但券后仍不达标：用真实试算价中最便宜的一组解释，避免猜测
            cand, yuan = min(failed_trials, key=lambda x: x[1])
            blocker = _explain_blocker(
                items, parsed, mode, cheapest=cand, real_yuan=yuan
            )
            blocker["message"] = "找到 %d 组搭配，但%s，因此没有可推荐方案" % (
                gen.get("totalCandidates") or len(gen["plans"]),
                blocker.get("message") or "优惠后仍不满足条件",
            )
        else:
            blocker = _explain_blocker(
                items, parsed, mode, cheapest=gen.get("cheapest")
            )

    notes = []
    if gen.get("downgrades"):
        notes.extend(gen["downgrades"])
    if trial_notes:
        notes.extend(dict.fromkeys(trial_notes))
    if parsed["unresolved"]:
        notes.extend(parsed["unresolved"])

    return {
        "ok": True,
        "mode": mode,
        "recognized": parsed["recognized"],
        "unresolved": parsed["unresolved"],
        "macroLimits": parsed.get("macroLimits") or {},
        "cartItems": locked,
        "cartKcal": round(locked_kcal),
        "cartPrice": round(locked_price, 2),
        "cartEstimated": sum(1 for r in locked if r.get("estimated")),
        "plans": plans,
        "blocker": blocker,
        "note": "；".join(notes) if notes else None,
        "estimatedCandidates": gen.get("totalCandidates"),
        "ai": ai_info,
    }


def _reason(rows: list[dict], parsed: dict, mode: str, coupon_title: str | None) -> str:
    bits = []
    if parsed["must"]:
        bits.append("含必选餐品")
    if "cheap" in parsed["prefs"]:
        bits.append("按更便宜优先")
    elif "full" in parsed["prefs"]:
        bits.append("在热量上限内尽量吃饱")
    if coupon_title:
        bits.append(f"已用券：{coupon_title}")
    burgers = [r for r in rows if r["kind"] == "burger" and r["sauceOff"]]
    if burgers:
        bits.append(f"{len(burgers)} 个汉堡已去酱")
    if not bits:
        bits.append("%s内搭配" % ("减脂" if mode == "lean" else "当前门店"))
    return "；".join(bits)
