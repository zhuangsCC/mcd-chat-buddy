#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦麦懂你 · 点餐入口（**唯一需要跑的一个脚本**）

不管你在哪个城市，告诉它地名，它就点你本地那家麦当劳：

    python3 scripts/mcd.py where ××市               # 我在××市 → 定下最近的门店
    python3 scripts/mcd.py want 香辣鸡腿堡 薯条       # 多少钱、多少热量、长什么样
    python3 scripts/mcd.py order 香辣鸡腿堡 薯条      # 下单（然后去麦当劳 APP 付款）

算账、拉菜单、配营养表这些活都在 `menu_engine.py` 里，这里只做三件事：

1. **门店定位** —— 地名 → `query-nearby-stores`（beType=1 到店自取，searchType=2 按位置搜）
2. **口语 → 菜单名** —— 用户说「香辣鸡腿堡」，菜单上叫「麦辣鸡腿汉堡」，得对上
3. **把结果排成人能看的一屏** —— 价格、热量、实物图，直接贴给用户

图片一律用官方商品图（`query-meals` 给的 `image`），本地不存、不裁、不画。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from menu_engine import MenuEngine, norm          # noqa: E402
from mcp_client import McDMcpClient, structured_of  # noqa: E402

# 门店记在这个文件里（用户级，不进仓库）。换城市就重跑一次 where。
STORE_FILE = Path.home() / ".mcd-buddy.json"

# 用户说「××市」，接口要求 city 和 keyword 都不能空 —— 两个都填原话就行。
# 实测 city 带不带「市」都返回 5 家（缺任一个报 600058）。


# --------------------------------------------------------------------------
# 门店
# --------------------------------------------------------------------------


def find_stores(client: McDMcpClient, place: str) -> list[dict]:
    """按地名找门店，离得最近的在前面。支持智能拆分城市与地标（如：××市××路）。"""
    city = place
    keyword = place

    # 常见城市后缀处理与拆分
    for suffix in ("市", "区", "县", "镇"):
        if suffix in place:
            idx = place.find(suffix) + len(suffix)
            if idx < len(place):
                city = place[:idx]
                keyword = place[idx:]
                break

    if city == place and len(place) > 2:
        for cname in sorted(("佛山", "广州", "深圳", "东莞", "北京", "上海", "成都", "杭州", "武汉", "南京", "西安", "重庆", "长沙", "苏州")):
            if place.startswith(cname) and len(place) > len(cname):
                city = cname + "市"
                keyword = place[len(cname):]
                break

    res = client.call_tool("query-nearby-stores", {
        "beType": 1,          # 到店自取（5 是得来速车道）
        "searchType": 2,      # 按位置搜索（1 是搜收藏的店）
        "city": city,
        "keyword": keyword,
    })
    data = (structured_of(res) or {}).get("data") or []
    if not data and city != place:
        # 回退直接用原词再搜一次
        res = client.call_tool("query-nearby-stores", {
            "beType": 1, "searchType": 2, "city": place, "keyword": place,
        })
        data = (structured_of(res) or {}).get("data") or []
    return data


