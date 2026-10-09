#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推荐 / 比价 / 儿童套餐 的单元测试。

全部跑在 mock 数据上，不联网、不下单。
跑法：
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from mcp_client import McDMcpClient  # noqa: E402
from menu_engine import MenuEngine  # noqa: E402


class EngineCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = MenuEngine(McDMcpClient(), use_mock=True)
        cls.engine.load()


class TestRecommend(EngineCase):
    def plan(self, **kwargs):
        kwargs.setdefault("budget", 60)
        kwargs.setdefault("count", 3)
        return self.engine.recommend(**kwargs)

    def test_basic_three_item_plan(self):
        plans = self.plan(wants=["汉堡", "薯条", "可乐"])
        self.assertTrue(plans)
        for plan in plans:
            with self.subTest(plan=plan["items"]):
                self.assertEqual(len(plan["items"]), 3)

    def test_every_plan_has_detail_and_price(self):
        for plan in self.plan(wants=["汉堡", "薯条", "可乐"]):
            with self.subTest(plan=plan["items"]):
                self.assertEqual(len(plan["items_detail"]), len(plan["items"]))
                for product in plan["items_detail"]:
                    self.assertIsNotNone(product)
                    self.assertIsNotNone(product.kcal)
                self.assertIsNotNone(plan["amount"])
                self.assertGreater(plan["amount"], 0)

    def test_never_exceeds_budget(self):
        for budget in (20, 30, 45, 60):
            plans = self.plan(wants=["汉堡", "薯条", "可乐"], budget=budget)
            for plan in plans:
                with self.subTest(budget=budget, plan=plan["items"]):
                    # amount 是走算价接口的实付；mock 下等于菜单价之和
                    self.assertLessEqual(plan["amount"] / 100, budget + 0.01)

    def test_kcal_cap_is_respected(self):
        """热量上限可以有容差，但不能离谱。"""
        tolerance = 120                 # 和 recommend 内部保持一致
        for cap in (400, 600, 900):
            plans = self.plan(wants=["主餐", "饮料"], kcal=cap)
            for plan in plans:
                with self.subTest(cap=cap, plan=plan["items"]):
                    self.assertLessEqual(plan["kcal"], cap + tolerance)

    def test_kcal_reason_distinguishes_budget_from_calories(self):
        """失败原因不能张冠李戴 —— 预算不够别说成热量超了。"""
        self.engine.recommend(wants=["汉堡", "薯条", "可乐"], budget=5)
        self.assertIn("预算", self.engine.last_reason)
        self.assertNotIn("大卡装不下", self.engine.last_reason)

        self.engine.recommend(wants=["主餐", "小食", "饮料"], budget=200, kcal=100)
        self.assertIn("大卡装不下", self.engine.last_reason)
        self.assertNotIn("预算不够", self.engine.last_reason)

    def test_no_match_reason(self):
        plans = self.engine.recommend(wants=["外星人套餐"], budget=50)
        self.assertEqual(plans, [])
        self.assertIn("找不到", self.engine.last_reason)

    def test_require_all_false_allows_partial(self):
        """「有薯条，其他怎么安排」—— 必选项在就行，其余能塞就塞。"""
        plans = self.engine.recommend(wants=["薯条", "主餐", "饮料"],
                                      budget=60, kcal=800, require_all=False)
        self.assertTrue(plans)
        for plan in plans:
            with self.subTest(plan=plan["items"]):
                self.assertIn("薯条", plan["items"])
                self.assertLessEqual(plan["kcal"], 800 + 120)

    def test_avoid_word_filters_candidates(self):
        """说不要辣，带「辣」的餐品就不能出现。"""
        plans = self.engine.recommend(wants=["汉堡"], budget=80, avoid=["辣"])
        self.assertTrue(plans)
        for plan in plans:
            for name in plan["items"]:
                with self.subTest(name=name):
                    self.assertNotIn("辣", name)

    def test_plans_are_distinct(self):
        plans = self.plan(wants=["汉堡", "薯条", "可乐"], count=3)
        seen = {tuple(sorted(p["items"])) for p in plans}
        self.assertEqual(len(seen), len(plans), "出了重复方案")

    def test_plan_numbers_are_sequential(self):
        plans = self.plan(wants=["汉堡", "薯条", "可乐"])
        self.assertEqual([p["no"] for p in plans], list(range(1, len(plans) + 1)))


