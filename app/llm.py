"""自然语言需求解析（MiniMax 文本模型）。

职责单一：把用户那句话翻译成结构化条件，**不做任何数值计算**。
价格、热量、营养素、可行性全部仍由 planner + 真实试算决定（PRD §1.4 口径不变）。

设计约束：
- 没配置 `MINIMAX_API_KEY` 时直接返回失败，调用方回退到正则解析，不影响主流程；
- 超时/报错/返回不是 JSON 都按失败处理，绝不把半截结果塞进方案；
- 提示词要求「没提到的字段一律 null」，避免模型自作主张加条件；
- 凭据只从环境变量读，不落盘、不写日志、不回显。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE = "https://api.minimax.cn/v1"
DEFAULT_MODEL = "MiniMax-M3.1-Flash"
TIMEOUT = 20

# 本机若存在 HTTP_PROXY，urllib 会把请求也发给代理；模型服务走公网，用默认 opener 即可，
# 但显式禁用代理会让部分网络环境失败，所以这里保持默认行为，不做 ProxyHandler({})。
_OPENER = urllib.request.build_opener()

# 饮食偏好短词 → 阈值（与 H5 三个快捷 chip 保持一致，避免两处口径漂移）
DIET_HINTS = {
    "highProtein": ("protein", "min", 25.0, "高蛋白"),
    "lowFat": ("fat", "max", 15.0, "低脂"),
    "lowCarb": ("carbohydrate", "max", 40.0, "低碳水"),
}

SYSTEM_PROMPT = """你是麦当劳点餐需求解析器。把用户的一句话翻译成一个 JSON 对象，只输出 JSON 本身，不要解释、不要 markdown 代码块。

输出结构（没提到的字段必须为 null 或空数组，禁止猜测、禁止补默认值）：
{
  "kcalMax": 数字或null,
  "budget": 数字或null,
  "people": 数字或null,
  "counts": {"main": 数字或null, "drink": 数字或null, "side": 数字或null},
  "macroMin": {"protein": 数字或null, "fat": 数字或null, "carbohydrate": 数字或null},
  "macroMax": {"protein": 数字或null, "fat": 数字或null, "carbohydrate": 数字或null},
  "mustKeywords": ["..."],
  "excludeKeywords": ["..."],
  "preferKeywords": ["..."],
  "dietHints": ["highProtein" 或 "lowFat" 或 "lowCarb"],
  "prefs": ["cheap" 或 "full"],
  "summary": "一句话复述用户的需求"
}

字段含义：
- kcalMax：热量上限，单位千卡。**全单只允许一个值**：用户提到多个上限（如「一个人 1000、一个人 2000」）时取最严格的那个（最小值），并在 summary 里写明「按最严格上限执行」，不要假装能分别处理。
- budget：预算上限，单位元。
- people：用餐人数。counts：明确的份数（汉堡=main，饮品/咖啡=drink，小食/薯条=side）。
  people 与 counts 二选一：说了人数填 people，说了份数填 counts。
- macroMin 只填「至少/以上/不低于」类；macroMax 只填「不超过/以内/低于」类。
- mustKeywords：用户点名想要的商品，用商品名中的连续片段（如「巨无霸」「麦香鱼」）。
- excludeKeywords：用户明确不要的，包括口味（如「辣」「香菜」）和商品。
- preferKeywords：用户偏好的口味或品类（如「牛肉」「鸡肉」「咖啡」「清淡」）。
- dietHints：仅当用户出现「高蛋白」「低脂」「低碳水/少碳水」时才填。
- prefs：仅当出现「便宜/省钱」填 cheap，「吃饱/管饱」填 full。

