"""自动选券与试算（PRD §1.4.6）。

券挂在**单个商品**上（items[].couponId / couponCode），没有订单级券参数，
所以“应付最低”只能靠多次试算比较。这里按 PRD 的固定顺序执行并遵守试算上限：

1. 先取无券基线价；
2. 用券的适用商品（coupon.products[].productCode）与购物车求交集，过滤掉明显不适用的券；
3. 每张候选券单独挂到其适用商品上试算，取应付最低；
4. 单次操作试算次数上限（默认 8 次，含基线），达上限时采用已试结果并标注已比较张数。
"""

from __future__ import annotations

import copy
import time

from ..mcp_client import MCDClient, MCDError

MAX_TRIALS = 8
# 单次选券的总时长上限（秒）。每张券都要调一次门店试算，接口一慢就会「试算中…」
# 卡到天荒地老；超过上限就用已试出的最优结果收尾，并注明未穷举。
DEADLINE_SECONDS = 25.0


def _calc(client: MCDClient, store_code: str, be_type: int, order_type: int, items: list[dict], **extra) -> dict:
    args = {
        "storeCode": store_code,
        "beType": be_type,
        "orderType": order_type,
        "items": items,
        **extra,
    }
    return client.data("calculate-price", args) or {}


def select_best(
    client: MCDClient,
    *,
    store_code: str,
    items: list[dict],
    be_type: int = 1,
    order_type: int = 1,
    need_tableware: bool | None = None,
    budget: int = MAX_TRIALS,
    deadline_seconds: float = DEADLINE_SECONDS,
) -> dict:
    """items: [{"productCode","quantity",...}]；返回应付最低方案与比较过程。"""
    extra = {} if need_tableware is None else {"needTableware": need_tableware}
    started = time.monotonic()
    timed_out = False

    baseline = _calc(client, store_code, be_type, order_type, items, **extra)
    best = {
        "cents": baseline.get("price"),
        "data": baseline,
        "coupon": None,
        "couponTitle": None,
    }
    tried = 1
    coupons_total = 0
    coupon_error = None

    try:
        coupons = client.data(
            "query-store-coupons",
            {"storeCode": store_code, "beType": be_type, "orderType": order_type},
        ) or []
    except MCDError as exc:
        # 优惠查询失败不能等同于无券（PRD US-04 场景四）
        return {
            **best,
            "tried": tried,
            "couponsTotal": 0,
            "couponError": str(exc),
            "note": "优惠暂未确认，当前为无券试算结果",
        }

    coupons_total = len(coupons)
    cart_codes = [it["productCode"] for it in items]

    for coupon in coupons:
        if tried >= budget:
            break
        if deadline_seconds and time.monotonic() - started > deadline_seconds:
            timed_out = True
            break
        applicable = {p.get("productCode") for p in (coupon.get("products") or []) if p.get("productCode")}
        idx = next(
            (i for i, code in enumerate(cart_codes) if not applicable or code in applicable),
            None,
        )
        if idx is None:
            continue

        trial = copy.deepcopy(items)
        trial[idx]["couponId"] = coupon.get("couponId")
        trial[idx]["couponCode"] = coupon.get("couponCode")
        try:
            data = _calc(client, store_code, be_type, order_type, trial, **extra)
        except MCDError:
            tried += 1
            continue
        tried += 1

        cents = data.get("price")
        if isinstance(cents, int) and isinstance(best["cents"], int) and cents < best["cents"]:
            best = {
                "cents": cents,
                "data": data,
                "coupon": coupon,
                "couponTitle": coupon.get("title"),
            }

    if coupons_total == 0:
        note = "暂无可用券，已按官方基础价试算"
    elif tried == 1:
        note = "返回 %d 张券，均不适用于当前餐品，已按无券试算" % coupons_total
    else:
        note = "已比较 %d 张券（共返回 %d 张）、%d 次试算" % (
            min(tried - 1, coupons_total),
            coupons_total,
            tried,
        )
    if tried >= budget:
        note += "；已达试算上限，未穷举全部组合"
    elif timed_out:
        note += "；已用 %.0f 秒完成 %d 次试算，剩余券未比较" % (time.monotonic() - started, tried)

    return {
        **best,
        "tried": tried,
        "couponsTotal": coupons_total,
        "couponError": coupon_error,
        "note": note,
    }
