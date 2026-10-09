# MCP 对接说明

麦当劳 MCP 服务端：`https://mcp.mcd.cn`
传输方式：**Streamable HTTP**（JSON-RPC 2.0 over POST，响应可能是 `text/event-stream`）
实测协议版本：`2025-06-18`，serverInfo `mcd-mcp 1.0.0`
工具总数：**35 个**，本项目用到 **7 个只读接口**，一个写接口都没碰。

---

## 一、握手

三步，`scripts/mcp_client.py` 里全手写（不用 SDK，纯标准库）：

```
POST /   initialize              → 拿 protocolVersion + serverInfo
POST /   notifications/initialized   ← 通知，没有响应体
POST /   tools/list              → 35 个工具的 schema
POST /   tools/call              → 实际调用
```

要点：

- 请求头带 `Accept: application/json, text/event-stream`，
  服务端**可能**回 JSON，**也可能**回 SSE。两种都要能解。
- `initialize` 之后必须发 `notifications/initialized`，否则后续调用可能被拒。
- 服务端是**无状态**的，不需要维护 session id。
- 鉴权：`Authorization: Bearer <MCD_MCP_TOKEN>`。

```bash
# 验证握手
python3 scripts/mcp_client.py --list

# 看某个工具的返回
python3 scripts/mcp_client.py --call query-nearby-stores '{"beType":1,"searchType":2,"city":"××市","keyword":"××市"}'
```

### SSE 分帧

响应长这样，`data:` 后面才是正文，可能分多段，最后 `data: [DONE]`：

```
event: message
data: {"jsonrpc":"2.0","id":1,"result":{...}}

data: [DONE]
```

解析时把 `data:` 行拼起来再 `json.loads`。**别照着 `\n\n\n` 切**
（不同实现的空行数不一样），按行扫更稳。

### 错误翻译

服务端返回的错误码要翻成人话，别把裸 JSON 抛给模型：

| 情况 | 翻成 |
|---|---|
| 401 / 403 | Token 无效或过期，去 open.mcd.cn/mcp 重新申请 |
| 429 | 调太快了，等几秒再试 |
| 5xx | 服务端抽风，重试一次 |
| 响应既不是 JSON 也不是 SSE | 把前 200 字贴出来，方便定位 |

---

## 二、只读工具一览

| 工具 | 干什么 | 关键点 |
|---|---|---|
| `now-time-info` | 当前时间 | **有些工具会按它做服务端时段过滤** |
| `query-nearby-stores` | 附近门店 | 直接返回 `distance`（米）和营业时间，**不用接地图 SDK** |
| `query-meals` | 菜单 | 按门店 + 当前时段过滤 |
| `query-meal-detail` | 餐品详情 | 套餐的 `rounds[]`、特制项都在这 |
| `list-nutrition-foods` | 营养表 | **不是 JSON**，是一整段纯文本 |
| `query-store-coupons` | 门店优惠券 | |
| `calculate-price` | 算实付价 | 改单后必须重算 |

写接口（`create-order` / `cancel-order` / `mall-create-order`）本项目**从不调用**，
只有用户在当轮明确说过「确认」才会碰 —— 见 `SKILL.md` 第六节。

---

## 三、踩过的坑

这部分是真正花时间的地方，按遇到的顺序记。

### 1. `query-meals` 的 `categories[].meals[]` 里没有名字

第一次拿到菜单只有 `{code, tags}`，连名字都没有。

原因是**真实数据在 `data.meals{code}` 这个字典里**，
`categories[]` 只是索引（哪一类下有哪些 code）：

```
data.categories[]           →  { categoryName, meals: [{code, tags}] }
data.meals{"4810"}          →  { name, price, image, ... }   ← 真正的数据
```

**要按 code 做 join。** 只看 `categories[]` 会得到一堆没有名字的 code。

### 2. 营养表是纯文本，不是 JSON

`list-nutrition-foods` 返回的是：

```
[160]{食物名称}:{能量(kJ)},{蛋白质(g)},{脂肪(g)},{碳水化合物(g)},{钠(mg)},{钙(mg)}...
```