解析规则：
1. 只翻译用户真正说过的内容。「随便」「都行」「看着办」不产生任何条件。
2. 口味词用商品名里可能出现的短词：不吃辣→excludeKeywords 加「辣」；想吃牛肉→preferKeywords 加「牛」。
3. 不要根据常识补条件（例如用户没说热量上限，就不要填 kcalMax）。
4. summary 用中文一句话复述，不要包含你没解析出的内容。
"""


def _error(message: str) -> tuple[None, str]:
    return None, message


def _extract_json(text: str) -> dict | None:
    """模型偶尔会用 ```json 包裹或在前后加说明，这里只取第一个 JSON 对象。"""
    raw = (text or "").strip()
    if not raw:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fenced:
        raw = fenced.group(1)
    start = raw.find("{")
    if start < 0:
        return None
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(raw[start:])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _menu_hint(items: list[dict], limit: int = 80) -> str:
    names = []
    for it in items:
        name = (it.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
        if len(names) >= limit:
            break
    return "、".join(names)


def parse_request(text: str, items: list[dict]) -> tuple[dict | None, str]:
    """调用模型解析需求。返回 (结果, 错误说明)；失败时结果为 None。

    返回的字典是**待合并的候选条件**，还需要 planner 用真实菜单把它落成商品编码。
    """
    key = os.environ.get("MINIMAX_API_KEY", "").strip()
    if not key:
        return _error("未配置 MINIMAX_API_KEY")
    if not (text or "").strip():
        return _error("没有输入内容")

    base = (os.environ.get("MINIMAX_BASE") or DEFAULT_BASE).rstrip("/")
    model = os.environ.get("MINIMAX_MODEL", DEFAULT_MODEL)
    payload = {
        "model": model,
        "max_tokens": 600,
        # 结构化抽取不需要深度思考，档位调到最低以降低耗时（该模型不支持关闭思考）
        "reasoning_effort": "low",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "本店在售商品：" + _menu_hint(items) + "\n\n用户需求：" + text.strip(),
            },
        ],
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        },
    )
    try:
        with _OPENER.open(req, timeout=TIMEOUT) as res:
            body = res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except OSError:
            pass
        return _error(f"模型接口返回 {exc.code}{('：' + detail) if detail else ''}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return _error(f"模型接口不可达：{getattr(exc, 'reason', exc)}")

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return _error("模型返回不是 JSON")
    choices = data.get("choices") or []
    if not choices:
        msg = (data.get("base_resp") or {}).get("status_msg") or data.get("error") or ""
        return _error(f"模型没有返回内容{('：' + str(msg)) if msg else ''}")
    content = ((choices[0].get("message") or {}).get("content")) or ""
    parsed = _extract_json(content)
    if not parsed:
        return _error("模型返回无法解析为 JSON")
    return parsed, ""


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _int(value: Any) -> int | None:
    n = _num(value)
    if n is None or n <= 0:
        return None
    return int(n)


def _positive(value: Any) -> float | None:
    """上限类数值必须为正，0 或负数当没提（模型偶尔会输出 -1 表示"无"）。"""
    n = _num(value)
    return n if n is not None and n > 0 else None


def normalize(raw: dict) -> dict:
    """把模型输出收敛成固定结构，过滤掉非法值。只做类型清洗，不改语义。"""
    counts_raw = raw.get("counts") if isinstance(raw.get("counts"), dict) else {}
    macro_min_raw = raw.get("macroMin") if isinstance(raw.get("macroMin"), dict) else {}
    macro_max_raw = raw.get("macroMax") if isinstance(raw.get("macroMax"), dict) else {}

    def keywords(value) -> list[str]:
        if not isinstance(value, list):
            return []
        out = []
        for v in value:
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
        return out[:8]

    return {
        "kcalMax": _positive(raw.get("kcalMax")),
        "budget": _positive(raw.get("budget")),
        "people": _int(raw.get("people")),
        "counts": {
            "main": _int(counts_raw.get("main")),
            "drink": _int(counts_raw.get("drink")),
            "side": _int(counts_raw.get("side")),
        },
        "macroMin": {k: _positive(macro_min_raw.get(k)) for k in ("protein", "fat", "carbohydrate")},
        "macroMax": {k: _positive(macro_max_raw.get(k)) for k in ("protein", "fat", "carbohydrate")},
        "mustKeywords": keywords(raw.get("mustKeywords")),
        "excludeKeywords": keywords(raw.get("excludeKeywords")),
        "preferKeywords": keywords(raw.get("preferKeywords")),
        "dietHints": [h for h in keywords(raw.get("dietHints")) if h in DIET_HINTS],
        "prefs": [p for p in keywords(raw.get("prefs")) if p in ("cheap", "full")],
        "summary": (raw.get("summary") or "").strip()[:80],
    }
