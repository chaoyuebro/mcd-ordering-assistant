"""离线自测：用真实菜单快照 + 真实详情结构（已实测）验证判定与热量口径。

运行：python tests/test_local.py
"""

from __future__ import annotations

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.domain import nutrition, taxonomy  # noqa: E402

SNAPSHOT = os.path.join(ROOT, "data", "menu-categories.snapshot.json")

# 来自 2026-10-24 真实 query-meal-detail 实测
FACTS = {
    "1440": {"rounds": 0, "supportModify": True, "name": "麦辣鸡腿汉堡"},
    "1406": {"rounds": 0, "supportModify": True, "name": "板烧鸡腿堡"},
    "507411": {"rounds": 0, "supportModify": True, "name": "麦咖啡™美式"},
    "507409": {"rounds": 0, "supportModify": True, "name": "麦咖啡™奶铁"},
    "9900000891": {"rounds": 4, "supportModify": False, "name": "麦辣鸡腿堡四件套"},
}


def load_items() -> dict[str, dict]:
    with open(SNAPSHOT, encoding="utf-8") as f:
        snap = json.load(f)
    items: dict[str, dict] = {}
    for cat in snap["categories"]:
        for meal in cat["meals"]:
            code = meal["code"]
            entry = items.setdefault(code, {"code": code, "name": meal["name"], "categories": []})
            entry["categories"].append(cat["name"])
    return items


class TestTaxonomy(unittest.TestCase):
    def setUp(self):
        self.items = load_items()

    def test_normalize_category_strips_newlines(self):
        self.assertEqual(taxonomy.normalize_category("鸡肉汉堡\n/卷"), "鸡肉汉堡/卷")
        self.assertEqual(taxonomy.normalize_category(" 500\n大卡套餐 "), "500大卡套餐")

    def test_single_burger_allowed(self):
        item = self.items["1440"]
        d = taxonomy.classify("1440", item["name"], item["categories"], FACTS["1440"])
        self.assertTrue(d.allowed)
        self.assertEqual(d.kind, "burger")
        self.assertFalse(d.is_combo)

    def test_combo_blocked_even_in_burger_category(self):
        item = self.items["9900000891"]
        self.assertIn("鸡肉汉堡\n/卷", item["categories"])
        d = taxonomy.classify("9900000891", item["name"], item["categories"], FACTS["9900000891"])
        self.assertFalse(d.allowed)
        self.assertTrue(d.is_combo)
        self.assertIn("套餐组成", d.reason)

    def test_black_coffee_allowed_by_override(self):
        item = self.items["507411"]
        d = taxonomy.classify("507411", item["name"], item["categories"], FACTS["507411"])
        self.assertTrue(d.allowed)
        self.assertEqual(d.kind, "coffee")

    def test_milk_coffee_blocked(self):
        item = self.items["507409"]
        d = taxonomy.classify("507409", item["name"], item["categories"], FACTS["507409"])
        self.assertFalse(d.allowed)
        self.assertIn("未标注为黑咖啡", d.reason)

    def test_merchandise_in_other_category_blocked(self):
        item = self.items["521956"]  # 麦当劳限定丝巾
        d = taxonomy.classify("521956", item["name"], item["categories"], {"rounds": 0})
        self.assertFalse(d.allowed)
        self.assertEqual(d.kind, "other")

    def test_unknown_facts_do_not_guess(self):
        item = self.items["1440"]
        d = taxonomy.classify("1440", item["name"], item["categories"], None)
        self.assertIsNone(d.allowed)


