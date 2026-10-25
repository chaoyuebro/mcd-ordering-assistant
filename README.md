# 麦当劳点餐助手（Skill + H5）

按 `docs/prd/PRD-001.md`（V1.6）实现。手机优先的 H5 + 共享业务层，业务口径全部来自 PRD 的 §1.4。

## 目录

```
app/
  server.py            # 最小服务：托管 H5 + 代理麦当劳 MCP（标准库，无 Web 框架）
  mcp_client.py        # MCP 客户端（Streamable HTTP + JSON-RPC，含混合文本解析）
  config.py            # 路径与配置加载
  domain/
    taxonomy.py        # 减脂范围判定（分类归一 → 组成校验 → 商品清单兜底）
    nutrition.py       # 热量计算（official / heuristic，去酱扣减、奶油球叠加）
    coupons.py         # 自动选券与试算（上限 8 次，遵守 PRD §1.4.6 顺序）
web/index.html         # 单文件 H5（内联 CSS/JS，无外部依赖）
.workbuddy/skills/
  mcd-meal-planner/    # 配餐 Skill 包（SKILL.md + CLI + 接口口径）
data/                  # 判定与营养数据（含实测快照）
scripts/               # 一次性数据抓取/生成脚本
tests/                 # 离线测试 + 真实只读冒烟
docs/                  # PRD 与需求记录
```

## Skill 包

`.workbuddy/skills/mcd-meal-planner/` 是把智能配餐交给 Agent 调用的入口，项目级生效（放在工作区内即被加载）。

```
SKILL.md              # 触发条件、工作流、硬约束（不得编造数字、不得放宽条件）
scripts/plan.py       # CLI：health / stores / menu / plan，不依赖第三方库
references/api.md     # 接口字段、口径与实测样例
```

```bash
cd .workbuddy/skills/mcd-meal-planner
python scripts/plan.py health
python scripts/plan.py plan --store 3560134 --mode lean --text "1000千卡以内，预算40元，要一杯咖啡"
```

CLI 只是 `/api/*` 的薄封装：不做任何计算，价格与热量全部来自服务端规则引擎。配餐链路里没有大模型——模型只负责收集条件与转述结果。

## 运行

```bash
# 依赖只有 pyyaml
pip install pyyaml

# Windows PowerShell
$env:MCD_TOKEN="<你的麦当劳 MCP token>"
$env:MINIMAX_API_KEY="<MiniMax key>"   # 可选：不配则输入框退回关键词解析
python app/server.py
# Git Bash / macOS
MCD_TOKEN=<token> python app/server.py
```

打开 `http://127.0.0.1:8000`。手机同网段访问 `http://<电脑局域网IP>:8000`（Windows 防火墙需放行 8000）。

> **定位限制**：浏览器只在 HTTPS 或 localhost 下提供定位。手机用局域网 HTTP 打开时，“使用当前地址”会提示不可用，需手动输入城市与关键词；想用定位就给服务加自签证书并用 https 访问。

凭据只从环境变量读取，不写入任何文件；H5 只调用本地 `/api/*`，不会接触 token。

## 已实现

- 选店（城市 + 位置关键词；无关键词时走收藏门店）、营业状态展示
- **使用当前地址**：浏览器定位 → 服务端反查城市与区县 → 查附近门店（接口只吃文本条件，不吃经纬度；坐标不落盘）
- 菜单（分类侧栏、名称搜索、图片、当前价、约整数千卡）
- **三大营养素**：卡片显示「蛋白/脂肪/碳水」（仅官方同名数据；没有时明确标「营养素暂无官方数据」，不拿 0 或估算值顶替）；购物车给整餐合计，缺数据时标 `≥` 并注明覆盖件数。去酱只扣热量，营养素保持原值
- 普通 / 减脂模式；减脂模式按 §1.4.5 三层规则过滤，不可选商品三处入口均不出现
- 加购与数量增减；购物车整餐热量（先合计再取整）
- 去酱（通过 `modification` 的 `unselectedKey` 取消酱料项，热量按 45 kcal/份扣减）
- 自动选券试算（无券基线 → 适用券逐张试算 → 取最低，≤8 次）
- 确认页：堂食/外带（`takeWayCode` 取当次试算返回）、餐具默认需要、金额明细
- 购物车与门店本地持久化（localStorage）
- **智能解析（可选）**：配餐输入框支持「人数、预算、口味与饮食偏好」的口语表达。接入 MiniMax `MiniMax-M3.1-Flash` 把整句翻成结构化条件，再按真实菜单落成商品。**模型只做翻译**——不产出价格/热量/营养素，也拿不到商品编码；正则优先、模型补空，每条 AI 条件都会在「已识别条件」里标出来。未配置 `MINIMAX_API_KEY` 时静默回退关键词解析
- **智能配餐（US-03）**：`POST /api/plan`。自然语言提要求（热量上限、预算、蛋白质/脂肪/碳水、必选、排除、便宜/吃饱），生成 ≤6 个方案（条件宽松时更多）并逐组真实试算；支持「换一套」；价格与热量只算方案自身、不与购物车相加，购物车商品只用于「不重复推荐」；无解时用真实试算价说明阻碍（口径见 PRD §1.4.11）。
  营养素筛选按上下界判定：下限条件用下界、上限条件用热量反推的硬上界（脂肪 ≤ 热量/9，蛋白碳水 ≤ 热量/4），所以一杯没有官方数据的黑咖啡不会让整批方案无解

## 未实现（有意留白）

- **创建订单 / 支付 / 取消**：涉及真实订单与资金，按 PRD 需逐次确认后才开启，当前按钮只提示未接入
- 麦乐送：接口链路尚未实测（`addressId` / 配送门店），当前只开到店自取
- 优惠的手动换券交互（US-04 的换券/恢复自动）：当前只做默认自动

## 测试

```bash
python tests/test_local.py                                  # 离线，47 项
MCD_TOKEN=<token> python tests/smoke_live.py --city 郑州 --keyword 二七广场
```

## 数据文件

| 文件 | 说明 |
|---|---|
| `data/menu-taxonomy.yaml` | 分类归一与商品清单（v3，实测上海/郑州两地） |
| `data/nutrition-mapping.csv` | 商品编码 ↔ 官方营养条目（24 项 official 带热量+蛋白/脂肪/碳水/钠/钙，其余留空待补） |
| `data/nutrition-official.csv` | 官方营养清单原始 160 条 |
| `data/estimate-params.yaml` | 去酱 45 kcal/份、奶油球 20 kcal/颗（已确认），大类兜底值 |
| `data/product-facts.json` | 商品详情缓存（rounds / 特制项），自动生成 |
| `data/menu-categories*.json` | 门店菜单分类实测快照 |

## 移动端约束

见 PRD §1.4.10：320/360/390 三档宽度无横向滚动、`100dvh`、安全区、≥44px 触控、输入框 ≥16px、键盘让位、固定栏不遮挡内容。
