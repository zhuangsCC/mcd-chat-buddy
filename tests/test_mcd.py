#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""点餐入口（mcd.py）的单元测试 —— 不联网、不下单。

重点守两件事：

1. **口语能不能对上菜单。** 用户说「香辣鸡腿堡」，菜单上写「麦辣鸡腿汉堡」；
   说「麦旋风」，菜单上有四款带这个后缀的。这一层对不上，整个东西就没用。
2. **图不能丢、热量不能编。** 用户明确要求过图片必须有；热量拿不到就写「暂无」。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from mcd import (  # noqa: E402
    TIE_RATIO, combo_hint, combo_items, kcal_text, match, render_menu, score,
    store_line,
)
from menu_engine import Product  # noqa: E402


def product(name, price=20.0, kcal=300, image="", estimated=False, variants=None,
            combos=False):
    return Product(code=name, name=name, price=price, kcal=kcal, image=image,
                   kcal_estimated=estimated, variants=variants or [], combos=combos)


REAL_MENU = [
    "麦辣鸡腿汉堡", "板烧鸡腿堡", "巨无霸", "麦香鸡", "麦香鱼",
    "双层深海鳕鱼堡", "经典麦旋风", "蓝莓爆爆珠麦旋风", "柠檬脆脆麦旋风",
    "薯条", "可乐", "玉米杯", "香芋派", "麦乐鸡", "圆筒冰淇淋",
]


class TestScore(unittest.TestCase):
    def test_exact_beats_everything(self):
        self.assertEqual(score("薯条", "薯条"), 1.0)

    def test_substring_is_high(self):
        """菜单「薯条」对营养表「中薯条」这种情况靠的就是包含关系。"""
        self.assertEqual(score("麦旋风", "经典麦旋风"), 0.9)

    def test_colloquial_name_matches(self):
        """「香辣鸡腿堡」对上「麦辣鸡腿汉堡」（重了辣/鸡/腿/堡四个字），
        且明显高于「板烧鸡腿堡」（只重三个）。"""
        real = score("香辣鸡腿堡", "麦辣鸡腿汉堡")
        other = score("香辣鸡腿堡", "板烧鸡腿堡")
        self.assertGreater(real, other)
        self.assertGreater(real, 0.4)

    def test_one_shared_char_is_not_a_match(self):
        """只重一个字多半是巧合 —— 「香辣鸡腿堡」和「香芋派」只共一个「香」。"""
        self.assertLess(score("香辣鸡腿堡", "香芋派"), 0.35)

    def test_empty_is_zero(self):
        self.assertEqual(score("", "薯条"), 0.0)
        self.assertEqual(score("薯条", ""), 0.0)


class TestMatch(unittest.TestCase):
    def setUp(self):
        self.products = [product(n) for n in REAL_MENU]

    def test_picks_the_colloquial_hit(self):
        hits = match("香辣鸡腿堡", self.products)
        self.assertEqual(hits[0].name, "麦辣鸡腿汉堡")

    def test_short_name_wins_on_tie(self):
        """「麦旋风」四款都含这三个字、分数一样 —— 该落到不带修饰的经典款。"""
        hits = match("麦旋风", self.products)
        self.assertEqual(hits[0].name, "经典麦旋风")

    def test_tie_lists_the_alternatives(self):
        """并列的都要给出来，用户才知道还有别的口味。"""
        names = [p.name for p in match("麦旋风", self.products)]
        self.assertIn("经典麦旋风", names)
        self.assertTrue(any("麦旋风" in n and n != "经典麦旋风" for n in names))

    def test_no_hit_is_empty(self):
        self.assertEqual(match("佛跳墙", self.products), [])

    def test_tie_ratio_is_sane(self):
        self.assertGreater(TIE_RATIO, 0)
        self.assertLessEqual(TIE_RATIO, 1)

    def test_combo_never_shadows_single(self):
        """用户没说套餐，¥23 的单品不能被 ¥28 的四件套顶掉。

        实测踩过的坑：「麦辣鸡腿堡」是「麦辣鸡腿堡四件套」的子串（0.9 分），
        真单品「麦辣鸡腿汉堡」只有模糊分 —— 所以没提套餐字眼时套餐要打对折。
        """
        products = [product("麦辣鸡腿堡四件套", price=28.0, combos=True),
                    product("麦辣鸡腿汉堡", price=23.0)]
        hits = match("麦辣鸡腿堡", products)
        self.assertEqual(hits[0].name, "麦辣鸡腿汉堡")

    def test_combo_still_wins_when_asked(self):
        """说了「四件套」就该给套餐 —— 惩罚只针对没提套餐的情况。"""
        products = [product("麦辣鸡腿堡四件套", price=28.0, combos=True),
                    product("麦辣鸡腿汉堡", price=23.0)]
        hits = match("麦辣鸡腿堡四件套", products)
        self.assertEqual(hits[0].name, "麦辣鸡腿堡四件套")


