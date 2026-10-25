# MCP_INTEGRATION.md — 麦当劳 MCP 接入说明

## 使用的 MCP Server

- 名称：麦当劳中国 MCP（mcd-mcp）
- 端点：`https://mcp.mcd.cn`（Streamable HTTP + JSON-RPC 2.0）
- 鉴权：`Authorization: Bearer <MCD_TOKEN>`，Token 在 [open.mcd.cn/mcp](https://open.mcd.cn/mcp) 用手机号登录后申请
- 本项目不保存 Token：只从环境变量 `MCD_TOKEN` 读取，不落盘、不回显；H5 前端只访问本地 `/api/*`，不接触 Token
- 客户端实现：`app/mcp_client.py`（Python 标准库，无第三方依赖），封装 JSON-RPC 调用与混合文本解析；`mcp-tools.schema.txt` 为 `tools/list` 的实测返回快照

## 实际调用的 Tool

| Tool | 用途 | 调用位置 |
|---|---|---|
| `query-nearby-stores` | 按城市+位置关键词查门店，或查收藏门店（到店自取 beType=1） | `app/server.py`（选店、附近门店、定位反查后的门店查询） |
| `query-meals` | 拉取门店完整菜单（分类、商品、列表价） | `app/server.py`；`scripts/fetch-mcd-categories.py`（分类实测快照） |
| `query-meal-detail` | 商品详情：特制轮次 rounds、酱料等可选项（去酱、营养口径的依据） | `app/server.py` |
| `query-store-coupons` | 查询当前门店可用优惠券 | `app/domain/coupons.py` |
| `calculate-price` | 真实价格试算：券后应付、金额明细、takeWayCode | `app/domain/coupons.py`（无券基线 + 适用券逐张试算，≤8 次取最低）；智能配餐逐组方案试算 |
| `list-nutrition-foods` | 官方营养清单（热量、蛋白质、脂肪、碳水等） | `scripts/build-nutrition-mapping.py`，离线整理进 `data/nutrition-official.csv` 与映射表 |

**未调用** `create-order` / `cancel-order` / 支付相关工具：真实下单与资金操作按 PRD 有意留白，页面上只提示未接入，不做任何真实下单。

## 调用流程

### 门店点餐主链路

选店（`query-nearby-stores`）→ 菜单（`query-meals`）→ 商品详情（`query-meal-detail`，去酱/特制）→ 加购 → 自动选券：`query-store-coupons` 取券 → `calculate-price` 无券基线 + 适用券逐张试算（≤8 次）→ 取最低价 → 确认页展示券后金额与明细（堂食/外带取当次试算返回的 `takeWayCode`）。

### 智能配餐链路（配餐计算无大模型）

用户口语（人数/预算/口味）→ 本地正则解析（可选 MiniMax 仅做文本到条件的翻译，不产出任何价格/热量/营养素，也拿不到商品编码）→ 本地枚举组合 → 每组 `calculate-price` 真实试算 → 输出最多 6 个方案（价格与热量只算方案自身，不与购物车相加）→「换一套」排除已看组合，候选看尽时返回 `exhausted` 阻碍提示而非误报无解。

## 业务价值

- **数字可信**：价格、热量、营养素只来自接口输出；列表价与试算价不一致时以 `calculate-price` 券后价为准；缺官方数据明确标注「暂无官方数据」，绝不用估算顶替。
- **一料三吃**：同一业务层支撑手机 H5、本地 HTTP API 与 WorkBuddy 配餐 Skill（独立仓库 [mcd-meal-planner](https://github.com/chaoyuebro/mcd-meal-planner)，CLI 仅做 `/api/*` 薄封装，不在 CLI 内计算）。
- **可复现**：`data/` 内置菜单分类与营养实测快照；`tests/` 含 56 项离线测试与真实只读冒烟脚本，配餐关键口径（模型必选接地、份数裁剪、换一套看尽）均有测试覆盖。
