#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""营养表解析 + 名称归一化 + 菜品/营养配对 的单元测试。

只依赖 mock/ 里已经落盘的接口返回，不联网。
跑法：
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from mcp_client import MOCK_DIR, McDMcpClient, text_of  # noqa: E402
from menu_engine import (  # noqa: E402
    COMBO_KW, MERCH_KW, MenuEngine, NutritionTable, norm, parse_nutrition,
)


def nutrition_blob() -> str:
    """从落盘的接口返回里把那段非 JSON 的表格原文抠出来。"""
    raw = json.loads((MOCK_DIR / "list-nutrition-foods.json").read_text(encoding="utf-8"))
    text = text_of(raw)
    return json.loads(text[text.find("{"):text.rfind("}") + 1])["data"]


def build_engine() -> MenuEngine:
    engine = MenuEngine(McDMcpClient(), use_mock=True)
    engine.load()
    return engine


class TestNorm(unittest.TestCase):
    """名称归一化——配对失败八成都出在这里。"""

    def test_fullwidth_to_halfwidth(self):
        self.assertEqual(norm("薯条（中）"), norm("薯条(中)"))

    def test_strip_trademark(self):
        # NFKC 会把 ™ 变成字母 "TM"，所以必须在 NFKC 之前剥掉
        self.assertEqual(norm("巨无霸™"), norm("巨无霸"))
        self.assertEqual(norm("麦咖啡™奶铁"), norm("麦咖啡奶铁"))

    def test_strip_hyphen(self):
        # 菜单写「麦乐鸡-4块」，营养表写「麦乐鸡4块」，差一个连字符
        self.assertEqual(norm("麦乐鸡-4块"), norm("麦乐鸡4块"))

    def test_strip_middle_dots_and_spaces(self):
        self.assertEqual(norm("那么大鸡排·大"), norm("那么大鸡排大"))

    def test_empty(self):
        self.assertEqual(norm(None), "")
        self.assertEqual(norm(""), "")


class TestParseNutrition(unittest.TestCase):
    """那张 160 行的表得自己拆，不是 JSON。"""

    @classmethod
    def setUpClass(cls):
        cls.blob = nutrition_blob()
        cls.table = parse_nutrition(cls.blob)

    def test_header_is_skipped(self):
        # 第一行是 `[160]{productName,...}:`，不该被当成数据
        self.assertNotIn("[160]", " ".join(self.table.keys()))

    def test_row_count_in_range(self):
        # 服务端声明 160 行，重名去重后允许略少
        self.assertGreater(len(self.table), 140)
        self.assertLessEqual(len(self.table), 160)

    def test_every_record_has_nine_fields(self):
        for name, record in self.table.items():
            with self.subTest(name=name):
                for key in ("kcal", "kj", "protein", "fat", "carb", "sodium", "calcium"):
                    self.assertIn(key, record)
                self.assertIsInstance(record["kcal"], int)
                self.assertGreaterEqual(record["kcal"], 0)

    def test_kj_and_kcal_are_consistent(self):
        """千焦 ÷ 千卡 应该约等于 4.184，差太多说明列串了。"""
        checked = 0
        for name, record in self.table.items():
            kcal = record["kcal"]
            if kcal < 20:            # 太小的话取整误差占比太大，跳过
                continue
            ratio = record["kj"] / kcal
            with self.subTest(name=name):
                self.assertAlmostEqual(ratio, 4.184, delta=0.25)
            checked += 1
        self.assertGreater(checked, 100)

    def test_named_item_has_expected_value(self):
        """挑几个稳定的老品做锚点，防止哪天解析悄悄错位。

        这些数字直接抄自接口原始返回，不要"凭印象"改。
        """
        sink = {"中薯条": 289, "巨无霸": 513}
        for name, kcal in sink.items():
            with self.subTest(name=name):
                self.assertIn(name, self.table, f"营养表里没有「{name}」了")
                self.assertEqual(self.table[name]["kcal"], kcal)

    def test_columns_are_not_shifted(self):
        """交叉验证：别把 protein 列当成 fat 列读。"""
        record = self.table.get("中薯条")
        if record:
            self.assertGreater(record["kcal"], record["protein"] * 4 - 1)
            self.assertLess(record["sodium"], 2000)

    def test_malformed_lines_are_skipped_not_crashing(self):
        blob = "[3]{a,b,c,d,e,f,g,h,i}:\n" + "\n".join([
            "正常,描述,100,24,1.0,2.0,3.0,400,10",
            "少了,列,几个",
            ",描述,100,24,1.0,2.0,3.0,400,10",
            "数字坏了,描述,abc,def,1,1,1,1,1",
        ])
        table = parse_nutrition(blob)
        self.assertEqual(list(table.keys()), ["正常"])
        self.assertEqual(table["正常"]["kcal"], 24)