def save_stores(place: str, stores: list[dict]) -> list[dict]:
    """把找到的门店（按距离，最近的在前面）记下来。"""
    slim = [{"code": s.get("storeCode"), "name": s.get("storeName"),
             "address": s.get("address"), "distance": s.get("distance")}
            for s in stores[:5] if s.get("storeCode")]
    STORE_FILE.write_text(json.dumps(
        {"city": place, "stores": slim}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    return slim


def load_store() -> dict:
    if not STORE_FILE.exists():
        return {}
    try:
        return json.loads(STORE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


# --------------------------------------------------------------------------
# 口语 → 菜单名
# --------------------------------------------------------------------------


def score(query: str, name: str) -> float:
    """用户口语词和菜单名的相似度，0~1。

    用户说「香辣鸡腿堡」，菜单上写的是「麦辣鸡腿汉堡」——
    重合的字是 辣/鸡/腿/堡，四个。对着「板烧鸡腿堡」只重合三个，
    对着「香芋派」只重合一个。**靠重合字数排序就行，不需要任何餐品清单。**

    字面完全一样 / 互相包含的，直接给高分，压在打分的上面。
    """
    q, n = norm(query), norm(name)
    if not q or not n:
        return 0.0
    if q == n:
        return 1.0
    if q in n or n in q:
        return 0.9

    qs, ns = set(q), set(n)
    common = len(qs & ns)
    if common < 2:
        return common / len(qs) * 0.4      # 只重合一个字，多半是巧合
    precision = common / len(qs)
    recall = common / len(ns)
    f1 = 2 * precision * recall / (precision + recall)
    return f1 * 0.85                       # 打个折，排到「字面命中」后面


# 分差在这个比例内算「差不多」，一起列出来让用户挑
TIE_RATIO = 0.85
MIN_SCORE = 0.35

# 用户话里带这些字，才算在点套餐（四件套/随心选/开心乐园餐…）。
# 没带就别让套餐盖过单品 —— 实测「麦辣鸡腿堡」是「麦辣鸡腿堡四件套」的子串（0.9 分），
# 会压过真单品「麦辣鸡腿汉堡」（0.773 分），¥23 的汉堡被悄悄换成 ¥28 的套餐。
COMBO_WORDS = ("套", "组合", "随心", "餐", "盒", "桶", "拼")

# 列套餐时最多铺几个。这家店实测有 58 个，全铺出来屏幕装不下、图也太多。
COMBO_LIMIT = 12


def match(query: str, products: list) -> list:
    """按口语词挑餐品，最像的排前面。挑不到就是空。

    分数打平时**名字短的优先**：用户说「麦旋风」，菜单上既有「经典麦旋风」
    也有「蓝莓爆爆珠麦旋风」，两个都是子串命中、分数一样 ——
    短的那个才是不带修饰的经典款。

    用户没提套餐字眼时，套餐一律打对折再比：**说单品就给单品**。
    真想要套餐，说全名（含「套」「随心选」）或者走 `mcd.py combo`。
    """
    wants_combo = any(w in norm(query) for w in COMBO_WORDS)

    def ranked():
        for p in products:
            s = score(query, p.name)
            if p.combos and not wants_combo:
                s *= 0.5
            yield s, len(norm(p.name)), p

    ranked = sorted(ranked(), key=lambda item: (-item[0], item[1]))
    ranked = [item for item in ranked if item[0] >= MIN_SCORE]
    if not ranked:
        return []
    best = ranked[0][0]
    return [p for s, _, p in ranked if s >= best * TIE_RATIO][:3]


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------


def yuan(cents) -> str:
    return f"¥{cents / 100:.1f}"


def kcal_text(product) -> str:
    """热量怎么念。不知道就说不知道，**绝不写成 0**。

    「经典麦旋风」这种是选口味的，上面那个数字只是**默认口味**的，
    所以要把口味也摊出来（奥利奥 266 / 草莓 295）。
    """
    if product.kcal is None:
        text = "热量暂无"
    else:
        text = f"{product.kcal} 大卡" + ("（按默认规格估）" if product.kcal_estimated else "")
    if getattr(product, "variant_text", ""):
        text += f"<br>口味：{product.variant_text}"
    return text


def store_line(store: dict) -> str:
    bits = [store.get("name") or "", store.get("address") or ""]
    if store.get("distance"):
        bits.append(f"{store['distance']} 米")
    return " · ".join(x for x in bits if x)


def render_menu(store: dict, rows: list[dict]) -> str:
    """把「要什么 → 多少钱多少热量」排成 Markdown，直接能贴。"""
    lines = [f"**{store_line(store)}**", ""]
    has_qty = any(r.get("quantity", 1) > 1 for r in rows)
    if len(rows) > 1 or has_qty:
        header = "| 你要的 | 菜单上叫 | 数量 | 单价 | 小计 | 热量 |" if has_qty else "| 你要的 | 菜单上叫 | 单价 | 热量 |"
        sep = "|:---|:---|:---:|---:|---:|---:|" if has_qty else "|:---|:---|---:|---:|"
        lines += [header, sep]
        for row in rows:
            kcal = row.get("kcal_text") or kcal_text(row["product"])
            if has_qty:
                lines.append(f"| {row['ask']} | {row['product'].name} | {row.get('quantity', 1)} | "
                             f"{yuan(row.get('single_cents', row['cents']))} | {yuan(row['cents'])} | {kcal} |")
            else:
                lines.append(f"| {row['ask']} | {row['product'].name} | "
                             f"{yuan(row['cents'])} | {kcal} |")
    else:
        row = rows[0]
        lines.append(f"**{row['product'].name}** · {yuan(row['cents'])} · "
                     f"{row.get('kcal_text') or kcal_text(row['product'])}")

    total = sum(r["cents"] for r in rows)
    lines += ["", f"**合计 {yuan(total)}**"]

    # 图必须有。约束宽度（160px）和圆角，保证在界面/聊天框里精致适中，不破坏版面。
    lines.append("")
    for row in rows:
        product = row["product"]
        if product.image:
            lines.append(f'<img src="{product.image}" alt="{product.name}" width="160" style="border-radius:8px;margin:6px 0;display:block;">')
    return "\n".join(lines)


def combo_hint(engine, rows: list[dict]) -> str:
    """买多件时比一句价：有没有套餐把要的几样全含了、还更便宜。

    **只提示不替换** —— 多出来的东西要点明，换不换用户说了算
    （红线：不怂恿加量）。数量不齐（2 汉堡 1 可乐）不比，账会算不平。
    """
    qtys = {r.get("quantity", 1) for r in rows}
    if len(rows) < 2 or len(qtys) != 1:
        return ""
    qty = qtys.pop()
    names = [r["product"].name for r in rows]
    total = sum(r["cents"] for r in rows)

    best = None
    for c in engine.combo_catalog():
        if c.price is None:
            continue
        combo = engine.combo_detail(c.code)
        detail = engine.combo_breakdown(combo) if combo else None
        parts = [p["name"] for p in detail["parts"]] if detail else []
        if not parts:
            continue
        # 要的**每一样**套餐里都得有；套餐多出来的没关系，那是要点明的「会多出」
        if not all(max((score(n, pt) for pt in parts), default=0) >= 0.8 for n in names):
            continue
        cents = int(round(c.price * 100)) * qty
        if cents < total and (best is None or cents < best[1]):
            best = (c, cents, parts)
    if not best:
        return ""
    c, cents, parts = best
    extra = [pt for pt in parts
             if max((score(n, pt) for n in names), default=0) < 0.8]
    more = f"（会多出：{'、'.join(extra)}）" if extra else ""
    return (f"\n💡 顺便比个价：{qty} 份「{c.name}」= {yuan(cents)}，"
            f"你要的这几样都含，比单买省 {yuan(total - cents)}{more}。要换就说套餐名。")


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------


def cmd_where(client: McDMcpClient, place: str) -> int:
    if not place:
        print("你在哪个城市？（说个城市名或者地标，比如「××市」「××区」「××路」）")
        return 2
    stores = find_stores(client, place)
    if not stores:
        print(f"「{place}」附近没找到能到店自取的麦当劳，换个说法试试。")
        return 1

    print(f"**{place}** 附近这几家：")
    for index, store in enumerate(stores[:5], 1):
        print(f"{index}. {store.get('storeName')} · {store.get('address')} "
              f"· {store.get('distance')} 米")

    slim = save_stores(place, stores)
    print(f"\n已记下最近的一家：**{slim[0]['name']}**"
          f"（打烊了会自动换下一家）")
    return 0


def load_engine(client: McDMcpClient, saved: dict):
    """从近到远，挑一家**现在真的能点餐**的店。

    光看营业状态不够 —— 实测有门店标着「营业中」，
    23:05 拉菜单却报 600057「门店可能已关闭或不在营业时间」。
    所以直接拿菜单试，谁出得来算谁的。

    返回 (门店, 引擎)。一家都不行就返回 (None, None)。
    """
    stores = saved.get("stores") or []
    if not stores and saved.get("code"):
        stores = [saved]                      # 兼容老格式
    for store in stores:
        engine = MenuEngine(client, store_code=store["code"])
        engine.load()
        if engine.products:
            return store, engine
    return None, None


def parse_quantity(text: str) -> tuple[int, str]:
    """从人话中提取数量，比如「2个麦辣鸡腿堡」-> (2, '麦辣鸡腿堡')，「薯条*3」-> (3, '薯条')。"""
    import re
    # 形式1：开头的数字 + 量词（个/份/包/杯/支/盒/套/块）
    m1 = re.match(r"^(\d+)\s*(?:个|份|包|杯|支|盒|套|块|只|对)?\s*(.+)$", text.strip())
    if m1 and m1.group(2).strip():
        try:
            qty = max(1, min(99, int(m1.group(1))))
            return qty, m1.group(2).strip()
        except ValueError:
            pass

    # 形式2：结尾的 *2 或 x2
    m2 = re.search(r"^(.+?)[*xX×](\d+)$", text.strip())
    if m2 and m2.group(1).strip():
        try:
            qty = max(1, min(99, int(m2.group(2))))
            return qty, m2.group(1).strip()
        except ValueError:
            pass

    return 1, text.strip()


def pick_rows(engine: MenuEngine, wants: list[str]) -> tuple[list[dict], list[str]]:
    """把用户说的几样对上菜单。支持识别数量（如 2个麦辣鸡腿堡）。返回 (对上的, 没对上的)。"""
    candidates = [p for p in engine.products.values() if not p.merch]

    rows, missing = [], []
    for want in wants:
        qty, clean_name = parse_quantity(want)
        hits = match(clean_name, candidates)
        if not hits:
            missing.append(want)
            continue
        product = hits[0]
        single_cents = int(round((product.price or 0) * 100))
        row = {
            "ask": want,
            "product": product,
            "quantity": qty,
            "single_cents": single_cents,
            "cents": single_cents * qty,
        }
        if product.combos:
            # 套餐没有整体热量 —— 拆开相加，和 combo 命令同一个口径
            combo = engine.combo_detail(product.code)
            detail = engine.combo_breakdown(combo) if combo else None
            if detail:
                row["kcal_text"] = detail["kcal_text"]
        rows.append(row)

    engine.apply_variants([r["product"] for r in rows], include_estimated=True)
    return rows, missing


def open_store(client: McDMcpClient, saved: dict):
    """共用的开场：找到能点餐的店 + 拉好菜单。不行就说清楚为什么。"""
    store, engine = load_engine(client, saved)
    if store:
        return store, engine
    city = saved.get("city") or "你那边"
    print(f"{city}附近这几家现在都点不了（打烊或者不在营业时间）。"
          f"换一家店试试？`mcd.py where <别的城市或地标>`")
    return None, None


def cmd_combo(client: McDMcpClient, saved: dict, args: list[str]) -> int:
    """列在售套餐：多少钱、多少卡、里面有什么。

    套餐**没有整体热量** —— 只能按 `rounds[]` 拆成子项，逐个配营养表再相加。
    拆不全的如实写「至少 X 大卡」，不拿相近商品顶。
    """
    store, engine = open_store(client, saved)
    if not store:
        return 1

    keyword = "".join(args)
    # 口语同义词映射
    kw_pool = [keyword] if keyword else []
    if keyword in ("儿童", "小孩", "小朋友", "玩具"):
        kw_pool = ["乐园", "亲子", "儿童", "玩具"]

    rows = []
    for product in engine.products.values():
        if not product.combos:
            continue
        combo = engine.combo_detail(product.code)
        detail = engine.combo_breakdown(combo) if combo else None
        combo_text = product.name + " " + (" ".join(p["name"] for p in detail["parts"]) if detail else "")
        if kw_pool and not any(k in combo_text for k in kw_pool):
            continue
        rows.append((product, detail))

    if not rows:
        print(f"这家店没有含「{keyword}」的套餐。" if keyword
              else "这家店现在没有套餐。")
        return 1

    total = len(rows)
    rows = rows[:COMBO_LIMIT]

    lines = [f"**{store_line(store)}**", ""]
    lines += ["| 套餐 | 价格 | 热量 | 含什么 |", "|:---|---:|---:|:---|"]
    for product, detail in rows:
        cents = int(round((product.price or 0) * 100))
        if detail:
            kcal = detail["kcal_text"]
            inside = " + ".join(p["name"] for p in detail["parts"])
        else:
            kcal, inside = "热量暂无", "组成没拿到"
        lines.append(f"| {product.name} | {yuan(cents)} | {kcal} | {inside} |")

    if total > COMBO_LIMIT:
        lines += ["", f"（这家店一共 **{total}** 个套餐，上面是门店陈列顺序的前 "
                      f"{COMBO_LIMIT} 个。想看某类就说 `mcd.py combo 四件套`）"]

    lines.append("")
    for product, _ in rows:
        if product.image:
            lines.append(f'<img src="{product.image}" alt="{product.name}" width="160" style="border-radius:8px;margin:6px 0;display:block;">')
    print("\n".join(lines))
    return 0


def combo_items(engine: MenuEngine, product) -> dict:
    """套餐下单要带 `roundList` —— 每一档选了什么。

    这里一律按**默认选项**选（接口用 `isDefault` 标了）。用户要换可乐大小、
    换小食，是下一步的事，先把默认那一套下出来。
    """
    combo = engine.combo_detail(product.code)
    rounds = []
    for rnd in (combo.rounds if combo else []) or []:
        choices = rnd.get("choices") or []
        chosen = (next((c for c in choices if c.get("isDefault")), None)
                  or (choices[0] if choices else None))
        if not chosen:
            continue
        rounds.append({"round": str(rnd.get("id")),
                       "comboItemList": [{"code": chosen.get("code"), "quantity": 1}]})
    item = {"productCode": product.code, "quantity": 1}
    if rounds:
        item["roundList"] = rounds
    return item


def cmd_want(client: McDMcpClient, saved: dict, wants: list[str]) -> int:
    if not wants:
        print("你想吃点什么？（说个名字就行，比如「香辣鸡腿堡 薯条」）")
        return 2
    store, engine = open_store(client, saved)
    if not store:
        return 1
    rows, missing = pick_rows(engine, wants)

    if rows:
        print(render_menu(store, rows))
        hint = combo_hint(engine, rows)
        if hint:
            print(hint)
    for want in missing:
        print(f"\n⚠️ 菜单上没有「{want}」这一样，换个说法或者问问别的？")
    return 0


def cmd_order(client: McDMcpClient, saved: dict, wants: list[str]) -> int:
    if not wants:
        print("你想点什么？（说个名字就行）")
        return 2
    store, engine = open_store(client, saved)
    if not store:
        return 1
    rows, missing = pick_rows(engine, wants)
    if not rows:
        print("这几个都没对上菜单，先说说你想吃什么？")
        return 1
    for want in missing:
        print(f"⚠️ 菜单上没有「{want}」，这一样先跳过了。")

    # 套餐要带 roundList（每一档选什么），单品直接下
    items = []
    for r in rows:
        qty = r.get("quantity", 1)
        if r["product"].combos:
            it = combo_items(engine, r["product"])
            it["quantity"] = qty
            items.append(it)
        else:
            items.append({"productCode": r["product"].code, "quantity": qty})

    # 先算价。takeWayCode 要从算价结果里取，不能自己写死。
    quote = structured_of(client.call_tool("calculate-price", {
        "storeCode": store["code"], "orderType": 1, "beType": 1, "items": items,
    })) or {}
    quote = quote.get("data") or {}
    cents = quote.get("price")
    if not cents:
        print("算价没算出来，先别下单 —— 把上面的清单发我看看。")
        return 1

    take_ways = quote.get("takeWayList") or []
    if not take_ways:
        print("这家店暂时不能到店自取下单。")
        return 1
    take_way = next((w for w in take_ways if "堂食" in (w.get("name") or "")),
                    take_ways[0])

    order = structured_of(client.call_tool("create-order", {
        "storeCode": store["code"], "orderType": 1, "beType": 1,
        "takeWayCode": take_way.get("code"),
        "items": items,
    })) or {}
    order = order.get("data") or {}
    order_id = order.get("orderId") or order.get("orderNo")
    if not order_id:
        print("下单没成功，接口没给订单号。再试一次？")
        return 1

    print(render_menu(store, rows))
    print()
    print(f"✅ **下单成功** · {store.get('name')} · {take_way.get('name') or '到店自取'}")
    print(f"实付 **{yuan(int(cents))}** ｜ 支付截止 **{order.get('expirePayTime') or '15 分钟内'}**")
    print()
    print("📱 你手机上的麦当劳 APP 里已经出现这个订单了 —— 打开「我的订单」就能看到。")
    print("这个二维码不一定要扫，直接在 APP 里下单就可以了，有问题可以直接取消。")
    return 0


def cmd_suggest(client: McDMcpClient, saved: dict, raw_args: list[str]) -> int:
    """根据预算、吃饱/控卡目标、必选单品动态推荐套餐与组合。"""
    parser = argparse.ArgumentParser(prog="mcd.py suggest", description="动态预算与搭配推荐")
    parser.add_argument("--budget", type=float, default=None, help="预算（元）")
    parser.add_argument("--must", action="append", default=[], help="必须包含的餐品关键词，可多次指定")
    parser.add_argument("--goal", choices=["full", "low_kcal", "deal"], default="deal",
                        help="目标：full(吃饱/高热量) / low_kcal(控卡/低热量) / deal(最实惠超值)")
    try:
        opts = parser.parse_args(raw_args)
    except SystemExit:
        return 2

    store, engine = open_store(client, saved)
    if not store:
        return 1

    combos = engine.combo_catalog()
    valid_combos = []
    for c in combos:
        if c.price is None:
            continue
        if opts.budget and c.price > opts.budget:
            continue
        b = engine.combo_breakdown(c)
        combo_obj = engine.combo_detail(c.code)
        # 收集套餐名字、默认部件名字，以及轮次所有可选子项（如咖啡、美式、红茶等）
        choice_names = []
        if combo_obj:
            for rnd in combo_obj.rounds:
                for ch in rnd.get("choices") or []:
                    choice_names.append(ch.get("name", ""))
        all_names = " ".join([c.name] + [p["name"] for p in b["parts"]] + choice_names)
        if opts.must and not all(any(m in all_names for m in opts.must) for m in opts.must):
            continue
        valid_combos.append((c, b))

    if not valid_combos:
        conds = []
        if opts.budget:
            conds.append(f"预算 ¥{opts.budget:.0f} 内")
        if opts.must:
            conds.append(f"包含「{'、'.join(opts.must)}」")
        cond_text = "、".join(conds) if conds else "当前条件"
        print(f"**{store_line(store)}**\n\n这家店在{cond_text}下没有找到完全匹配的现成套餐，试试放宽预算或换个想吃的东西？")
        return 0

    if opts.goal == "full":
        valid_combos.sort(key=lambda x: -x[1]["kcal"])
        goal_desc = "吃饱推荐（热量与份量最足）"
    elif opts.goal == "low_kcal":
        # 过滤掉 0 大卡（可能没算全的）
        valid_combos.sort(key=lambda x: (x[1]["kcal"] == 0, x[1]["kcal"]))
        goal_desc = "控卡推荐（清爽低负担）"
    else:
        # deal: 默认按性价比（热量/价格比或最便宜）
        valid_combos.sort(key=lambda x: (-x[1]["kcal"] / max(x[0].price, 1)))
        goal_desc = "超值推荐（最实惠划算）"

    # 组成和价格都一样的只留一个 —— 「随心选」和原版套餐经常是同一套东西，
    # 不去重的话推荐位会被它占掉一半
    seen, unique = set(), []
    for c, b in valid_combos:
        key = (tuple(sorted(p["name"] for p in b["parts"])), c.price)
        if key not in seen:
            seen.add(key)
            unique.append((c, b))
    valid_combos = unique

    top = valid_combos[:3]

    lines = [f"**{store_line(store)}**", f"\n💡 **为你量身定制的动态{goal_desc}：**\n"]
    for idx, (c, b) in enumerate(top, 1):
        price_str = f"¥{c.price:.1f}"
        kcal_str = b["kcal_text"]
        parts_str = " + ".join(p["name"] for p in b["parts"])
        # 寻找套餐主图
        img_url = ""
        p_obj = engine.products.get(c.code)
        if p_obj and p_obj.image:
            img_url = p_obj.image

        lines.append(f"### 方案 {idx}：{c.name} · {price_str} · {kcal_str}")
        lines.append(f"• **包含**：{parts_str}")
        if img_url:
            lines.append(f'<img src="{img_url}" width="160" style="border-radius:8px;margin:6px 0;">')
        lines.append("")

    lines.append("想点哪套？直接告诉我方案编号或套餐名，确认后即可下单！")
    print("\n".join(lines))
    return 0


# --------------------------------------------------------------------------


def main(argv=None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if not args_list:
        print("用法：mcd.py {where,want,combo,suggest,order} [参数...]")
        return 2

    cmd = args_list[0]
    rest = args_list[1:]

    client = McDMcpClient()
    if client.demo_mode:
        print("现在没配 MCD_MCP_TOKEN，读不了实时菜单。"
              "去 https://open.mcd.cn/mcp 申请一个，然后 export 一下。")
        return 1

    if cmd == "where":
        return cmd_where(client, "".join(rest))

    saved = load_store()
    if not (saved.get("stores") or saved.get("code")):
        print("还不知道你在哪儿 —— 先说个地名，比如：mcd.py where ××市")
        return 2

    if cmd == "combo":
        return cmd_combo(client, saved, rest)
    if cmd == "suggest":
        return cmd_suggest(client, saved, rest)
    if cmd == "want":
        return cmd_want(client, saved, rest)
    if cmd == "order":
        return cmd_order(client, saved, rest)

    print(f"未知命令「{cmd}」，可选：where / want / combo / suggest / order")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