`[160]` 是行数，后面 160 行逗号分隔。**得自己拆。**

拆完立刻遇到名字对不上的问题：

| 菜单 | 营养表 | 问题 |
|---|---|---|
| `巨无霸™` | `巨无霸` | 商标符号 |
| `麦乐鸡-4块` | `麦乐鸡4块` | 连字符 |
| `苹果片一袋(60g)` | `苹果片一袋（60g）` | 全角/半角括号 |
| `可乐` | `可乐中杯` / `可乐大杯` | 规格后缀 |

统一做归一化：去空格、去连字符、去全角半角差异、NFKC。

> **顺序陷阱：必须先去掉 `™®©`，再跑 NFKC。**
> 因为 NFKC 会把 `™` 规范化成字母 `TM`，`巨无霸™` 就变成 `巨无霸TM`，
> 从此永远配不上 `巨无霸`。这个 bug 很难查 —— 因为 `®` 恰好会被 NFKC 直接吃掉，
> 所以只测 `®` 的话看不出问题。

### 3. 套餐没有热量，得自己加

营养表里只有单品。套餐要按 `query-meal-detail` 的 `rounds[]` 拆成子项
（`rounds[].choices[]` 里 `isDefault=1` 的是默认选中的），
逐个配营养表再相加。

两个额外规则：

- **玩具不参与合计。** 儿童套餐里有一档是玩具（`海绵宝宝×航海王系列玩具`），
  它有独立的 `rounds` 槽位。识别方式是**看结构不看名字** ——
  玩具名字每季都换，但「有一个必选的非食品档位」这个结构不会变。
  判定顺序也有讲究：**先查营养表，再判非食品**。
  因为 `苹果片一袋(60g)` 名字里有「袋」，先判非食品会被误杀。
- **配不上的就标「热量暂无」。** 季节限定、蘸酱在 160 行表里确实没有。
  合计要注明是**下限**，绝不能把「不知道」算成 0。

### 4. `roundList` 的字段名写错会静默返回 `null`

给套餐算价时要指定每一档选了什么，结构是：

```json
{
  "storeCode": "1400200",
  "orderType": 1,
  "beType": 1,
  "items": [{
    "productCode": 9900005456,
    "quantity": 1,
    "roundList": [
      {"round": "1", "comboItemList": [{"code": "1440", "quantity": 1}]},
      {"round": "2", "comboItemList": [{"code": "4810", "quantity": 1}]},
      {"round": "3", "comboItemList": [{"code": "521817", "quantity": 1}]}
    ]
  }]
}
```

三个坑：

- **字段名是 `round` + `comboItemList`**，
  不是 `roundId` + `choices`。写成后者**不报错，直接返回 `price: null`** ——
  很难查，因为请求本身是成功的。
- **不带 `roundList` 也能算出来**，拿的是「全默认」的价（这家店三件套 3400 分）。
  所以想验证 `roundList` 有没有生效，**必须挑一个带差价的选项**：
  饮料档从默认的「可乐中杯」换成「美汁源多汁柠柠」（`+ ¥1`），
  价格 3400 → 3500 才对。
- `round` 传 `"1"` 还是 `1` 现在都能过（两个都试了，都是 3400）。
  代码里统一用字符串，稳妥。

每档的 `diffPrice` 形如 `"+ ¥1"`，得自己转成分。

### 5. `query-meal-detail` 少一个参数就返回空

这个接口的参数是 **`storeCode` + `orderType` + `beType` + `code`**，四个都要。

少传 `storeCode` 时不会报错，返回里 `data.rounds` 是空的 ——
看起来像「这个套餐没有选配」，实际是参数没给全。

> 顺带：`query-meal-detail` 的返回里 `structuredContent` 是**有**的，
> 用 `structured_of()` 直接能取到 `data`。
> 但如果哪天拿不到了，它的 `content[].text` 里也藏着一份
> （在 `## Original Response` 那段），可以用 `find_json_blob()` 兜。

### 6. 套餐内部的 SKU 和单点 SKU 不是同一个