class TestKcalText(unittest.TestCase):
    def test_unknown_is_never_zero(self):
        """最要紧的一条：不知道就是不知道，绝不能念成 0 大卡。"""
        text = kcal_text(product("龙焰鸡腿堡", kcal=None))
        self.assertEqual(text, "热量暂无")
        self.assertNotIn("0", text)

    def test_estimated_is_marked(self):
        self.assertEqual(kcal_text(product("薯条", kcal=289, estimated=True)),
                         "289 大卡（按默认规格估）")

    def test_variants_are_spelled_out(self):
        """「经典麦旋风」是选口味的 —— 只报一个数字会让人以为就那一种。"""
        text = kcal_text(product("经典麦旋风", kcal=266, estimated=True, variants=[
            {"name": "奥利奥麦旋风", "kcal": 266, "default": True},
            {"name": "草莓麦旋风", "kcal": 295, "default": False},
        ]))
        self.assertIn("奥利奥麦旋风 266", text)
        self.assertIn("草莓麦旋风 295", text)
        self.assertIn("（默认）", text)


class TestRender(unittest.TestCase):
    def test_images_are_included(self):
        """用户原话：「不管怎么样，图片都要报上来的」。

        WorkBuddy 会拦截 <img> 标签（白屏）—— 图必须用 Markdown 语法。
        """
        rows = [{"ask": "薯条", "product": product("薯条", image="https://x/1.png"),
                 "cents": 1400}]
        rendered = render_menu({}, rows)
        self.assertIn("![薯条](https://x/1.png)", rendered)
        self.assertNotIn("<img", rendered)

    def test_image_lives_in_its_own_column(self):
        """多件时图嵌进「官方实物图」列，防大图撑屏。"""
        rows = [{"ask": "a", "product": product("a", image="https://x/a.png"), "cents": 1000},
                {"ask": "b", "product": product("b", image="https://x/b.png"), "cents": 2000}]
        rendered = render_menu({}, rows)
        self.assertIn("官方实物图", rendered)
        self.assertIn("| a | a | ![a](https://x/a.png) |", rendered)
        self.assertNotIn("<img", rendered)

    def test_total_is_summed(self):
        rows = [{"ask": "a", "product": product("a"), "cents": 2300},
                {"ask": "b", "product": product("b"), "cents": 1400}]
        self.assertIn("**合计 ¥37.0**", render_menu({}, rows))

    def test_store_line_omits_missing_bits(self):
        self.assertEqual(store_line({"name": "某店"}), "某店")
        self.assertIn("309 米", store_line({"name": "某店", "distance": 309}))

    def test_combo_kcal_override(self):
        """`want` 点到套餐时热量来自拆解相加 —— 不许显示「热量暂无」。"""
        rows = [{"ask": "麦辣鸡腿堡四件套",
                 "product": product("麦辣鸡腿堡四件套", kcal=None, combos=True),
                 "cents": 2800, "kcal_text": "1091 大卡"}]
        rendered = render_menu({}, rows)
        self.assertIn("1091 大卡", rendered)
        self.assertNotIn("热量暂无", rendered)


