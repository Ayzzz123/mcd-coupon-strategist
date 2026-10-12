#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦麦省钱助手 —— 零配置演示。

不需要 MCP Token、不联网、不装任何第三方库：

    python demo.py

它读取 fixtures/ 里**录制的真实 MCP 响应**，跑通两条链路，并打印与真实
使用时完全相同的输出。

真实运行时，这些响应由 Skill 编排层通过麦当劳 MCP 实时获取
（query-meals / query-store-coupons / order-list / calculate-price）。
本脚本只是把「实时」换成「录播」，业务逻辑一字未改 ——
所以它既是演示，也是这套数据流水线的端到端回归测试。

数据来源与录制时间见 fixtures/README.md。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src"))

from auditor import audit_report, render_audit  # noqa: E402
from mcd_adapter import (  # noqa: E402
    build_menu_price_table,
    parse_coupon_stock,
    parse_menu,
    parse_orders,
    parse_price_response,
)
from saver import (  # noqa: E402
    Plan,
    PricedPlan,
    build_base_plans,
    dedupe_plans,
    detect_filler_trap,
    rank,
    render,
)

FIXTURES = os.path.join(HERE, "fixtures")

# 录制的门店与日期。写死是为了让 demo 的输出可复现。
STORE_NAME = "麦当劳南京东城汇餐厅(1990622)"
FIXTURE_TODAY = date(2026, 10, 12)

# demo 场景用到的商品编码（均来自 fixtures/menu.json 的真实菜单）
CODE_BURGER = "1440"          # 麦辣鸡腿汉堡   22.5
CODE_COMBO3 = "9900005456"    # 麦辣鸡腿汉堡三件套 33.5
CODE_FRIES = "4810"           # 薯条           14.5
CODE_COLA = "9900008751"      # 可乐            9.5

BUDGET = 40.0


def load(name: str) -> dict:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


MENU = load("menu.json")
COUPONS = load("coupons.json")
ORDERS = load("orders.json")
PRICE_BOOK: dict = load("prices.json")["prices"]


# --------------------------------------------------------------------------
# 计价：用录制的 calculate-price 响应
# --------------------------------------------------------------------------


def signature(plan: Plan) -> str:
    """商品组合的规范签名，与 fixtures/prices.json 的 key 一一对应。"""
    return "+".join(
        f"{i.code}:{i.qty}" for i in sorted(plan.items, key=lambda x: x.code)
    )


def recorded_price(plan: Plan) -> PricedPlan:
    """用录制的 calculate-price 响应计价。

    这不是「本地估算」—— 每个数字都出自真实调用 calculate-price 的返回，
    只是被录制下来了，所以没有 Token 也能复现。

    组合若没有录制结果，**直接报错**，绝不猜一个价格出来 ——
    这与项目「绝不猜测价格」的铁律一致。
    """
    sig = signature(plan)
    resp = PRICE_BOOK.get(sig)
    if resp is None:
        raise KeyError(
            f"fixtures/prices.json 缺少组合 [{sig}] 的录制计价结果。\n"
            f"请用真实 MCP 调用 calculate-price 后补充，不要手工估算。"
        )
    return parse_price_response(plan, resp)


# --------------------------------------------------------------------------
# 链路 A · 单次省钱
# --------------------------------------------------------------------------


def chain_a() -> None:
    print("=" * 66)
    print("链路 A · 单次省钱")
    print("=" * 66)
    print(f"门店：{STORE_NAME}（到店自取）")
    print("需求：想吃「汉堡 + 薯条 + 可乐」，预算 40 元")
    print()

    mains, sides = parse_menu(MENU)
    by_code = {i.code: i for i in [*mains, *sides]}

    burger, combo3 = by_code[CODE_BURGER], by_code[CODE_COMBO3]
    fries, cola = by_code[CODE_FRIES], by_code[CODE_COLA]

    # 枚举候选：单品 / 单品+配餐 / 套餐
    plans = build_base_plans([burger], [fries, cola], [[combo3]])
    for p in plans:
        # build_base_plans 给套餐的标签是「套餐方案 1」，换成可读名称
        if p.items and p.items[0].code == CODE_COMBO3:
            p.label = f"套餐：{combo3.name}"
    # build_base_plans 只枚举「主食」与「主食 + 1 个配餐」。
    # 需求要三样齐全，故显式补上「主食 + 2 个配餐」的等价单点组合，
    # 用于与三件套做**同内容**对比。（有券时 build_filler_plans 会枚举这一类。）
    plans.append(
        Plan(items=[burger, fries, cola], label="单点：麦辣鸡腿汉堡 + 中薯条 + 可乐")
    )

    priced = [recorded_price(p) for p in dedupe_plans(plans)]

    # 需求约束：三样必须齐全 —— 这正是不推荐「更便宜但吃不饱」选项的原因
    def satisfies(plan: Plan) -> bool:
        codes = {i.code for i in plan.items}
        return CODE_COMBO3 in codes or {CODE_BURGER, CODE_FRIES, CODE_COLA} <= codes

    satisfying = rank([p for p in priced if satisfies(p.plan)])

    result = {
        "recommended": satisfying,
        "warnings": detect_filler_trap(priced),
        "best": satisfying[0] if satisfying else None,
    }
    print(render(result))

    if len(satisfying) >= 2:
        best, alt = satisfying[0], satisfying[-1]
        print()
        print(
            f"  → 同样吃到「汉堡 + 薯条 + 可乐」："
            f"{best.payable} 元 vs {alt.payable} 元，"
            f"差 {round(alt.payable - best.payable, 2)} 元。"
        )
        print("     这个价差来自套餐结构本身，不需要任何优惠券。")
    print()


# --------------------------------------------------------------------------
# 链路 B · 消费审计
# --------------------------------------------------------------------------


def chain_b() -> None:
    print("=" * 66)
    print("链路 B · 消费审计")
    print("=" * 66)
    print()

    raw = ORDERS["data"]["list"]
    orders = parse_orders(ORDERS)
    refunded = len(raw) - len(orders)

    menu_prices = build_menu_price_table(MENU)
    stock = parse_coupon_stock(COUPONS, menu_prices=menu_prices)

    # 只统计近 90 天
    since = FIXTURE_TODAY - timedelta(days=90)
    recent = [o for o in orders if o.order_time >= since]

    print(
        f"  历史订单 {len(raw)} 条 → 过滤「整单退款」{refunded} 条 "
        f"→ 近 90 天 {len(recent)} 条计入统计"
    )
    print()

    report = audit_report(recent, stock, months=3, today=FIXTURE_TODAY)
    print(render_audit(report))
    print()


def main() -> None:
    print()
    print("麦麦省钱助手 · 零配置演示")
    print(f"数据：fixtures/ 中录制的真实 MCP 响应（{FIXTURE_TODAY} 采集）")
    print()
    chain_a()
    chain_b()


if __name__ == "__main__":
    main()