同一个东西，在套餐内部有内部 code，单点有另一个 code。

表现是：拿套餐里的 code 去按单点算价，**价格是 0**。
比如 `3050 可乐中杯` 作为单品算出来 0 元。

要按名字回菜单里找单点的那个 code。`menu_engine.find_product()` 就是干这个的，
三级兜底：**code 直查 → 名字精确 → 归一化后包含匹配（取名字最长的那个）**。

### 6b. `items[]` 里的编码字段叫 `productCode`，不叫 `code`

写错**不报错**，`code` 还是 200、返回结构完整，但 `price` 是 **0**。
比套餐 SKU 那个坑更阴：连「是不是拿错了 code」的怀疑方向都不会有。

```python
# ✗ 全是 0：price=0 originalPrice=0 productList=[]
{"items": [{"code": "4810", "quantity": 1}]}

# ✓ 2900 分
{"items": [{"productCode": "4810", "quantity": 1}]}
```

排查方法：看 `data.productList` —— 它是空的就说明**一个 item 都没被认出来**，
大概率是字段名写错了，不是门店问题、也不是 code 问题。
（实测拿 `code` 写法在两家店都是 0，换门店排查只会白跑。）

### 7. `calculate-price` 不校验特制参数

传进去的 `modification` 参数它不看，照样返回价格。

**所以不能说「已经帮你改成不要洋葱了」** —— 价格算对了不代表改动生效了。
只能说「帮你备注不要洋葱」。

### 8. 有时段过滤，别缓存菜单

`query-meals` 返回的菜单跟**当前时间**有关：早餐时段拿不到正餐，反过来也一样。

所以菜单**不落盘当缓存**（`mock/query-meals.json` 只是演示快照，且会标注拉取时间）。
唯一缓存的是**套餐目录**（`mock/combo-catalog.json`），
因为套餐构成不随时段变，而拉一次要几十个请求。

### 9. 图片是官方图床，但 `file://` 加载不了 `file://`

`query-meals` 每样商品自带 `image`，
指向 `https://menu-img.mcd.cn/.../MS_xxxx_320.png`，427×320。
**覆盖率 100%**（实测 123～125 项，随门店和时段浮动，每次都是全量带图）
—— 不用自己找素材图。

坑在出图这步：Chrome 打开本地 HTML 时，页面里 `<img src="file://...">`
会被同源策略拦掉，图全白。**只能转成 base64 data URI 内联进 HTML。**
好处是出来的卡片是单文件，能直接发微信。

> `_320` 换成 `_600` 想拿大图 → 404。只有 320 那一档是公开的。

### 10. 图片域名校验别用子串

```python
# 错：menu-img.mcd.cn.evil.com 也会通过
if "menu-img.mcd.cn" in url

# 对
urlparse(url).hostname == "menu-img.mcd.cn"
```

域名白名单一定要比对 `urlparse().hostname`，而且只允许 `https`。

---

## 四、演示模式（没有 Token 时）

配了 `MCD_MCP_TOKEN` 就走真实接口。**没配时 `mcd.py` 直接告诉你读不了菜单** ——
不给任何编出来的价格：

```
现在没配 MCD_MCP_TOKEN，读不了实时菜单。
去 https://open.mcd.cn/mcp 申请一个，然后 export 一下。
```

`mock/` 里存的是**真实接口的原始返回**（不是手编的假数据），
测试用它离线跑，不联网也能验证解析逻辑：

```bash
python3 -m unittest discover -s tests
```

---

## 五、自己验一遍

```bash
# 1. 握手 + 列工具
python3 scripts/mcp_client.py --list

# 2. 看营养表原始返回（最容易出问题的一个）
python3 scripts/mcp_client.py --call list-nutrition-foods '{}' | head -40

# 3. 定门店 → 查价格热量（不会下单）
python3 scripts/mcd.py where ××市
python3 scripts/mcd.py want 香辣鸡腿堡 薯条

# 4. 单测 —— 不联网、不下单
python3 -m unittest discover -s tests
```