class TestAvoidNotes(EngineCase):
    def test_removable_ingredients_come_from_api(self):
        """能去掉什么配料必须现查接口，不能预设一张表。"""
        product = self.engine.by_name("麦辣鸡腿汉堡")
        if product is None:
            self.skipTest("mock 菜单里没有这个品")
        items = self.engine.removable_ingredients(product.code)
        self.assertTrue(items, "特制项没读出来")
        for item in items:
            with self.subTest(item=item):
                self.assertIn("name", item)

    def test_avoid_matches_splits_removable_and_not(self):
        product = self.engine.by_name("麦辣鸡腿汉堡")
        if product is None:
            self.skipTest("mock 菜单里没有这个品")
        can, cannot = self.engine.avoid_matches(product.code, ["洋葱", "香菜", "酱"])
        self.assertIsInstance(can, list)
        self.assertIsInstance(cannot, list)
        # 能去掉的和去不掉的不能重叠
        self.assertFalse(set(can) & set(cannot))

    def test_notes_are_attached_to_plan(self):
        plans = self.engine.recommend(wants=["汉堡"], budget=80, avoid=["洋葱"])
        for plan in plans:
            with self.subTest(plan=plan["items"]):
                self.assertIsInstance(plan["notes"], list)
                for note in plan["notes"]:
                    self.assertIn("去掉", note)


class TestComboBreakdown(EngineCase):
    def test_combos_have_no_aggregate_kcal_but_do_decompose(self):
        combos = self.engine.combo_catalog()
        self.assertGreater(len(combos), 40)
        for combo in combos[:10]:
            with self.subTest(combo=combo.name):
                product = self.engine.products.get(combo.code)
                self.assertIsNone(product.kcal)
                detail = self.engine.combo_breakdown(combo)
                self.assertTrue(detail["parts"])
                # 拆出来必须要么有热量，要么老实说「这几样没收录」
                self.assertTrue(detail["kcal"] > 0 or detail["unknown"],
                                f"「{combo.name}」合计 0 大卡却没说缺什么")

    def test_missing_part_never_renders_as_zero(self):
        """「不知道」不能被念成「0 大卡」——这是营养工具最不能犯的错。"""
        for combo in self.engine.combo_catalog():
            detail = self.engine.combo_breakdown(combo)
            if detail["unknown"]:
                with self.subTest(combo=combo.name):
                    self.assertNotEqual(detail["kcal_text"], "0 大卡")
                    self.assertNotEqual(detail["kcal_text"], f"{detail['kcal']} 大卡")
                    self.assertIn("没收录", detail["kcal_text"])
            else:
                with self.subTest(combo=combo.name):
                    self.assertEqual(detail["kcal_text"], f"{detail['kcal']} 大卡")

    def test_combo_total_equals_sum_of_parts(self):
        for combo in self.engine.combo_catalog()[:15]:
            detail = self.engine.combo_breakdown(combo)
            with self.subTest(combo=combo.name):
                expected = sum(p["kcal"] for p in detail["parts"] if p["kcal"] is not None)
                self.assertEqual(detail["kcal"], expected)

    def test_incomplete_combo_is_flagged(self):
        for combo in self.engine.combo_catalog():
            detail = self.engine.combo_breakdown(combo)
            if detail["unknown"]:
                with self.subTest(combo=combo.name):
                    self.assertFalse(detail["complete"])
            else:
                with self.subTest(combo=combo.name):
                    self.assertTrue(detail["complete"])

    def test_combo_nutrition_has_all_macros(self):
        combo = self.engine.combo_catalog()[0]
        detail = self.engine.combo_nutrition(combo)
        for key in ("kcal", "protein", "fat", "carb", "sodium"):
            with self.subTest(key=key):
                self.assertIn(key, detail["nutrition"])
                self.assertGreaterEqual(detail["nutrition"][key], 0)