class TestComboHint(unittest.TestCase):
    """多件购买时比一句价：套餐更便宜就提示，换不换用户定。"""

    def _engine(self, price=28.0, parts=("麦辣鸡腿汉堡", "中薯条", "麦乐鸡4块", "可乐中杯")):
        class _Combo:
            rounds: list = []

        class Engine:
            def combo_catalog(self):
                return [Product(code="c1", name="麦辣鸡腿堡四件套",
                                price=price, combos=True)]

            def combo_detail(self, code):
                return _Combo()

            def combo_breakdown(self, combo):
                return {"parts": [{"name": p} for p in parts],
                        "kcal_text": "1091 大卡"}

        return Engine()

    @staticmethod
    def _rows(*specs):
        return [{"ask": n, "product": product(n), "quantity": q,
                 "single_cents": cents, "cents": cents * q}
                for n, cents, q in specs]

    def test_cheaper_combo_is_suggested(self):
        # 1 汉堡 + 1 可乐 = ¥34；一份四件套 ¥28 全都含 → 提示省 ¥6
        hint = combo_hint(self._engine(),
                          self._rows(("麦辣鸡腿汉堡", 2300, 1), ("可乐", 1100, 1)))
        self.assertIn("麦辣鸡腿堡四件套", hint)
        self.assertIn("¥28.0", hint)
        self.assertIn("¥6.0", hint)
        self.assertIn("中薯条", hint)          # 多出来的东西必须点明

    def test_quantity_multiplies_the_price(self):
        # 2 汉堡 + 2 可乐 = ¥68；两份四件套 = ¥56 → 省 ¥12
        hint = combo_hint(self._engine(),
                          self._rows(("麦辣鸡腿汉堡", 2300, 2), ("可乐", 1100, 2)))
        self.assertIn("¥56.0", hint)
        self.assertIn("¥12.0", hint)

    def test_no_hint_for_single_item(self):
        self.assertEqual(
            combo_hint(self._engine(), self._rows(("麦辣鸡腿汉堡", 2300, 1))), "")

    def test_no_hint_when_combo_is_pricier(self):
        hint = combo_hint(self._engine(price=40.0),
                          self._rows(("麦辣鸡腿汉堡", 2300, 1), ("可乐", 1100, 1)))
        self.assertEqual(hint, "")

    def test_no_hint_when_combo_covers_nothing(self):
        # 套餐里没有可乐 —— 覆盖不了，闭嘴
        hint = combo_hint(self._engine(parts=("麦辣鸡腿汉堡", "中薯条")),
                          self._rows(("麦辣鸡腿汉堡", 2300, 1), ("可乐", 1100, 1)))
        self.assertEqual(hint, "")

    def test_uneven_quantities_are_skipped(self):
        """2 汉堡 1 可乐这种，换套餐账算不平 —— 不比。"""
        hint = combo_hint(self._engine(),
                          self._rows(("麦辣鸡腿汉堡", 2300, 2), ("可乐", 1100, 1)))
        self.assertEqual(hint, "")


class TestComboItems(unittest.TestCase):
    """套餐下单必须带 roundList（每一档选什么）—— 漏了会被接口拒掉。"""

    class _Combo:
        rounds = [
            {"id": 1, "choices": [{"code": "a", "isDefault": 1},
                                  {"code": "b", "isDefault": 0}]},
            {"id": 2, "choices": [{"code": "c", "isDefault": 0}]},   # 没有默认项
        ]

    def test_defaults_are_used(self):
        class Engine:
            def combo_detail(self, code):
                return TestComboItems._Combo()

        item = combo_items(Engine(), product("某套餐"))
        self.assertEqual(item["productCode"], "某套餐")
        self.assertEqual(item["quantity"], 1)
        self.assertEqual(len(item["roundList"]), 2)
        self.assertEqual(item["roundList"][0]["round"], "1")
        self.assertEqual(item["roundList"][0]["comboItemList"][0]["code"], "a")
        # 那一档没有默认项时取第一个，**不能整档丢掉** —— 丢掉就是少给用户一样东西
        self.assertEqual(item["roundList"][1]["comboItemList"][0]["code"], "c")

    def test_no_rounds_falls_back_to_plain_item(self):
        class Engine:
            def combo_detail(self, code):
                return None

        item = combo_items(Engine(), product("单品"))
        self.assertNotIn("roundList", item)


if __name__ == "__main__":
    unittest.main(verbosity=2)
