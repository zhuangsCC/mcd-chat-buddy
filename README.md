# 🍔 麦麦懂你 (mcd-chat-buddy) · AI 极速点餐
> **面向腾讯 WorkBuddy 与主流 Agent 的麦当劳官方 MCP 生产力 Skill**
> 你在哪儿，说一声；想吃什么，说一声。价格、热量、实物图一起给你 —— 你的 AI"超级店员"。

📖 **不懂代码？看这份就够了：[使用说明（大白话版）](使用说明.md)**

[![Tencent WorkBuddy](https://img.shields.io/badge/Powered%20by-Tencent%20WorkBuddy-0052d9?style=flat-square)](#)
[![McDonald's MCP](https://img.shields.io/badge/API-McDonald's%20Official%20MCP-red?style=flat-square)](#)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9+-blue.svg?style=flat-square)](#)
[![Zero Dependency](https://img.shields.io/badge/Dependencies-0%20(Pure%20Stdlib)-brightgreen?style=flat-square)](#)
[![Tests Passing](https://img.shields.io/badge/Tests-123%20Passed-success?style=flat-square)](#)

---

## 💡 项目介绍 · 为什么需要这套 AI 流水线？

在手机 APP 点单，你必须自己翻几十页菜单算账；而有了 **mcd-chat-buddy**，你只需要对 AI 说人话：

- 🗺️ **天南海北都能点**：「我在××市××商圈」→ 自动定位最近的 5 家在营门店，读的就是**你本地那家店**的价格（示例里的「××」只是占位，用户说哪就用哪）
- 🥗 **热量价格张口就来**：「香辣鸡腿堡和薯条」→ 单价、大卡、官方实物图一起打出来；查不到的热量老实说「暂无」，**绝不编数**
- 🧸 **带娃亲子专线**：「带小孩吃有什么带玩具的？」→ 开心乐园餐连玩具一起报
- 💰 **报预算直接推荐**：「我手里有 40 块，要吃饱」/「25 块以内，来个低卡的」→ 从在售套餐里现算现排
- 🧮 **多件购买自动比价**：买 2 汉堡 2 可乐，算完账提醒你「换成套餐省 ¥9」—— 只提示不替换，换不换你说了算
- ⚡️ **极速看图决策**：官方实物原图 160px 圆角卡片，不霸屏
- 🔒 **安全下单闸门**：AI 确认前绝不下单；下单后手机麦当劳 APP 瞬间出现订单，直接手机支付

---

## ⚡️ 安装方法 · 30 秒开箱即用

### 1. 克隆到你的工作区

```bash
git clone https://github.com/zhuangsCC/mcd-chat-buddy.git
```

### 2. 配置你的麦当劳 MCP Token

前往 [麦当劳开放平台](https://open.mcd.cn/mcp) 免费获取个人密钥，并在终端配置：

```bash
export MCD_MCP_TOKEN="你的个人Token"
```

### 3. 使用示例 · 开点

底层由 `scripts/mcd.py` 驱动，AI 与人类均可直接调用：

| 场景 | 命令示例 | 特点说明 |
|:---|:---|:---|
| 智能定位 | `python3 scripts/mcd.py where ××市××商圈` | 支持复合地标，自动找最近 5 家，**打烊的自动跳过** |
| 看图问价 | `python3 scripts/mcd.py want 2个麦辣鸡腿堡 3包薯条` | 支持数量（`2个`/`薯条*3`）、小计与带图输出 |
| 套餐陈列 | `python3 scripts/mcd.py combo 龙焰` | 当季新品、四件套全息展开（价格+总热量+组成） |
| 儿童乐园餐 | `python3 scripts/mcd.py combo 儿童` | 自动识别开心乐园餐与附赠玩具 |
| 动态推荐 | `python3 scripts/mcd.py suggest --budget 40 --goal full` | 按预算 + 目标（`full` 吃饱 / `low_kcal` 控卡 / `deal` 超值）+ 必含项推荐 |
| 安全下单 | `python3 scripts/mcd.py order 麦辣鸡腿堡 薯条` | 套餐自动配好 `roundList`，订单直达麦当劳 APP |

---

## 🎯 给谁用（目标用户）

- **想点麦当劳但懒得翻菜单的人**：说一句「香辣鸡腿堡和薯条」，价格、热量、实物图直接摆桌上
- **在减肥/控卡的人**：点之前先看热量；报个预算要「低卡一点的」，它从在售套餐里现算
- **带娃的家长**：问一句「有带玩具的吗」，开心乐园餐连玩具有什么都写清楚
- **不在常住城市的人**：出差旅游，说个地名就点当地那家店，价格是**本地实价**
- **想省钱的人**：手里有 30 块要吃饱，或者买多件时想比一比套餐划不划算
- **用 AI 助手的人**：把 `SKILL.md` 交给 WorkBuddy 等 Agent，全程说人话点单

---

## 🏭 工程特性（每一条都是实测数）
- 🚀 **门店菜单 15 分钟短缓存**：首次拉全菜单 + 营养表约 **5 秒**，之后命中缓存压到 **0.07 秒**（`time` 实测）
- 📦 **零第三方依赖**：纯 Python 标准库手写（`urllib` / `json` / `unicodedata` / `argparse`），macOS / Linux / Windows 开箱即跑，连 `requirements.txt` 都没有
- 🧪 **130 个自动化单测**：口语模糊匹配、热量核算、下单闸门、仓库卫生，全部不联网、不下单，Python 3.9 与 3.13 双版本通过
- 🖼 **图片必须要有**：官方 `menu-img.mcd.cn` 图等比缩到 160px 展示，域名白名单校验，本地不存图
- 🔒 **写接口只住一个房间**：`create-order` 全仓库只准出现在 `cmd_order()` 里，有单测日夜把守

跑测试：

```bash
python3 -m unittest discover -s tests
```

---

## 🏆 赛事声明

本项目为 **2026 麦当劳程序员创意开发大赛** 参赛开源作品，深度基于腾讯 WorkBuddy 平台与麦当劳官方开放平台 MCP 服务开发构建。License: MIT。