class TestLeanCategories(unittest.TestCase):
    """减脂分类栏归位：门店把黑咖啡挂在“饮品”下时，不能让它从分类栏消失。

    实测门店 3560147 的减脂菜单只有 1 款咖啡（鲜萃咖啡），门店分类叫“饮品”，
    原实现照抄分类 + 前端隐藏“其他”分类，导致减脂用户完全看不到咖啡。
    """

    def setUp(self):
        from app.server import lean_categories

        self.fn = lean_categories

    @staticmethod
    def _item(code, kind):
        return {"code": code, "name": code, "decision": {"kind": kind, "allowed": True}}

    def test_coffee_under_drink_category_moves_to_coffee(self):
        cats = [
            {"name": "巨无霸\n牛鱼肉堡", "kind": "burger", "codes": ["b1"]},
            {"name": "饮品", "kind": "other", "codes": ["c1"]},
        ]
        items = [self._item("b1", "burger"), self._item("c1", "coffee")]
        out = self.fn(cats, items)
        self.assertEqual([c["kind"] for c in out], ["burger", "coffee"])
        self.assertEqual(out[1]["codes"], ["c1"])
        self.assertNotIn("other", [c["kind"] for c in out])

    def test_new_coffee_category_created_when_store_has_none(self):
        cats = [
            {"name": "巨无霸\n牛鱼肉堡", "kind": "burger", "codes": ["b1"]},
            {"name": "饮品", "kind": "other", "codes": ["c1"]},
        ]
        items = [self._item("b1", "burger"), self._item("c1", "coffee")]
        out = self.fn(cats, items)
        coffee = next(c for c in out if c["kind"] == "coffee")
        self.assertEqual(coffee["name"], "咖啡")

    def test_duplicate_category_does_not_repeat_item(self):
        cats = [
            {"name": "汉堡", "kind": "burger", "codes": ["b1"]},
            {"name": "人气热卖", "kind": "other", "codes": ["b1"]},
        ]
        items = [self._item("b1", "burger")]
        out = self.fn(cats, items)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["codes"], ["b1"])

    def test_empty_category_dropped(self):
        cats = [
            {"name": "汉堡", "kind": "burger", "codes": ["b1"]},
            {"name": "麦咖啡", "kind": "coffee", "codes": ["gone"]},
        ]
        items = [self._item("b1", "burger")]
        out = self.fn(cats, items)
        self.assertEqual([c["kind"] for c in out], ["burger"])


