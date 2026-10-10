#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦麦懂你 (mcd-chat-buddy) · 菜单智能引擎

设计原则（重要）
----------------
**不写死任何餐品清单。** 所有候选都从麦当劳接口实时拉取，动态推断。
唯一保留的"知识"是三类语言规则，不是商品列表：

1. `MERCH_KW` 等关键词：用于把餐品**归类**（主食用「堡/卷」，周边用「毛绒/帽」）。
   任何人加新品，不需要改代码。
2. `SIZE_DEFAULT`：菜单给「薯条」这种不带规格的通用名，营养表只有「中薯条/大薯条」。
   接口没提供对应关系，只能按惯例默认取中杯/中份。**这是唯一的兜底，且会在输出里标注。**
3. 忌口完全动态：不预设"洋葱"这类词，而是**实时读该餐品的特制项**
   （`modification.items[].values[]`），用户说不吃什么就现查能不能去掉。

用法（作为库）
--------------
    from menu_engine import MenuEngine
    engine = MenuEngine(client)
    engine.load(city="××市")
    engine.recommend(wants=["汉堡", "薯条", "可乐"], budget=45, kcal=700, avoid=["辣", "洋葱"])
"""

from __future__ import annotations

import json
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_client import MOCK_DIR, McDMcpClient, structured_of, text_of  # noqa: E402

# 官方图只认这一个域名
ALLOWED_HOSTS = ("menu-img.mcd.cn",)


def clean_image_url(raw) -> str:
    """把接口给的图 URL 洗干净，并挡住非官方域名。

    实测坑：原始返回里是 `https:\\/\\/menu-img.mcd.cn\\/...png`，
    解析后一般是干净的，但保险起见把反斜杠和多余空白都去掉。

    域名用 urlparse 比 **hostname**，不能用子串判断 ——
    `menu-img.mcd.cn.evil.com` 是包含 `menu-img.mcd.cn` 的，子串判断会放它过去。
    """
    if not raw:
        return ""
    url = str(raw).replace("\\", "").strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https"):
        return ""
    if (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        return ""
    return url


# --------------------------------------------------------------------------
# 语言规则（不是商品清单）
# --------------------------------------------------------------------------

# 非食品：周边、玩具、礼品卡、联名收藏品
MERCH_KW = ("周边", "毛绒", "帽", "捶", "痒痒挠", "盲盒", "徽章", "玩具", "礼品卡", "公仔")

# 套餐：名字里带这些的是组合商品，本身没有整体热量，要拆开算
COMBO_KW = ("套餐", "件套", "随心配", "双人餐", "分享餐", "乐园餐", "宫格",
            "单品精选", "买一送一", "拼", "任选", "盒")

DRINK_KW = ("可乐", "雪碧", "咖啡", "锡兰", "奶茶", "牛奶", "纯悦", "水", "果汁", "苹果汁",
            "豆浆", "奶铁", "美式", "卡布", "拿铁", "怡泉", "麦炫酷", "雪冰", "阿芙佳朵",
            "红茶", "热朱古力", "阳光橙", "橙橙")
DESSERT_KW = ("新地", "麦旋风", "圆筒", "冰淇淋", "派", "阿芙佳朵")
SIDE_KW = ("薯条", "薯饼", "麦乐鸡", "鸡翅", "鸡块", "鸡柳", "玉米杯", "苹果片",
           "芝士条", "扭扭薯", "鸡排", "脆汁鸡", "笋卷")
MAIN_KW = ("堡", "卷", "汉堡")

# 口语 → 品类角色。用户说「汉堡」时，巨无霸/麦香鸡名字里没有「汉堡」两字，
# 但它们是主餐，应该一起给出来 —— 这是店员的常识，不是商品清单。
CATEGORY_MAP = {
    "汉堡": "main", "堡": "main", "汉堡包": "main", "主食": "main", "主餐": "main", "正餐": "main",
    "小食": "side", "小吃": "side", "配餐": "side", "炸物": "side", "配菜": "side", "配搭": "side",
    "饮料": "drink", "饮品": "drink", "喝的": "drink", "汽水": "drink", "水": "drink", "杯": "drink",
    "甜品": "dessert", "甜点": "dessert", "冰淇淋": "dessert", "冰": "dessert",
}

# 口语同义词：说「薯条」也可能想要薯饼、扭扭薯（都是薯制品）
SYNONYM = {
    "薯条": ("薯条", "薯饼", "扭扭薯"),
    "薯": ("薯",),
    "鸡": ("鸡",),
    "卷": ("卷",),
    "咖啡": ("咖啡", "奶铁", "美式", "卡布", "拿铁"),
    "牛奶": ("牛奶", "燕麦奶"),
}

# 规格兜底：菜单通用名 → 营养表里的规格名后缀偏好（按优先级）
SIZE_DEFAULT = ("中", "标准")   # 「薯条」优先配「中薯条」
# 用来**认出**一个营养表名带规格前缀 —— 跟 SIZE_DEFAULT 是两回事：
# 前者是「优先选谁」，这个是「哪些算带规格」。迷你不该进偏好表（见 _match_key）。
SIZE_PREFIX = ("迷你", "小", "中", "大", "标准")

# 套餐详情缓存：套餐结构变化不频繁，缓存下来省几十次请求
COMBO_CACHE = MOCK_DIR / "combo-catalog.json"          # mock 模式的离线数据（进仓库，只读）
COMBO_CACHE_USER = Path.home() / ".mcd-combo-cache.json"  # 真实模式的缓存（用户级，不进仓库）


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass
class Product:
    code: str
    name: str
    price: float | None
    category: str = ""
    combos: bool = False          # 是否套餐
    merch: bool = False           # 是否周边
    role: str = "other"           # main / side / drink / dessert / other
    image: str = ""               # 官方商品图 URL（menu-img.mcd.cn）
    kcal: int | None = None
    nutrition: dict | None = None
    nutrition_name: str = ""      # 实际命中的营养表条目名（模糊匹配时会和 name 不同）
    kcal_estimated: bool = False  # True 表示热量是按默认规格估的

    # 「同物多口味」容器解引用出来的口味列表（见 MenuEngine.variant_choices）。
    # 例：「经典麦旋风」挂的是口味集合，实际卖的是奥利奥 / 草莓。
    # 每项 {code, name, kcal, default, non_food}。
    variants: list = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.name}（¥{self.price:.1f}）" if self.price is not None else self.name

    @property
    def kcal_text(self) -> str:
        if self.kcal is None:
            return "热量暂无"
        return f"{self.kcal} 大卡" + ("（按默认规格估）" if self.kcal_estimated else "")

    @property
    def variant_text(self) -> str:
        """口味怎么念。没有口味（或一个口味都没收录热量）就返回空串。

        不写「经典麦旋风 266 大卡」这么省事的说法 —— 它其实是**让你选口味**的，
        说是奥利奥还是草莓，用户才知道自己吃到的是哪一个。
        """
        rows = [v for v in self.variants if v.get("kcal") is not None]
        if not rows:
            return ""
        parts = []
        for row in rows:
            mark = "（默认）" if row.get("default") else ""
            parts.append(f"{row['name']} {row['kcal']}{mark}")
        return " / ".join(parts)


@dataclass
class Combo:
    code: str
    name: str
    price: float | None
    rounds: list = field(default_factory=list)   # 原始 rounds


# --------------------------------------------------------------------------
# 营养表
# --------------------------------------------------------------------------


def parse_nutrition(raw_data: str) -> dict[str, dict]:
    """解析 `list-nutrition-foods` 那段非 JSON 的表格文本。

    格式：`[160]{列名,...}:` 换行，之后每行 9 个逗号分隔字段。
    """
    table: dict[str, dict] = {}
    for line in raw_data.split("\n")[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 9:
            continue
        name = parts[0]
        if not name:
            continue
        try:
            record = {
                "kcal": int(parts[3]),
                "kj": int(parts[2]),
                "protein": float(parts[4]),
                "fat": float(parts[5]),
                "carb": float(parts[6]),
                "sodium": int(parts[7]),
                "calcium": int(parts[8]),
            }
        except ValueError:
            continue
        # 重名时保留第一条（表里有重复行）
        table.setdefault(name, record)
    return table


def norm(text) -> str:
    """统一全角半角、去掉商标符号和连字符，用于名称比对。

    三个坑都在这里踩过：
    - 连字符：菜单写「麦乐鸡-4块」，营养表写「麦乐鸡4块」
    - 商标号：NFKC 会把 ™ 变成两个字母 "TM"，所以必须在 NFKC **之前**剥掉
    - None：直接 str(None) 会得到字符串 "None"
    """
    if text is None:
        return ""
    s = str(text)
    for ch in "™®©":
        s = s.replace(ch, "")
    s = unicodedata.normalize("NFKC", s)
    for ch in " -－·．.":
        s = s.replace(ch, "")
    return s


# 品牌前缀：菜单会在同一款商品上反复挂品牌名，营养表不挂。
# 这是**品牌名**不是商品名，剥离之后才比得上。
BRAND_PREFIX = ("麦咖啡", "麦当劳")


class NutritionTable:
    def __init__(self, records: dict[str, dict]) -> None:
        self.records = records
        self._norm_index = {norm(k): k for k in records}
        # 所有归一化名拼成一串，给 shares_fragment() 做快速子串判断。
        # 用 "|" 分隔：待查片段里不可能含 "|"（它来自商品名），所以不会跨条目误命中。
        self._haystack = "|".join(self._norm_index)

    def exact(self, name: str) -> dict | None:
        if name in self.records:
            return self.records[name]
        hit = self._norm_index.get(norm(name))
        return self.records[hit] if hit else None

    def shares_fragment(self, name: str, min_size: int = 2) -> bool:
        """名字里有没有哪一段（≥min_size 字）出现在**别的**营养表条目名里。

        这是给「值不值得为它打一次详情接口」做**廉价预筛**用的 ——
        营养表没收录的商品有 90 个，全量查详情实测 38 秒。
        「经典麦旋风」含「麦旋风」→ 营养表有「奥利奥麦旋风」→ 值得查一次。

        ⚠️ 预筛**不参与判断对错**：筛掉了也只是少查一次，不会写出错数据。
        所以它宁可宽松（多查几次）也不要漏。
        """
        key = norm(name)
        if len(key) < min_size:
            return False
        return any(key[i:i + size] in self._haystack
                   for size in range(min_size, min(len(key), 5) + 1)
                   for i in range(len(key) - size + 1))

    def fuzzy(self, name: str) -> tuple[dict, str] | None:
        """按规格兜底匹配。返回 (记录, 实际用的名字)。

        两个方向都要试：
          菜单「薯条」      → 营养表「中薯条」        （营养表名更长）
          菜单「苹果片一袋(60g)」→ 营养表「苹果片」     （菜单名更长）
        只做单向匹配会漏掉后者，我踩过这个坑。
        """
        key = norm(name)
        if not key:
            return None

        hit = self._match_key(key)
        if hit:
            return hit

        # 剥掉品牌前缀再试一次：「麦咖啡™美式」→「美式」→「热美式中杯」。
        # 只剥品牌名，不剥「经典/招牌」这类会改变商品身份的修饰词 —— 宁可留空也别配错。
        for prefix in BRAND_PREFIX:
            brand = norm(prefix)
            if key.startswith(brand) and len(key) > len(brand):
                hit = self._match_key(key[len(brand):])
                if hit:
                    return hit
        return None

    def _match_key(self, key: str) -> tuple[dict, str] | None:
        """对一个已经归一化的键做双向比对。"""
        if not key:
            return None

        # 正向：菜单名是营养表名的子串（营养表名更长）
        candidates = [(n, r) for n, r in self.records.items() if key in norm(n)]
        if candidates:
            for pref in SIZE_DEFAULT:
                for n, r in candidates:
                    if norm(n).startswith(pref):
                        return r, n
            # 偏好的规格都没有，就退回**不带规格前缀**的那一条。
            # 踩过：菜单「新地」¥12.5（标准新地，要选朱古力/草莓）被配到了
            # 「迷你朱古力新地」140 kcal —— 候选里第一个带前缀的恰是「迷你」。
            # 便宜的小份款不该在没别的选择时先于标准份胜出。
            bare = [(n, r) for n, r in candidates
                    if not any(norm(n).startswith(p) for p in SIZE_PREFIX)]
            if bare:
                return bare[0][1], bare[0][0]
            return candidates[0][1], candidates[0][0]

        # 反向：菜单名里含着营养表名。取最长的那个，避免「苹果」这种过短的误配。
        inner = [(n, r) for n, r in self.records.items()
                 if len(norm(n)) >= 2 and norm(n) in key]
        if inner:
            inner.sort(key=lambda item: -len(norm(item[0])))
            return inner[0][1], inner[0][0]
        return None


def _combo_kcal_text(total: int, unknown: list[str]) -> str:
    """套餐热量怎么念。缺了就说缺了，绝不把「不知道」念成「0 大卡」。"""
    if not unknown:
        return f"{total} 大卡"
    if not total:
        return f"热量暂无（{len(unknown)} 样营养表没收录）"
    return f"至少 {total} 大卡（还有 {len(unknown)} 样没收录，实际更高）"


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------


class MenuEngine:
    def __init__(self, client: McDMcpClient, store_code: str = "", use_mock: bool = False) -> None:
        self.client = client
        self.store_code = store_code
        self.use_mock = use_mock
        self.products: dict[str, Product] = {}
        self.combos: dict[str, Combo] = {}
        self.table = NutritionTable({})
        self._detail_cache: dict[str, dict] = {}
        self._variant_cache: dict[str, list[dict]] = {}
        self._price_cache: dict[str, int | None] = {}
        self._catalog_cache: list[Combo] | None = None
        self.warnings: list[str] = []
        self.last_reason: str = ""

    # -- 拉数据 -----------------------------------------------------------

    def _call(self, tool: str, args: dict):
        if self.use_mock:
            if tool == "query-meal-detail":
                return self._mock_detail(args.get("code"))
            path = MOCK_DIR / f"{tool}.json"
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
            raise RuntimeError(f"演示数据缺 {tool}")
        return self.client.call_tool(tool, args)

    @staticmethod
    def _mock_detail(code: str) -> dict:
        """演示模式下的餐品详情。

        mock 里只有几个样本，**必须按 code 取**。早期版本不管传什么 code 都返回
        同一个文件，结果「麦辣鸡腿汉堡」读到了「两件套」的详情 —— 数据串了还看不出来。
        """
        path = MOCK_DIR / "meal-details.json"
        if path.exists():
            try:
                table = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                table = {}
            body = table.get(str(code))
            if body:
                return {"structuredContent": {"code": 200, "data": body}}
        # 取不到就老实返回空，让上层走「演示数据里没有」的分支，别拿别人家的数据顶
        return {"structuredContent": {"code": 200, "data": {}}}

    def load(self, store_code: str | None = None, refresh: bool = False) -> None:
        if store_code:
            self.store_code = store_code

        cache_file = Path.home() / f".mcd-menu-cache-{self.store_code}.json" if self.store_code else None
        cached_data = None
        if cache_file and cache_file.exists() and not refresh and not self.use_mock:
            try:
                import time
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                # 15分钟缓存 (900秒)
                if time.time() - data.get("ts", 0) < 900:
                    cached_data = data
            except Exception:
                cached_data = None

        if cached_data:
            raw = cached_data.get("raw_meals") or {}
            raw_nut = cached_data.get("raw_nut") or {}
        else:
            # 菜单
            raw = structured_of(self._call("query-meals", {
                "storeCode": self.store_code, "orderType": 1, "beType": 1,
            })) or {}
            raw_nut = self._call("list-nutrition-foods", {})
            if cache_file and not self.use_mock:
                try:
                    import time
                    cache_file.write_text(json.dumps({
                        "ts": time.time(),
                        "raw_meals": raw,
                        "raw_nut": raw_nut,
                    }, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass

        body = raw.get("data") or {}
        meals = body.get("meals") or {}
        for category in body.get("categories") or []:
            cat_name = category.get("name") or ""
            for item in category.get("meals") or []:
                code = item.get("code")
                info = meals.get(code) or {}
                if not code or not info.get("name"):
                    continue
                product = Product(
                    code=code,
                    name=info["name"],
                    price=float(info["currentPrice"]) if str(info.get("currentPrice", "")).replace(".", "").isdigit() else None,
                    category=cat_name,
                    image=clean_image_url(info.get("image")),
                )
                self.products[code] = product

        # 营养表
        nut_body = structured_of(raw_nut) or {}
        blob = nut_body.get("data")
        if not isinstance(blob, str):
            text = text_of(raw_nut)
            start, end = text.find("{"), text.rfind("}")
            blob = json.loads(text[start:end + 1]).get("data", "") if start != -1 else ""
        self.table = NutritionTable(parse_nutrition(blob))

        self._classify()

    # 类目 → 角色的兜底规则。只在**名字判不出来**时才用，名字规则优先。
    # 这是语言规则，不是商品清单 —— 新品只要类目名字正常就能归位。
    CATEGORY_ROLE = (
        ("main", ("堡", "汉堡", "卷")),
        ("side", ("炸鸡",)),
        ("drink", ("饮品", "咖啡")),
    )

    @staticmethod
    def _role_from_category(category) -> str:
        """用接口给的类目名兜底判角色。判不出来就还是 other。"""
        text = " ".join(str(category or "").split())
        for role, keywords in MenuEngine.CATEGORY_ROLE:
            if any(k in text for k in keywords):
                return role
        return "other"

    def _classify(self) -> None:
        """动态推断每个餐品的角色，并算出热量。"""
        for product in self.products.values():
            name = product.name
            if any(k in name for k in MERCH_KW):
                product.merch = True
                product.role = "merch"
                continue
            if any(k in name for k in COMBO_KW):
                product.combos = True
                product.role = "combo"
                continue

            if any(k in name for k in DRINK_KW):
                product.role = "drink"
            elif any(k in name for k in DESSERT_KW):
                product.role = "dessert"
            elif any(k in name for k in SIDE_KW):
                product.role = "side"
            elif any(k in name for k in MAIN_KW):
                product.role = "main"
            else:
                # 名字里没有那几个字，就看**接口自己的类目**。
                # 踩过的坑：「巨无霸」「麦香鱼」「麦香鸡」三个最经典的汉堡
                # 名字里既没有「堡」也没有「卷」，全被判成了 other ——
                # 而它们所在的类目叫「巨无霸 牛鱼肉堡」「鸡肉汉堡/卷」。
                product.role = self._role_from_category(product.category)

            record = self.table.exact(name)
            if record:
                product.nutrition = record
                product.nutrition_name = name
                product.kcal = record["kcal"]
            else:
                fuzzy = self.table.fuzzy(name)
                if fuzzy:
                    product.nutrition = fuzzy[0]
                    product.nutrition_name = fuzzy[1]
                    product.kcal = fuzzy[0]["kcal"]
                    product.kcal_estimated = True

    # -- 查询 -------------------------------------------------------------

    def _match_word(self, word: str, product: Product) -> bool:
        """一个口语词能否匹配到这个餐品。三条通道，任一命中即可。"""
        key = norm(word)
        if not key:
            return False
        if key in norm(product.name):                     # ① 字面命中
            return True
        for alias in SYNONYM.get(word, ()):               # ② 同义词
            if norm(alias) in norm(product.name):
                return True
        role = CATEGORY_MAP.get(word)                     # ③ 品类角色
        return bool(role) and product.role == role

    def search(self, keyword: str, roles=("main", "side", "drink", "dessert", "other")) -> list[Product]:
        """按口语关键词在菜单里搜索。完全动态，没有预设餐品清单。"""
        return [p for p in self.products.values() if p.role in roles and self._match_word(keyword, p)]

    def search_with_kcal(self, keyword: str) -> list[Product]:
        """搜索并只保留有热量数据的（推荐时用）。"""
        return [p for p in self.search(keyword) if p.kcal is not None]

    def by_name(self, name: str) -> Product | None:
        for p in self.products.values():
            if p.name == name:
                return p
        return None

    def find_product(self, name: str, code=None) -> Product | None:
        """按 code 或名字在菜单里找一个餐品，用于取官方商品图。

        套餐档位给的 code 是**套餐内的 SKU**，多半不在菜单里
        （实测「麦乐鸡-4块」code=2846 查不到，菜单里那条叫「麦乐鸡」code=1401）。
        所以名字要兜几层：精确名 → 归一化名 → 互相包含（取最长的，避免「可乐」误配到「可乐中杯」）。
        """
        if code is not None and str(code) in self.products:
            return self.products[str(code)]
        if not name:
            return None
        exact = self.by_name(name)
        if exact is not None:
            return exact

        key = norm(name)
        if not key:
            return None
        candidates = []
        for product in self.products.values():
            other = norm(product.name)
            if not other:
                continue
            if other == key:
                return product
            if other in key or key in other:
                candidates.append(product)
        if not candidates:
            return None
        candidates.sort(key=lambda p: -len(norm(p.name)))
        return candidates[0]

    def detail(self, code: str) -> dict:
        """餐品详情（含 rounds 与 modification），带缓存，省请求。"""
        if code in self._detail_cache:
            return self._detail_cache[code]
        raw = structured_of(self._call("query-meal-detail", {
            "storeCode": self.store_code, "orderType": 1, "beType": 1, "code": code,
        })) or {}
        body = raw.get("data") or {}
        self._detail_cache[code] = body
        return body

    # -- 特制 / 忌口（完全动态）-------------------------------------------

    def removable_ingredients(self, code: str) -> list[dict]:
        """这个餐品**能去掉**哪些配料。直接读接口，不预设任何忌口词。"""
        body = self.detail(code)
        out = []
        for group in (body.get("modification") or {}).get("items") or []:
            for value in group.get("values") or []:
                if value.get("selectedQuantity"):
                    out.append({
                        "code": value.get("code"),
                        "name": value.get("name"),
                        "key_keep": value.get("selectedKey"),
                        "key_drop": value.get("unselectedKey"),
                    })
        return out

    def avoid_matches(self, code: str, avoid: list[str]) -> tuple[list[str], list[str]]:
        """返回 (能在特制里去掉的, 去不掉的)。

        用户说「不吃洋葱」→ 现查该餐品有没有可以去掉的「洋葱」；
        用户说「不要酱」→ 现查有没有任何名字带「酱」的可去掉项。
        命中不了就如实说命中不了，不装作能做到。
        """
        removable = {item["name"]: item for item in self.removable_ingredients(code)}
        can_remove, cannot = [], []
        for word in avoid:
            hit = [name for name in removable if word in name or word in ("酱",) and "酱" in name]
            if hit:
                can_remove.extend(sorted(set(hit)))
            else:
                cannot.append(word)
        return sorted(set(can_remove)), cannot

    # -- 算价 -------------------------------------------------------------

    def price_of(self, items: list[dict]) -> int | None:
        """items: [{productCode, quantity, roundList?}]，返回应付金额（分）。"""
        if self.use_mock:
            total = 0.0
            for item in items:
                product = self.products.get(item["productCode"])
                if product and product.price is not None:
                    total += product.price * item.get("quantity", 1)
            return int(round(total * 100))
        raw = structured_of(self._call("calculate-price", {
            "storeCode": self.store_code, "orderType": 1, "beType": 1, "items": items,
        })) or {}
        return (raw.get("data") or {}).get("price")

    def price_single(self, code: str) -> int | None:
        """单个餐品的**单点**价格。会成为套餐专用 SKU 的餐品拿不到价，返回 None。"""
        if code not in self._price_cache:
            self._price_cache[code] = self.price_of([{"productCode": code, "quantity": 1}])
        return self._price_cache[code]

    # -- 套餐展开 ---------------------------------------------------------

    def combo_detail(self, code: str) -> Combo | None:
        product = self.products.get(code)
        if not product or not product.combos:
            return None

        # 磁盘缓存优先：套餐结构变化不频繁，缓存里已经有 56 个套餐的完整 rounds。
        # 演示模式下更是必须走缓存 —— mock 只有一个 query-meal-detail.json 样本，
        # 直接拿它当所有套餐的返回会串成假数据。
        cached = self._cached_combo(code)
        if cached:
            return cached
        if self.use_mock:
            self.warnings.append(f"演示数据里没有套餐「{product.name}」的组成，跳过")
            return None

        body = self.detail(code)
        if not body.get("rounds"):
            return None
        return Combo(code=code, name=product.name, price=product.price, rounds=body["rounds"])

    def _cached_combo(self, code: str) -> Combo | None:
        """mock 读仓库里的离线数据；真实模式读用户目录的缓存。

        **真实数据绝不写回 mock/** —— 那是全部测试的数据源，
        被某家门店的真实目录一冲，测试就集体瞎了（踩过一次）。
        """
        path = COMBO_CACHE if self.use_mock else COMBO_CACHE_USER
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        for item in raw.get("combos", []):
            if item.get("code") == code:
                return Combo(code=item["code"], name=item["name"],
                             price=item.get("price"), rounds=item.get("rounds") or [])
        return None

    def combo_options(self, combo: Combo) -> list[list[dict]]:
        """枚举一个套餐在不同 round 下的可选组合。

        返回 [{round_id, choices:[{code,name,diffPrice}]}] 的简化结构，供推荐时挑。
        """
        rounds = []
        for rnd in combo.rounds:
            choices = []
            for choice in rnd.get("choices") or []:
                choices.append({
                    "code": choice.get("code"),
                    "name": choice.get("name"),
                    "default": bool(choice.get("isDefault")),
                })
            rounds.append({"id": rnd.get("id"), "name": rnd.get("name"), "choices": choices})
        return rounds

    # 不是食品的组成（玩具、周边、包装、贴纸）—— 它们本来就不该有热量
    NON_FOOD_KW = ("玩具", "周边", "毛绒", "帽", "贴", "徽章", "包装", "捶", "百宝袋", "收藏卡")

    def _lookup(self, name: str) -> tuple[dict | None, bool]:
        """查营养表。返回 (记录, 是否非食品)。

        顺序很重要：**先查营养表**，查得到就是食品。
        否则「苹果片一袋(60g)」会因为带个「袋」字被当成周边（我踩过这个坑）。
        """
        record = self.table.exact(name or "")
        if record:
            return record, False
        fuzzy = self.table.fuzzy(name or "")
        if fuzzy:
            return fuzzy[0], False
        if any(k in (name or "") for k in self.NON_FOOD_KW):
            return None, True
        return None, False

    # -- 「同物多口味」解引用 ---------------------------------------------

    def variant_choices(self, product: Product, client: McDMcpClient | None = None) -> list[dict]:
        """商品是「同物多口味」容器就返回口味列表，否则返回 []。

        菜单上有这么一类商品，挂的是**口味集合**的名字，实际卖的是其中一种：

        - 「经典麦旋风」(9900008754) → 奥利奥麦旋风（默认）/ 草莓麦旋风
        - 「新地」(9900008745)       → 朱古力新地（默认）/ 草莓新地

        营养表里**没有**「经典麦旋风」这一条，所以以前只能报「热量暂无」——
        但它的口味在营养表里都躺着（奥利奥 266 / 草莓 295），算得出来。

        判据完全来自接口结构（`query-meal-detail` 的 `rounds[].choices[]`），
        **没有任何商品名清单**。名字会换，结构不会。

        ⚠️ 这是一次网络请求，**按需调用**。别在 `load()` 里对全菜单做 ——
        实测 90 个营养表未命中商品全量解引用要 38 秒。

        `client` 是给并发用的**独立连接**（见 `apply_variants`），单线程传 None 即可。
        """
        code = str(product.code)
        if code in self._variant_cache:
            return self._variant_cache[code]

        out: list[dict] = []
        try:
            body = self._fetch_detail(code, client)
        except Exception:      # noqa: BLE001 —— 解引用失败不该让整个流程崩
            body = {}

        for rnd in body.get("rounds") or []:
            for choice in rnd.get("choices") or []:
                name = choice.get("name") or ""
                record, non_food = self._lookup(name)
                out.append({
                    "code": choice.get("code"),
                    "name": name,
                    "kcal": record["kcal"] if record else None,
                    "default": bool(choice.get("isDefault")),
                    "non_food": non_food,
                })

        self._variant_cache[code] = out
        return out

    def _fetch_detail(self, code: str, client: McDMcpClient | None = None) -> dict:
        """取详情 body。给了 `client` 就走那条独立连接，否则走主连接（带缓存）。"""
        if client is None:
            return self.detail(code)
        raw = structured_of(client.call_tool("query-meal-detail", {
            "storeCode": self.store_code, "orderType": 1, "beType": 1, "code": code,
        })) or {}
        body = raw.get("data") or {}
        self._detail_cache[code] = body        # 缓存回填，下次不用再问
        return body

    def apply_variants(self, products, workers: int = 6,
                       include_estimated: bool = False) -> int:
        """把选中的商品解引用一遍，能补上热量的就补。返回补上的个数。

        只处理**营养表没命中、且名字跟营养表沾边**的商品（`shares_fragment` 预筛）。
        不预筛的话，一次 `--pick` 会把整屏没营养数据的时令款全查一遍。

        `include_estimated=True` 时连**模糊匹配来**的也一起查（菜单「新地」被配到
        「迷你朱古力新地」140 那种）。这会明显变多（16 样），所以只在
        「用户正在看的就那几样」时开，全量生成速查表时别开。

        并发用**每线程一条独立连接**：客户端那条长连接是复用的
        （见 `mcp_client._connection`），多线程同时往一条连接上写会互相踩 ——
        实测报 `Idle` 状态错误，所以每任务开一条自己的、用完就关。
        """
        todo = [p for p in products
                if not p.merch and not p.combos and not p.variants
                and (p.kcal is None or (include_estimated and p.kcal_estimated))
                and self.table.shares_fragment(p.name)]
        if not todo:
            return 0

        if self.use_mock:
            lists = [self.variant_choices(p) for p in todo]
        else:
            def one(product: Product) -> list[dict]:
                with McDMcpClient() as client:
                    return self.variant_choices(product, client=client)

            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                lists = list(pool.map(one, todo))

        resolved = 0
        for product, choices in zip(todo, lists):
            if not choices:
                continue
            product.variants = choices
            default = next((c for c in choices if c["default"]), None) or choices[0]
            if default.get("kcal") is not None:
                # 标 estimated：这是**默认口味**的热量，换了口味数字就不一样
                product.kcal = default["kcal"]
                product.nutrition_name = default["name"]
                product.kcal_estimated = True
                resolved += 1
        return resolved

    def combo_breakdown(self, combo: Combo, picked: dict | None = None) -> dict:
        """拆开一个套餐：每档选了什么、各多少热量、总共多少。

        套餐在接口里没有整体热量，只能按组成相加。
        儿童餐里的玩具不是一个「热量缺失」——它本来就不该有热量，要区别对待。
        """
        picked = picked or {}
        parts: list[dict] = []
        total = 0
        known: list[str] = []
        unknown: list[str] = []

        for rnd in combo.rounds or []:
            want = picked.get(rnd.get("id"))
            chosen = None
            for choice in rnd.get("choices") or []:
                if want and choice.get("code") == want:
                    chosen = choice
                    break
                if not want and choice.get("isDefault"):
                    chosen = choice
                    break
            if not chosen:
                choices = rnd.get("choices") or []
                chosen = choices[0] if choices else None
            if not chosen:
                continue

            name = chosen.get("name") or ""
            record, non_food = self._lookup(name)
            kcal = record["kcal"] if record else None
            parts.append({"round": rnd.get("name"), "name": name,
                          "code": chosen.get("code"), "kcal": kcal, "non_food": non_food})
            if kcal is not None:
                total += kcal
                known.append(name)
            elif not non_food:
                unknown.append(name)

        return {
            "name": combo.name, "code": combo.code, "price": combo.price,
            "parts": parts, "kcal": total, "known": known, "unknown": unknown,
            "complete": not unknown,
            "has_toy": any(p["non_food"] for p in parts),
            "kcal_text": _combo_kcal_text(total, unknown),
        }

    def kids_meals(self) -> list[dict]:
        """所有儿童套餐（开心乐园餐 / 亲子餐）。

        判定是**动态**的：套餐组成里含有玩具这一档，或者名字里带乐园/亲子。
        只按名字猜不可靠——名字会换，套餐结构不会。
        """
        out = []
        for combo in self.combo_catalog():
            all_choice = " ".join(
                (c.get("name") or "") for r in combo.rounds for c in (r.get("choices") or [])
            )
            if "玩具" in all_choice or any(k in combo.name for k in ("乐园", "亲子", "儿童")):
                out.append(self.combo_breakdown(combo))
        out.sort(key=lambda c: c["kcal"])
        return out

    def combo_nutrition(self, combo: Combo, picked: dict | None = None) -> dict:
        """套餐的合计营养（热量/蛋白质/脂肪/碳水/钠）。"""
        detail = self.combo_breakdown(combo, picked)
        totals = {"kcal": 0, "protein": 0.0, "fat": 0.0, "carb": 0.0, "sodium": 0}
        for part in detail["parts"]:
            record, non_food = self._lookup(part["name"])
            if not record:
                continue
            for key in totals:
                totals[key] += record.get(key, 0)
        detail["nutrition"] = totals
        return detail

    def combo_price(self, combo: Combo, picked: dict[int, str]) -> int | None:
        round_list = []
        for rnd in combo.rounds:
            code = picked.get(rnd.get("id"))
            if not code:
                default = next((c for c in rnd.get("choices") or [] if c.get("isDefault")), None)
                code = default.get("code") if default else None
            if not code:
                continue
            round_list.append({"round": str(rnd.get("id")), "comboItemList": [{"code": code, "quantity": 1}]})
        return self.price_of([{"productCode": combo.code, "quantity": 1, "roundList": round_list}])

    @staticmethod
    def _diff_cents(text) -> int:
        """把 「+ ¥1.5」/「- ¥1」 这类文本转成分。"""
        if not text:
            return 0
        raw = str(text).replace("¥", "").replace("￥", "").replace("+", "").replace(" ", "")
        negative = raw.startswith("-")
        raw = raw.lstrip("-")
        try:
            cents = int(round(float(raw) * 100))
        except ValueError:
            return 0
        return -cents if negative else cents

    def _combo_price_estimate(self, combo: Combo, picked: dict) -> int | None:
        """本地估算套餐价 = 套餐标价 + 各 round 选品的加价。

        用来快速筛掉大部分候选，避免每个套餐都打一次算价接口（那要几十秒）。
        """
        if combo.price is None:
            return None
        cents = int(round(combo.price * 100))
        for rnd in combo.rounds or []:
            code = picked.get(rnd.get("id"))
            for choice in rnd.get("choices") or []:
                if choice.get("code") == code:
                    cents += self._diff_cents(choice.get("diffPrice"))
                    break
        return cents

    def combo_catalog(self, refresh: bool = False) -> list[Combo]:
        """菜单里所有**带 rounds 的套餐**。

        磁盘缓存打底（省几十次详情请求），再用菜单现状校正：
        - 菜单新上了套餐但缓存没有 → 补一次详情
        - 缓存里有、菜单里已经下架 → 丢掉
        演示模式下补不到详情的会记进 warnings，不会拿假数据顶上。
        """
        if self._catalog_cache is not None and not refresh:
            return self._catalog_cache

        wanted = {p.code: p for p in self.products.values() if p.combos}
        combos: dict[str, Combo] = {}
        if not refresh:
            for code in wanted:
                cached = self._cached_combo(code)
                if cached:
                    combos[code] = cached

        missing = [code for code in wanted if code not in combos]
        for code in missing:
            combo = self.combo_detail(code)
            if combo:
                combos[code] = combo

        result = [combos[code] for code in wanted if code in combos]

        # 有新发现的就回写缓存，省下次的请求 —— 只写用户目录，**不碰 mock/**
        if not self.use_mock and any(code in combos for code in missing):
            try:
                COMBO_CACHE_USER.write_text(
                    json.dumps(
                        {"combos": [{"code": c.code, "name": c.name,
                                     "price": c.price, "rounds": c.rounds}
                                    for c in result]},
                        ensure_ascii=False, indent=1),
                    encoding="utf-8")
            except OSError:
                pass

        self._catalog_cache = result
        return result

    # -- 套餐比价：这是「怎么点划算」的核心 --------------------------------

    def find_deals(self, names: list[str], top: int = 3, verify: bool = True) -> list[dict]:
        """给定想吃的几样，找出能装下它们的套餐，并跟单点价对比。

        两个必须守住的点：
        1. 套餐内的 choice code 有的是**套餐专用 SKU**（如 3050 可乐中杯），
           单独算价会返回 0/None。所以单点价必须用菜单里能单点的那一份来算，
           否则会算出假优惠。
        2. 先本地估算筛掉大部分候选，只对最优的几个打真实算价接口。
        """
        wanted = [norm(n) for n in names]
        candidates = []

        for combo in self.combo_catalog():
            picked: dict = {}
            covered: list[str] = []
            ok = True
            for rnd in combo.rounds or []:
                choices = rnd.get("choices") or []
                match = None
                for choice in choices:
                    cname = norm(choice.get("name") or "")
                    if any(w and w in cname for w in wanted):
                        match = choice
                        break
                if match is not None:
                    covered.append(match.get("name"))
                else:
                    match = next((c for c in choices if c.get("isDefault")), None)
                if match is None:
                    ok = False
                    break
                picked[rnd.get("id")] = match.get("code")
            if not ok or not covered:
                continue

            estimate = self._combo_price_estimate(combo, picked)
            if estimate is None:
                continue
            candidates.append({"combo": combo, "picked": picked, "covered": covered, "estimate": estimate})

        if not candidates:
            return []

        # 先按「装下的越多越好」，再按便宜。只按价格排会把正好的套餐挤出候选。
        candidates.sort(key=lambda c: (-len(c["covered"]), c["estimate"]))
        results = []
        for candidate in candidates[: max(top * 5, 12)]:
            amount = candidate["estimate"]
            if verify:
                real = self.combo_price(candidate["combo"], candidate["picked"])
                if real:
                    amount = real

            # 只跟套餐**真正装下的**那几样比单点价。
            # 拿全部请求项去比会得出「省 30 多」这种假优惠（我在实测里踩到过）。
            cover_single = 0
            cover_detail: list[dict] = []
            unpriced: list[str] = []
            for cname in candidate["covered"]:
                cents, note = self.single_price_by_name(cname)
                if cents:
                    cover_single += cents
                    cover_detail.append({"name": cname, "price": cents, "note": note})
                else:
                    unpriced.append(cname)
            if not cover_detail:
                continue

            missing = [n for n in names
                       if not any(norm(n) in norm(c) or norm(c) in norm(n) for c in candidate["covered"])]
            results.append({
                "combo_name": candidate["combo"].name,
                "combo_code": candidate["combo"].code,
                "combo_price": amount,
                "covered": candidate["covered"],
                "covered_priced": cover_detail,
                "covered_unpriced": unpriced,
                "missing": missing,
                "single_price": cover_single,
                "saving": cover_single - amount,
                "complete": not unpriced,
            })

        results.sort(key=lambda r: (-r["saving"], len(r["missing"])))
        return results[:top]

    def single_price_by_name(self, name: str) -> tuple[int | None, str]:
        """给一个名字（可能是套餐内的规格名，如「小薯条」）找**单点**价。

        返回 (分, 说明)。找不到就返回 (None, 原因)，调用方必须如实标注，
        不能拿 0 或者别的价格糊弄过去。
        """
        product = self.by_name(name)
        if product:
            return self.price_single(product.code), ""

        key = norm(name)
        for candidate in self.products.values():
            if candidate.combos or candidate.merch:
                continue
            hit = candidate.nutrition_name or candidate.name
            if norm(hit) == key:
                return self.price_single(candidate.code), f"按菜单「{candidate.name}」计"

        for candidate in self.products.values():
            if norm(candidate.name) == key:
                return self.price_single(candidate.code), ""

        return None, "菜单里没有可单点的同款"

    def single_total(self, names: list[str]) -> tuple[int | None, list[dict]]:
        """把几样东西按**单点**加起来。单点不到的会被跳过并记下来。"""
        total = 0
        detail = []
        for name in names:
            product = self.by_name(name)
            if not product:
                detail.append({"name": name, "price": None, "note": "菜单里没有"})
                continue
            amount = self.price_single(product.code)
            detail.append({"name": name, "price": amount, "note": ""})
            if amount:
                total += amount
        return (total or None), detail

    # -- 推荐 -------------------------------------------------------------

    def recommend(self, wants: list[str], budget: float, kcal: int | None = None,
                  avoid: list[str] | None = None, count: int = 3,
                  require_all: bool = True) -> list[dict]:
        """核心入口。

        wants:       想吃的关键词，如 ["汉堡", "薯条", "可乐"]
        budget:      预算（元）
        kcal:        热量上限/目标（None 表示不限制）
        avoid:       忌口词，如 ["辣", "洋葱"]。**不预设**，实时去餐品特制项里找
        require_all: True  = 每样都要有（「我想吃 A、B、C」）
                     False = 第一样必选，其余按热量/预算自动填充（「有薯条，其他怎么安排」）
        """
        avoid = avoid or []
        banned = [w for w in avoid if w not in ("洋葱", "生菜", "酱", "吉士", "酸黄瓜")]

        def allowed(product: Product) -> bool:
            return not (banned and any(w in product.name for w in banned))

        # 每个关键词 → 一个候选池（动态搜索）
        pools: list[tuple[str, list[Product]]] = []
        for word in wants:
            hits = [p for p in self.search(word) if p.kcal is not None and allowed(p)]
            if hits:
                pools.append((word, hits))

        if not pools:
            self.last_reason = f"菜单里找不到跟「{'、'.join(wants)}」相关的餐品。"
            return []

        results: list[dict] = []
        min_cost: dict | None = None
        min_kcal: dict | None = None
        tolerance = 120          # 超一点也算「贴着目标」，不然永远选不到整数

        def consider(chosen: list[Product]) -> None:
            nonlocal min_cost, min_kcal
            total_kcal = sum(p.kcal for p in chosen)
            cost = sum(p.price or 0 for p in chosen)
            bundle = {"items": [p.name for p in chosen], "kcal": total_kcal, "menu_cost": cost}
            if min_cost is None or cost < min_cost["menu_cost"]:
                min_cost = bundle
            if min_kcal is None or total_kcal < min_kcal["kcal"]:
                min_kcal = bundle
            if cost <= budget and (kcal is None or total_kcal <= kcal + tolerance):
                results.append(bundle)

        def walk(index: int, chosen: list[Product]) -> None:
            if index == len(pools):
                consider(chosen)
                return
            for product in pools[index][1]:
                if product.name in [c.name for c in chosen]:
                    continue
                nxt = chosen + [product]
                if require_all:
                    walk(index + 1, nxt)
                else:
                    consider(nxt)        # 允许提前收手：只要含必选项就成一套
                    walk(index + 1, nxt)

        walk(0, [])

        if not results:
            # 分清到底是「钱不够」还是「热量装不下」，别给错建议
            if min_cost and budget and min_cost["menu_cost"] > budget:
                over = min_cost["menu_cost"] - budget
                self.last_reason = (
                    f"预算不够。最便宜的一套是「{' + '.join(min_cost['items'])}」，"
                    f"共 ¥{min_cost['menu_cost']:.2f}，比预算 ¥{budget:.0f} 多 ¥{over:.2f}。"
                )
            elif kcal is not None and min_kcal and min_kcal["kcal"] > kcal + tolerance:
                self.last_reason = (
                    f"{kcal} 大卡装不下。最少的一套是「{' + '.join(min_kcal['items'])}」，"
                    f"光这些就 {min_kcal['kcal']} 大卡，超了 {min_kcal['kcal'] - kcal}。"
                    f"要么放宽热量，要么换小一号的。"
                )
            else:
                self.last_reason = "组合条件互相打架，凑不出来。"
            return []

        # 去重 + 排序：贴近热量目标的优先
        seen = set()
        unique = []
        for r in sorted(results, key=lambda r: abs(r["kcal"] - (kcal or r["kcal"]))):
            key = tuple(sorted(r["items"]))
            if key in seen:
                continue
            seen.add(key)
            unique.append(r)

        # 尽量三套的主食不重复，读起来才像三个不同的方案
        picked: list[dict] = []
        mains: set[str] = set()
        for plan in unique:
            if plan["items"][0] in mains and len(picked) < count:
                continue
            mains.add(plan["items"][0])
            picked.append(plan)
            if len(picked) >= count:
                break
        for plan in unique:
            if len(picked) >= count:
                break
            if plan not in picked:
                picked.append(plan)

        for index, plan in enumerate(picked, 1):
            plan["no"] = index
            products = [self.by_name(n) for n in plan["items"]]
            plan["protein"] = sum(p.nutrition["protein"] for p in products if p and p.nutrition)
            plan["amount"] = self.price_of([{"productCode": p.code, "quantity": 1} for p in products if p])
            plan["items_detail"] = products
            plan["notes"] = self._avoid_notes(products, avoid)
        return picked

    def _avoid_notes(self, products: list[Product | None], avoid: list[str]) -> list[str]:
        """实时查每个餐品的特制项，给出「可以去掉 X」的备注。"""
        if not avoid:
            return []
        notes = []
        for product in products:
            if not product:
                continue
            can_remove, cannot = self.avoid_matches(product.code, avoid)
            for ingredient in can_remove:
                notes.append(f"{product.name}：可以去掉「{ingredient}」")
        return notes