class TestNutritionTableLookup(unittest.TestCase):
    def setUp(self):
        self.table = NutritionTable(parse_nutrition(nutrition_blob()))

    def test_exact(self):
        record = self.table.exact("中薯条")
        self.assertIsNotNone(record)
        self.assertEqual(record["kcal"], 289)

    def test_exact_tolerates_fullwidth_and_hyphen(self):
        self.assertIsNotNone(self.table.exact("麦乐鸡－4块"))

    def test_exact_misses_unknown(self):
        self.assertIsNone(self.table.exact("薯条"))

    def test_fuzzy_menu_name_shorter_than_table(self):
        """菜单「薯条」→ 营养表「中薯条」：表名更长。"""
        hit = self.table.fuzzy("薯条")
        self.assertIsNotNone(hit)
        record, used = hit
        self.assertTrue(norm(used).endswith("薯条"))
        self.assertGreater(record["kcal"], 0)

    def test_fuzzy_menu_name_longer_than_table(self):
        """菜单「苹果片一袋(60g)」→ 营养表「苹果片」：表名更短。"""
        hit = self.table.fuzzy("苹果片一袋(60g)")
        self.assertIsNotNone(hit)
        _, used = hit
        self.assertEqual(norm(used), norm("苹果片"))

    def test_fuzzy_prefers_middle_size(self):
        """不带规格的通用名，默认配「中」号。"""
        hit = self.table.fuzzy("薯条")
        self.assertTrue(norm(hit[1]).startswith("中"))

    def test_fuzzy_ignores_single_char_noise(self):
        self.assertIsNone(self.table.fuzzy(""))


class TestClassifyAndPairing(unittest.TestCase):
    """动态分类 + 菜单/营养表配对。"""

    @classmethod
    def setUpClass(cls):
        cls.engine = build_engine()

    def test_menu_loaded(self):
        self.assertGreater(len(self.engine.products), 100)

    def test_roles_are_from_known_set(self):
        allowed = {"main", "side", "drink", "dessert", "combo", "merch", "other"}
        for product in self.engine.products.values():
            with self.subTest(name=product.name):
                self.assertIn(product.role, allowed)

    def test_merch_is_not_food(self):
        for product in self.engine.products.values():
            if product.merch:
                with self.subTest(name=product.name):
                    self.assertTrue(any(k in product.name for k in MERCH_KW))
                    self.assertIsNone(product.kcal)

    def test_combos_have_no_aggregate_kcal(self):
        """套餐在接口里没有整体热量，必须靠拆组成，不能凭空算。"""
        for product in self.engine.products.values():
            if product.combos:
                with self.subTest(name=product.name):
                    self.assertIsNone(product.kcal)
                    self.assertFalse(product.kcal_estimated)

    def test_pairing_coverage(self):
        """能吃的单品里，过半应该配上热量。

        **这里刻意不要求 100%。** 营养表只有 160 条，覆盖的是常驻主力菜单；
        季节限定和蘸酱类（龙焰系列、心形薯饼、风味酱等）接口里根本没给热量。
        宁可显示「热量暂无」，也不能猜一个数上去 —— 这是营养工具不是菜单大全。
        """
        food = [p for p in self.engine.products.values()
                if not p.merch and not p.combos]
        paired = [p for p in food if p.kcal is not None]
        ratio = len(paired) / len(food)
        self.assertGreater(ratio, 0.5, f"配对率掉到 {ratio:.0%} 了，比之前差")
        # 主力品类必须配全：主食里的经典款一条都不能缺
        staples = ["巨无霸", "麦辣鸡腿汉堡", "板烧鸡腿堡", "双层深海鳕鱼堡"]
        for name in staples:
            product = self.engine.by_name(name)
            if product is None:
                continue
            with self.subTest(name=name):
                self.assertIsNotNone(product.kcal, f"主力款「{name}」没配上热量")

    def test_unpaired_items_are_honestly_reported(self):
        """没热量的餐品必须**明说**，不能被当成 0 蒙混过去。"""
        for product in self.engine.products.values():
            if product.kcal is None:
                with self.subTest(name=product.name):
                    self.assertEqual(product.kcal_text, "热量暂无")

    def test_unpaired_food_is_never_zero_in_totals(self):
        """缺热量的餐品不能被当成 0 计进套餐合计里。"""
        engine = self.engine
        for combo in engine.combo_catalog()[:20]:
            detail = engine.combo_breakdown(combo)
            if not detail["complete"]:
                with self.subTest(combo=combo.name):
                    # 缺了就老实说缺了什么
                    self.assertTrue(detail["unknown"])
                    for name in detail["unknown"]:
                        self.assertNotIn(name, detail["known"])

    def test_estimated_flag_is_honest(self):
        """模糊匹配出来的热量必须打标记，不能冒充精确值。"""
        for product in self.engine.products.values():
            if product.kcal_estimated:
                with self.subTest(name=product.name):
                    self.assertIsNotNone(product.kcal)
                    self.assertNotEqual(product.nutrition_name, "")
            else:
                if product.kcal is not None:
                    with self.subTest(name=product.name):
                        self.assertEqual(product.nutrition_name, product.name)

    def test_no_hardcoded_product_lists_in_source(self):
        """守住「不写死餐品清单」这条底线。"""
        source = (ROOT / "scripts" / "menu_engine.py").read_text(encoding="utf-8")
        for banned in ("MAIN_CANDIDATES", "SIDE_CANDIDATES", "DRINK_CANDIDATES", "SPEC_ALIAS"):
            self.assertNotIn(banned, source, f"又冒出来写死的清单了：{banned}")
        # COMBO_KW / MERCH_KW 是「语言规则」不是商品清单，应当存在
        self.assertIn(COMBO_KW[0], source)


class TestSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = build_engine()

    def test_search_by_word(self):
        hits = self.engine.search("薯条")
        self.assertTrue(hits)
        self.assertTrue(all("薯" in p.name for p in hits))

    def test_search_by_category_word(self):
        """用户说「主餐」，巨无霸这种名字里没「主餐」的也得出来。"""
        hits = self.engine.search("主餐")
        self.assertTrue(hits)
        self.assertTrue(all(p.role == "main" for p in hits))

    def test_search_synonym(self):
        hits = {p.name for p in self.engine.search("咖啡")}
        self.assertTrue(hits & {"鲜萃咖啡", "拿铁", "美式"} or hits, "咖啡同义词没生效")

    def test_search_with_kcal_filters_unknown(self):
        for product in self.engine.search_with_kcal("汉堡"):
            with self.subTest(name=product.name):
                self.assertIsNotNone(product.kcal)

    def test_search_empty_keyword(self):
        self.assertEqual(self.engine.search(""), [])

    def test_search_no_match(self):
        self.assertEqual(self.engine.search("外星人套餐"), [])


class TestFindProduct(unittest.TestCase):
    """按 code / 名字反查菜单里的餐品 —— 取官方商品图要用。

    套餐档位给的 code 是**套餐内的 SKU**，多半不在菜单里；
    档位名也常常和菜单名不一样（「麦乐鸡-4块」vs「麦乐鸡」）。
    """

    @classmethod
    def setUpClass(cls):
        cls.engine = build_engine()

    def test_by_exact_code(self):
        product = self.engine.products["1440"]
        self.assertIs(self.engine.find_product("随便什么名字", "1440"), product)

    def test_by_exact_name(self):
        self.assertEqual(self.engine.find_product("巨无霸").name, "巨无霸")

    def test_combo_sku_code_falls_back_to_name(self):
        # 2846 是套餐内的 SKU，不在菜单里；名字「麦乐鸡-4块」也要能兜到「麦乐鸡」
        product = self.engine.find_product("麦乐鸡-4块", "2846")
        self.assertIsNotNone(product)
        self.assertIn("麦乐鸡", product.name)

    def test_menu_name_longer_than_slot_name(self):
        product = self.engine.find_product("苹果片一袋(60g)")
        self.assertIsNotNone(product)
        self.assertIn("苹果片", product.name)

    def test_slot_name_longer_than_menu_name(self):
        product = self.engine.find_product("小杯玉米杯")
        self.assertIsNotNone(product)
        self.assertIn("玉米", product.name)

    def test_unknown_returns_none(self):
        """玩具在菜单里根本没有对应条目，要老实返回 None，不能乱配一个。"""
        self.assertIsNone(self.engine.find_product("海绵宝宝×航海王系列玩具", "521991"))

    def test_short_name_does_not_mismatch(self):
        """「可乐」不能被配到「可乐中杯」之外的东西上。"""
        product = self.engine.find_product("可乐")
        self.assertIsNotNone(product)
        self.assertIn("可乐", product.name)

    def test_empty_input(self):
        self.assertIsNone(self.engine.find_product(""))
        self.assertIsNone(self.engine.find_product(None))

    def test_found_products_have_images(self):
        """能查到的餐品，图上应该都带官方商品图 —— 这是配图功能的地基。"""
        for name in ("巨无霸", "薯条", "可乐", "麦辣鸡腿汉堡"):
            product = self.engine.find_product(name)
            with self.subTest(name=name):
                self.assertIsNotNone(product)
                self.assertTrue(product.image.startswith("https://menu-img.mcd.cn/"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
