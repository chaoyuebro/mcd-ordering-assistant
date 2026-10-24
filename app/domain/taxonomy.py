"""减脂范围判定（PRD §1.4.5 三层：分类归一 → 组成校验 → 商品清单兜底）。

判定必须是确定性的、可解释的：每个结论都带 reason，供 H5/Skill 展示
“为什么这个商品在减脂模式下不可选”。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import Taxonomy, load_taxonomy

BURGER = "burger"
COFFEE = "coffee"
OTHER = "other"
COMBO_HINTS = ("套餐", "三件套", "四件套", "双人餐", "单人餐", "开心乐园餐")


def normalize_category(name: str) -> str:
    """删除换行与空白后作为匹配键（实测分类名带 \\n，如 '鸡肉汉堡\\n/卷'）。"""
    return re.sub(r"[\r\n\s]+", "", name or "")


@dataclass
class Decision:
    allowed: bool | None  # True 可加购 / False 不可 / None 信息不足
    kind: str             # burger / coffee / other
    reason: str
    source: str           # override / category / fallback / unknown
    black_coffee: bool = False
    is_combo: bool = False
    details: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "kind": self.kind,
            "reason": self.reason,
            "source": self.source,
            "blackCoffee": self.black_coffee,
            "isCombo": self.is_combo,
        }


def _looks_like_combo(name: str) -> bool:
    return any(hint in (name or "") for hint in COMBO_HINTS)


def classify(
    product_code: str,
    name: str,
    categories: list[str],
    facts: dict | None = None,
    taxonomy: Taxonomy | None = None,
) -> Decision:
    """facts: {"rounds": int, "supportModify": bool, ...}，来自 query-meal-detail。"""
    tax = taxonomy or load_taxonomy()
    code = str(product_code)

    kinds = set()
    for raw in categories:
        kind = tax.category_map.get(normalize_category(raw))
        if kind:
            kinds.add(kind)

    override = tax.overrides.get(code)
    if override:
        kind = override.get("category") or OTHER
        source = "override"
        black = bool(override.get("black_coffee"))
        reason_prefix = "清单例外覆盖"
    else:
        black = False
        source = "category"
        reason_prefix = "分类归一"
        if BURGER in kinds:
            kind = BURGER
        elif COFFEE in kinds:
            kind = COFFEE
        else:
            kind = OTHER

    rounds = None if facts is None else facts.get("rounds")
    is_combo = bool(rounds) or (rounds is None and _looks_like_combo(name))

    # 第三层：清单未命中且分类也没有归一时，排除而非放行
    if kind == OTHER:
        return Decision(
            allowed=False,
            kind=OTHER,
            reason=f"{reason_prefix}为其他类目，减脂模式不展示",
            source=source,
        )

    # 咖啡：只有被清单标注为黑咖啡才允许，无需查详情即可定论
    if kind == COFFEE:
        if black:
            return Decision(True, COFFEE, "清单标注为黑咖啡，可加入减脂", source, black_coffee=True)
        return Decision(
            False,
            COFFEE,
            "分类只能证明是咖啡，未标注为黑咖啡（如含奶、含甜品），减脂不放行",
            source,
        )

    # 第二层：汉堡是否可加购取决于组成，必须有详情
    if rounds is None:
        return Decision(
            allowed=None,
            kind=kind,
            reason="尚未获取商品详情，无法确认是否为套餐或含不合规子项",
            source=source,
            black_coffee=black,
            is_combo=_looks_like_combo(name),
        )
    if rounds > 0:
        return Decision(
            allowed=False,
            kind=kind,
            reason=f"含 {rounds} 段套餐组成（含饮品或小食），需按成员判定，整份不可加入减脂",
            source=source,
            black_coffee=black,
            is_combo=True,
        )

    # 汉堡单品
    return Decision(True, BURGER, "单品汉堡，无套餐组成，可加入减脂", source)


def classify_menu(
    items: list[dict],
    facts_by_code: dict[str, dict] | None = None,
    taxonomy: Taxonomy | None = None,
) -> list[dict]:
    """批量判定，返回每项附带 decision 的列表。items: {code,name,categories,...}"""
    tax = taxonomy or load_taxonomy()
    facts_by_code = facts_by_code or {}
    out = []
    for item in items:
        decision = classify(
            item.get("code") or item.get("productCode"),
            item.get("name", ""),
            item.get("categories") or [],
            facts_by_code.get(str(item.get("code") or item.get("productCode"))),
            tax,
        )
        out.append({**item, "decision": decision.as_dict()})
    return out