class TestLLMParsing(unittest.TestCase):
    """模型只做「翻译」：输出条件 JSON，数值与商品编码仍由规则引擎决定。

    这些用例不联网，直接喂模型可能返回的结构，验证合并与清洗逻辑。
    """

    def setUp(self):
        from app import llm
        from app.domain import planner

        self.llm = llm
        self.planner = planner

        def mk(code, name, price, kcal, kind):
            return {
                "code": code,
                "name": name,
                "price": str(price),
                "kcal": {"unit": kcal, "source": "official"},
                "hasSauces": False,
                "autoSauceRemoved": False,
                "decision": {"kind": kind, "allowed": True},
            }

        self.items = [
            mk("1440", "麦辣鸡腿汉堡", 22.5, 440, "burger"),
            mk("1450", "麦香鸡", 16.5, 324, "burger"),
            mk("1600", "麦香鱼", 22, 280, "burger"),
            mk("520689", "鲜萃咖啡", 10.5, 10, "coffee"),
        ]

    def test_normalize_drops_invalid_values(self):
        raw = {
            "kcalMax": "1000",
            "budget": -5,
            "people": 2,
            "counts": {"main": "3", "drink": 0},
            "dietHints": ["highProtein", "随便写的"],
            "prefs": ["cheap", "expensive"],
            "mustKeywords": ["麦香鱼", 123, ""],
            "summary": "两人份",
        }
        n = self.llm.normalize(raw)
        self.assertEqual(n["kcalMax"], 1000.0)
        self.assertIsNone(n["budget"])          # 负数当没提
        self.assertEqual(n["people"], 2)
        self.assertEqual(n["counts"]["main"], 3)
        self.assertIsNone(n["counts"]["drink"])  # 0 当没提
        self.assertEqual(n["dietHints"], ["highProtein"])
        self.assertEqual(n["prefs"], ["cheap"])
        self.assertEqual(n["mustKeywords"], ["麦香鱼"])

    def test_people_becomes_counts(self):
        parsed = self.planner.parse_request("两个人，随便吃", self.items, "lean")
        self.assertFalse(parsed["explicitCounts"])
        merged, added = self.planner.merge_llm(
            parsed, self.llm.normalize({"people": 2, "summary": "两人份"}), self.items
        )
        self.assertEqual(merged["counts"], {"main": 2, "drink": 2, "side": 0})
        self.assertTrue(merged["explicitCounts"])
        self.assertTrue(any("2 人份" in label for label in added))

    def test_kcal_never_becomes_condition(self):
        """产品口径：配餐不再按热量筛选，正则与模型给的热量都不采纳。"""
        parsed = self.planner.parse_request("低于1000千卡", self.items, "lean")
        self.assertIsNone(parsed["kcalLimit"])
        self.assertTrue(any("不按热量筛" in u for u in parsed["unresolved"]))
        merged, _ = self.planner.merge_llm(parsed, self.llm.normalize({"kcalMax": 1200}), self.items)
        self.assertIsNone(merged["kcalLimit"])

    def test_taste_keyword_excludes_items(self):
        parsed = self.planner.parse_request("来点吃的", self.items, "lean")
        merged, added = self.planner.merge_llm(
            parsed, self.llm.normalize({"excludeKeywords": ["辣"]}), self.items
        )
        self.assertIn("1440", merged["exclude"])
        self.assertTrue(any("排除" in label for label in added))
        gen = self.planner.generate(self.items, merged, mode="lean")
        codes = {r["code"] for c in gen["plans"] for r in c["rows"]}
        self.assertNotIn("1440", codes)

    def test_must_keyword_maps_to_code(self):
        parsed = self.planner.parse_request("随便配", self.items, "lean")
        merged, _ = self.planner.merge_llm(
            parsed, self.llm.normalize({"mustKeywords": ["麦香鱼"]}), self.items
        )
        self.assertIn("1600", merged["must"])

    def test_diet_hint_ignored(self):
        """「高蛋白／低脂／低碳水」这类健身向条件已下线，不再转成阈值。"""
        parsed = self.planner.parse_request("想吃高蛋白的", self.items, "lean")
        merged, _ = self.planner.merge_llm(
            parsed, self.llm.normalize({"dietHints": ["highProtein"]}), self.items
        )
        self.assertEqual(merged["macroLimits"], {})
        gen = self.planner.generate(self.items, merged, mode="lean")
        self.assertTrue(gen["plans"])

    def test_people_survives_drink_off(self):
        """回归：说「两个人…不要咖啡」时，「不要咖啡」不能挡住人数折算。

        原实现里「不要饮品」也置 explicitCounts=True，导致人数整条被跳过，
        最终只配出 1 个汉堡（用户实测反馈）。
        """
        parsed = self.planner.parse_request("两个人，不要咖啡", self.items, "lean")
        self.assertEqual(parsed["counts"]["drink"], 0)
        self.assertFalse(parsed["explicitCounts"])
        merged, added = self.planner.merge_llm(
            parsed, self.llm.normalize({"people": 2, "excludeKeywords": ["咖啡"]}), self.items
        )
        self.assertEqual(merged["counts"], {"main": 2, "drink": 0, "side": 0})
        self.assertTrue(merged["explicitCounts"])
        self.assertTrue(any("不含饮品" in label for label in added))

    def test_explicit_count_beats_people(self):
        """用户自己说了「两个汉堡」时，人数不再覆盖份数。"""
        parsed = self.planner.parse_request("两个汉堡，不要咖啡", self.items, "lean")
        self.assertEqual(parsed["counts"]["main"], 2)
        self.assertTrue(parsed["explicitCounts"])
        merged, _ = self.planner.merge_llm(parsed, self.llm.normalize({"people": 3}), self.items)
        self.assertEqual(merged["counts"]["main"], 2)
        self.assertEqual(merged["counts"]["drink"], 0)

    def test_partial_counts_keep_existing(self):
        """模型只给了主餐份数时，不能把饮品份数清零。"""
        parsed = self.planner.parse_request("随便", self.items, "lean")
        merged, _ = self.planner.merge_llm(
            parsed, self.llm.normalize({"counts": {"main": 2}}), self.items
        )
        self.assertEqual(merged["counts"]["main"], 2)
        self.assertEqual(merged["counts"]["drink"], 1)

    def test_kcal_does_not_filter_plans(self):
        """热量条件（含「每人」口径）不再影响方案，人数照常折算。"""
        parsed = self.planner.parse_request("每个人不超过1000千卡，两个人，不要咖啡", self.items, "lean")
        self.assertIsNone(parsed["kcalLimit"])
        merged, _ = self.planner.merge_llm(
            parsed, self.llm.normalize({"people": 2, "kcalMax": 1000, "kcalScope": "perPerson"}), self.items
        )
        self.assertIsNone(merged["kcalLimit"])
        self.assertEqual(merged["counts"]["main"], 2)
        gen = self.planner.generate(self.items, merged, mode="lean")
        self.assertTrue(gen["plans"])

    def test_people_allows_more_than_one_main_each(self):
        """两个人时允许配 3~4 个汉堡：人均 1 个常常不够吃。"""
        parsed = self.planner.parse_request("两个人，不要咖啡", self.items, "lean")
        merged, _ = self.planner.merge_llm(parsed, self.llm.normalize({"people": 2}), self.items)
        self.assertTrue(merged.get("mainElastic"))
        gen = self.planner.generate(self.items, merged, mode="lean")
        self.assertTrue(gen["plans"])
        counts = {c["count"] for c in gen["plans"]}
        self.assertTrue(any(n >= 3 for n in counts), f"没有多于 2 件的方案：{counts}")

    def test_explicit_counts_do_not_float(self):
        """用户自己说了「两个汉堡」就严格两个，不自动加量。"""
        parsed = self.planner.parse_request("两个汉堡，不要咖啡", self.items, "lean")
        merged, _ = self.planner.merge_llm(parsed, self.llm.normalize({"people": 3}), self.items)
        self.assertFalse(merged.get("mainElastic"))
        gen = self.planner.generate(self.items, merged, mode="lean")
        self.assertTrue(gen["plans"])
        self.assertTrue(all(c["count"] == 2 for c in gen["plans"]))

    def test_trial_order_covers_different_item_counts(self):
        """试算取样要覆盖不同件数，否则 3、4 个汉堡的方案永远轮不到展示。"""
        cand = lambda codes: {"rows": [{"code": c, "name": c, "quantity": 1, "kind": "burger"} for c in codes]}
        candidates = [cand(["A"]), cand(["A", "B"]), cand(["A", "B", "C"]), cand(["A", "B", "C", "D"])]
        for c in candidates:
            c["rawPrice"] = 10 * len(c["rows"])
            c["rawKcal"] = 100 * len(c["rows"])
        picked = self.planner._trial_order(candidates, 4)
        self.assertEqual({len(c["rows"]) for c in picked}, {1, 2, 3, 4})

    def test_trial_order_keeps_same_items_different_quantity(self):
        """同款不同份数不能当重复：A+B 与 A+B×2 是两个不同方案。

        原实现只按商品编码去重，导致 3 个、4 个汉堡的组合（大多是「某个 ×2」）
        被当成 2 个的重复项丢掉，用户看到永远是 2 个汉堡。
        """
        def row(code, qty):
            return {"code": code, "name": code, "quantity": qty, "kind": "burger"}

        two = {"rows": [row("A", 1), row("B", 1)], "rawPrice": 30, "rawKcal": 600, "count": 2}
        three = {"rows": [row("A", 1), row("B", 2)], "rawPrice": 45, "rawKcal": 900, "count": 3}
        picked = self.planner._trial_order([two, three], 2)
        self.assertEqual(len(picked), 2, "同款不同份数被错误去重")
        self.assertEqual({c["count"] for c in picked}, {2, 3})

    def test_prefer_keyword_changes_order_not_filter(self):
        parsed = self.planner.parse_request("想吃鱼", self.items, "lean")
        merged, _ = self.planner.merge_llm(
            parsed, self.llm.normalize({"preferKeywords": ["鱼"]}), self.items
        )
        gen = self.planner.generate(self.items, merged, mode="lean")
        self.assertTrue(gen["plans"])
        # 命中偏好的方案排在最前，但便宜方案仍在候选里（偏好只调顺序不放宽条件）
        first = {r["name"] for r in gen["plans"][0]["rows"]}
        self.assertIn("麦香鱼", first)

    def test_missing_key_falls_back(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MINIMAX_API_KEY", None)
            data, err = self.llm.parse_request("两个人", self.items)
        self.assertIsNone(data)
        self.assertIn("MINIMAX_API_KEY", err)


class TestNutrition(unittest.TestCase):
    def test_official_kcal_used(self):
        r = nutrition.estimate_item("1440", "麦辣鸡腿汉堡", "burger")
        self.assertEqual(r["unitKcal"], 485)
        self.assertEqual(r["source"], "official")

    def test_combo_does_not_borrow_single_item_value(self):
        r = nutrition.estimate_item("9900000891", "麦辣鸡腿堡四件套", "burger", is_combo=True)
        self.assertNotEqual(r["unitKcal"], 485)
        self.assertIn("套餐", r["note"])

    def test_needs_review_candidate_not_applied(self):
        # 4810 薯条：归一候选是“迷你薯条 106”，不得采用
        r = nutrition.estimate_item("4810", "薯条", "side")
        self.assertNotEqual(r["unitKcal"], 106)
        self.assertIn("待核对", r["note"])

    def test_sauce_removal_deducts_and_never_negative(self):
        r = nutrition.estimate_item("1440", "麦辣鸡腿汉堡", "burger", sauces_removed=1)
        self.assertEqual(r["unitKcal"], 485 - 45)
        r2 = nutrition.estimate_item("1440", "麦辣鸡腿汉堡", "burger", sauces_removed=99)
        self.assertEqual(r2["unitKcal"], 0)

    def test_cart_sum_rounds_once(self):
        out = nutrition.sum_cart(
            [
                {"code": "1440", "name": "麦辣鸡腿汉堡", "kind": "burger", "quantity": 2},
                {"code": "507411", "name": "麦咖啡™美式", "kind": "coffee", "blackCoffee": True, "quantity": 1},
            ]
        )
        self.assertEqual(out["totalKcal"], 485 * 2 + 10)
        self.assertEqual(out["display"], "约 980 千卡")


class TestPlanner(unittest.TestCase):
    """智能配餐（US-03）：解析口径与组合生成。离线，不依赖网络。"""

    def setUp(self):
        from app.domain import planner

        self.planner = planner

        def mk(code, name, price, kcal, kind, sauces=False):
            return {
                "code": code,
                "name": name,
                "price": str(price),
                "kcal": {"unit": kcal, "source": "official"},
                "hasSauces": sauces,
                "autoSauceRemoved": sauces,
                "decision": {"kind": kind, "allowed": True},
            }

        self.items = [
            mk("A", "恰好堡", 20, 990, "burger", True),
            mk("B", "轻堡", 18, 300, "burger", True),
            mk("C", "美式", 10, 10, "coffee"),
            mk("D", "拿铁", 15, 180, "coffee"),
        ]

    def test_kcal_condition_ignored(self):
        """热量条件不再参与筛选：说了也不影响候选，只留一条提示。"""
        p = self.planner.parse_request("不超过1000千卡", self.items, "lean")
        self.assertIsNone(p["kcalLimit"])
        self.assertTrue(any("不按热量筛" in u for u in p["unresolved"]))
        gen = self.planner.generate(self.items, p, mode="lean")
        self.assertTrue(gen["plans"])
        self.assertTrue(any(c["rawKcal"] >= 1000 for c in gen["plans"]), "热量不应再过滤候选")

    def test_no_limit_when_not_mentioned(self):
        p = self.planner.parse_request("要一个汉堡一杯咖啡", self.items, "lean")
        self.assertIsNone(p["kcalLimit"])
        self.assertIsNone(p["budget"])  # 未提上限就不自动补

    def test_budget_with_unit(self):
        p = self.planner.parse_request("预算40元", self.items, "lean")
        self.assertEqual(p["budget"]["value"], 40.0)
        self.assertIn("40", p["budget"]["text"])

    def test_short_item_name_hit(self):
        items = self.items + [
            {"code": "S", "name": "薯条", "price": "12", "kcal": {"unit": 230, "source": "official"},
             "hasSauces": False, "autoSauceRemoved": False, "decision": {"kind": "side", "allowed": True}}
        ]
        p = self.planner.parse_request("要一份薯条", items, "normal")
        self.assertIn("S", p["must"])

    def test_must_and_exclude(self):
        p = self.planner.parse_request("要轻堡，不要恰好堡", self.items, "lean")
        self.assertIn("B", p["must"])
        self.assertIn("A", p["exclude"])
        # 被排除的商品不能同时算必选
        self.assertNotIn("A", p["must"])

    def test_same_item_count_merges_into_quantity(self):
        p = self.planner.parse_request("两个汉堡一杯咖啡", self.items, "lean")
        self.assertEqual(p["counts"]["main"], 2)
        gen = self.planner.generate(self.items, p, mode="lean")
        self.assertTrue(gen["plans"])
        top = gen["plans"][0]
        total_qty = sum(r["quantity"] for r in top["rows"])
        self.assertEqual(total_qty, 3)  # 2 个汉堡 + 1 杯咖啡，不能被去重成 2 件

    def test_blocker_uses_real_candidate_not_single_item(self):
        # 单个最便宜商品是 10 元（美式），但组合最便宜是 28 元，阻碍说明必须用后者
        p = self.planner.parse_request("预算5元", self.items, "lean")
        gen = self.planner.generate(self.items, p, mode="lean")
        self.assertFalse(gen["plans"])
        self.assertIsNotNone(gen["cheapest"])
        self.assertGreater(gen["cheapest"]["rawPrice"], 5)
        blocker = self.planner._explain_blocker(
            self.items, p, "lean", cheapest=gen["cheapest"]
        )
        msg = blocker["message"] + "".join(blocker["suggestions"])
        self.assertIn("28", msg)
        self.assertNotIn("10.00", msg)  # 不能拿单个最便宜商品当组合结论

    def test_cart_does_not_add_to_plan_totals(self):
        """配餐的价格和热量只算方案自身，不与购物车相加（2026-10-24 用户拍板）。"""
        p = self.planner.parse_request("不超过2000千卡", self.items, "lean")
        locked = [{"code": "A", "quantity": 1, "kcal": 990, "price": 20}]
        gen = self.planner.generate(self.items, p, mode="lean", locked=locked)
        self.assertTrue(gen["plans"])
        for c in gen["plans"][:5]:
            # 方案热量必须恰好等于自身明细之和，不含购物车里的 990
            self.assertEqual(c["rawKcal"], sum(r["kcal"] for r in c["rows"]))
            self.assertNotIn("A", [r["code"] for r in c["rows"]])  # 但也不重复推荐

    def test_blocker_never_mentions_cart(self):
        """阻碍说明只讲方案自身，不得出现「购物车已选…合计」这类整车口径。

        回归背景：曾把 760（购物车）+335（新增）=1095 写成一句话，用户读成
        「麦香鱼+咖啡=1095」。现在配餐独立计算，文案里不该再有购物车数字。
        """
        p = self.planner.parse_request("预算5元", self.items, "lean")
        locked = [{"code": "A", "name": "恰好堡", "quantity": 1, "kcal": 990, "price": 20}]
        gen = self.planner.generate(self.items, p, mode="lean", locked=locked)
        self.assertFalse(gen.get("plans"))  # 最便宜的一组 28 元也超 5 元
        msg = self.planner._explain_blocker(
            self.items, p, "lean", cheapest=gen["cheapest"]
        )["message"]
        self.assertIn("最便宜的一组", msg)
        self.assertNotIn("购物车", msg)
        self.assertNotIn("已选", msg)
        self.assertNotIn("合计", msg)

    def test_rows_text_never_mentions_cart(self):
        """_rows_text 只描述本次新增，绝不能把购物车件数混进括号里。"""
        rows = [{"name": "麦香鱼", "quantity": 1}, {"name": "美式", "quantity": 2}]
        txt = self.planner._rows_text(rows)
        self.assertEqual(txt, "麦香鱼×1 + 美式×2")
        self.assertNotIn("已选", txt)

    def _cand(self, main_code, drink_code, price, kcal):
        rows = [
            {
                "code": main_code,
                "name": main_code,
                "price": float(price),
                "quantity": 1,
                "kcal": kcal,
                "sauceOff": False,
                "kind": "burger",
                "locked": False,
            },
            {
                "code": drink_code,
                "name": drink_code,
                "price": 10.0,
                "quantity": 1,
                "kcal": 10,
                "sauceOff": False,
                "kind": "coffee",
                "locked": False,
            },
        ]
        return {"rows": rows, "rawPrice": float(price) + 10.0, "rawKcal": kcal + 10}

    # ---------- 三大营养素（显示项 + 筛选条件） ----------

    def _macro_item(self, code, name, price, kcal, kind, macros):
        return {
            "code": code,
            "name": name,
            "price": str(price),
            "kcal": {"unit": kcal, "source": "official"},
            "hasSauces": False,
            "autoSauceRemoved": False,
            "macros": macros,
            "decision": {"kind": kind, "allowed": True},
        }

    def test_macro_conditions_ignored(self):
        """营养素条件已下线：解析到也只给提示，不进筛选条件。"""
        p = self.planner.parse_request("蛋白质25g以上，脂肪低于15g", self.items, "normal")
        self.assertEqual(p["macroLimits"], {})
        self.assertTrue(any("不按营养素筛" in u for u in p["unresolved"]))

    def test_macro_no_longer_filters_items(self):
        """曾经「蛋白质25g以上」会排除低蛋白汉堡，现在不再排除。"""
        items = [
            self._macro_item("L1", "低蛋白堡", 20, 300, "burger", {"protein": 10.0, "fat": 10.0, "carbohydrate": 30.0}),
            self._macro_item("H2", "高蛋白堡", 22, 320, "burger", {"protein": 30.0, "fat": 12.0, "carbohydrate": 30.0}),
        ]
        p = self.planner.parse_request("蛋白质25g以上", items, "normal")
        gen = self.planner.generate(items, p, mode="normal")
        names = [r["name"] for c in gen["plans"] for r in c["rows"]]
        self.assertIn("低蛋白堡", names)

    def test_macro_upper_bound_is_energy_derived(self):
        """缺数据的商品：脂肪上界 ≤ 热量/9，蛋白/碳水 ≤ 热量/4。"""
        rows = [{"quantity": 1, "macros": None, "kcal": 90}]
        low, high, complete, unknown = self.planner._macro_bounds(rows)
        self.assertEqual(low["fat"], 0.0)
        self.assertAlmostEqual(high["fat"], 10.0)      # 90/9
        self.assertAlmostEqual(high["protein"], 22.5)  # 90/4
        self.assertFalse(complete)
        self.assertFalse(unknown)

    def test_macro_text_hidden_when_no_official_data(self):
        """一件官方数据都没有时，三项下界全是 0：不显示「≥0g」，留空。"""
        empty = {"protein": 0.0, "fat": 0.0, "carbohydrate": 0.0}
        self.assertEqual(self.planner._macros_text(empty, True), "")
        self.assertEqual(self.planner._macros_text(None, False), "")
        # 至少有值时才显示，且缺数据的方案标「≥」
        self.assertEqual(
            self.planner._macros_text({"protein": 15.0, "fat": 17.0, "carbohydrate": 39.0}, True),
            "蛋白 ≥15g · 脂肪 ≥17g · 碳水 ≥39g",
        )

    def test_trial_order_not_crushed_by_single_drink(self):
        """回归：试算取样不能被单一饮品分组压扁。

        背景：_trial_order 曾按饮品分组、每组只取 2 个。门店只有一种可选咖啡时
        整个候选池被压成 2 个，实际出现「14 组都符合条件却只出 2 个方案」。
        """
        cands = [self._cand("B%d" % i, "D1", 20 + i, 400) for i in range(12)]
        order = self.planner._trial_order(cands, 12)
        self.assertEqual(len(order), 12)

    def test_trial_order_diversifies_main_dish(self):
        """limit=3 时应给出 3 个不同主餐，而不是同一汉堡换三种咖啡。"""
        cands = []
        for m in ("M1", "M2", "M3"):
            for d, extra in (("D1", 0.0), ("D2", 3.0), ("D3", 6.0)):
                cands.append(self._cand(m, d, 20 + extra, 400))
        order = self.planner._trial_order(cands, 3)
        self.assertEqual(len(order), 3)
        self.assertEqual(len({self.planner._main_key(c) for c in order}), 3)

    def test_skip_excludes_previous_plans(self):
        p = self.planner.parse_request("不超过2000千卡", self.items, "lean")
        first = self.planner.generate(self.items, p, mode="lean")
        sig = [r["code"] for r in first["plans"][0]["rows"]]
        again = self.planner.generate(self.items, p, mode="lean", skip=[sig])
        self.assertFalse(any([r["code"] for r in c["rows"]] == sig for c in again["plans"]))


class TestGeo(unittest.TestCase):
    def test_strip_city_suffix(self):
        from app import geo

        self.assertEqual(geo.strip_city_suffix("上海市"), "上海")
        self.assertEqual(geo.strip_city_suffix("郑州"), "郑州")
        self.assertEqual(geo.strip_city_suffix("香港特别行政区"), "香港")

    def test_traditional_to_simplified(self):
        from app import geo

        self.assertEqual(geo.to_simplified("黃浦區"), "黄浦区")

    def test_keyword_candidates_order_and_dedup(self):
        from app import geo

        self.assertEqual(geo.keyword_candidates("上海市", "黃浦區"), ["黄浦区", "上海市"])
        self.assertEqual(geo.keyword_candidates("上海", "上海"), ["上海"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