class TestKidsMeals(EngineCase):
    """儿童套餐：判定、玩具、饮料档。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.kids = cls.engine.kids_meals()

    def test_detected(self):
        self.assertTrue(self.kids, "一个儿童套餐都没识别出来")
        self.assertGreaterEqual(len(self.kids), 3)

    def test_detection_is_structural_not_name_based(self):
        """判定依据必须是「套餐里有玩具这一档」，不能只靠名字里带「乐园」。"""
        for meal in self.kids:
            with self.subTest(meal=meal["name"]):
                has_toy_slot = meal["has_toy"]
                has_name = any(k in meal["name"] for k in ("乐园", "亲子", "儿童"))
                self.assertTrue(has_toy_slot or has_name,
                                f"「{meal['name']}」既没有玩具档，名字也不像儿童套餐")

    def test_toy_is_not_counted_as_calories(self):
        """玩具、周边本来就不该有热量，不能算进合计里。"""
        for meal in self.kids:
            with self.subTest(meal=meal["name"]):
                non_food = [p for p in meal["parts"] if p["non_food"]]
                for part in non_food:
                    self.assertIsNone(part["kcal"])
                expected = sum(p["kcal"] for p in meal["parts"] if p["kcal"] is not None)
                self.assertEqual(meal["kcal"], expected)

    def test_every_kid_meal_has_kcal(self):
        for meal in self.kids:
            with self.subTest(meal=meal["name"]):
                self.assertGreater(meal["kcal"], 0)

    def test_official_kid_drinks_are_not_soda(self):
        """儿童餐自己的饮料档只有牛奶 / 纯悦 / 苹果汁，官方没配碳酸饮料。

        「开心亲子双人餐」有**两个**饮料档：一档是小孩的（仅无糖/奶类），
        一档是家长的（可选可乐）。所以只能查 round 名里带「开心乐园」的那一档。
        """
        kid_round = "开心乐园餐饮料"
        for meal in self.kids:
            drinks = [p for p in meal["parts"] if kid_round in (p["round"] or "")]
            for part in drinks:
                with self.subTest(meal=meal["name"], drink=part["name"]):
                    self.assertNotIn("可乐", part["name"])
                    self.assertNotIn("雪碧", part["name"])

    def test_two_person_kid_meal_has_two_drink_rounds(self):
        """「开心亲子双人餐」是家长+小孩双人份，不能被当成一份儿童餐。"""
        two_person = [m for m in self.kids
                      if any("双人" in p["round"] or "套餐内饮料" in p["round"]
                             for p in m["parts"])]
        for meal in two_person:
            with self.subTest(meal=meal["name"]):
                drink_rounds = [p for p in meal["parts"] if "饮料" in (p["round"] or "")]
                self.assertGreaterEqual(len(drink_rounds), 2)
                self.assertGreater(meal["kcal"], max(m["kcal"] for m in self.kids
                                                     if m is not meal))

    def test_kids_meals_are_sorted_by_kcal_and_priced(self):
        kcals = [m["kcal"] for m in self.kids]
        self.assertEqual(kcals, sorted(kcals))
        for meal in self.kids:
            with self.subTest(meal=meal["name"]):
                self.assertIsNotNone(meal["price"])
                self.assertGreater(meal["price"], 0)


class TestFindDeals(EngineCase):
    def test_saving_compares_only_covered_items(self):
        """假优惠的坑：拿套餐价跟**全部**想点的东西比，会算出巨额省钱。"""
        deals = self.engine.find_deals(["麦辣鸡腿汉堡", "薯条", "可乐"], top=3)
        self.assertTrue(deals)
        for deal in deals:
            with self.subTest(combo=deal["combo_name"]):
                self.assertEqual(deal["saving"], deal["single_price"] - deal["combo_price"])
                for entry in deal["covered_priced"]:
                    self.assertGreater(entry["price"], 0)

    def test_covered_items_really_are_in_the_combo(self):
        names = ["麦辣鸡腿汉堡", "薯条"]
        for deal in self.engine.find_deals(names, top=3):
            with self.subTest(combo=deal["combo_name"]):
                joined = " ".join(deal["covered"])
                for name in names:
                    if name not in deal["missing"]:
                        self.assertIn(name[:2], joined)

    def test_no_deals_for_unknown_items(self):
        self.assertEqual(self.engine.find_deals(["外星人套餐"]), [])

    def test_unpriced_covered_items_are_disclosed(self):
        """套餐专用的 SKU 单点不到价，必须如实标出来，不能当成 0。"""
        for deal in self.engine.find_deals(["麦辣鸡腿汉堡", "薯条", "可乐"], top=3):
            for name in deal["covered_unpriced"]:
                with self.subTest(combo=deal["combo_name"], name=name):
                    self.assertNotIn(name, [d["name"] for d in deal["covered_priced"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
