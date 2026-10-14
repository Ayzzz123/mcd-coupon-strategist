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
    parse_available_coupons,
    parse_claimable_coupons,
    parse_combo_from_detail,
    parse_coupon_probes,
    parse_coupon_stock,
    parse_menu,
    parse_orders,
    parse_points_account,
    parse_points_products,
    parse_price_response,
)
from planner import (  # noqa: E402
    audit_claimable,
    audit_coupon_portfolio,
    order_frequency,
    plan_redemption,
    render_claimable,
    render_portfolio,
    render_schedule,
)
from points import audit_points, render_points  # noqa: E402
from saver import Plan, PricedPlan, recommend, render  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")

# 录制的门店与日期。写死是为了让 demo 的输出可复现。
STORE_NAME = "麦当劳南京东城汇餐厅(1990622)"
FIXTURE_TODAY = date(2026, 10, 12)
# 积分数据是另一批采集（10-14），按它自己的日期判断兑换截止日
POINTS_TODAY = date(2026, 10, 14)

# demo 场景用到的商品编码（均来自 fixtures/menu.json 的真实菜单）
CODE_BURGER = "1440"          # 麦辣鸡腿汉堡   22.5
CODE_COMBO3 = "9900005456"    # 麦辣鸡腿汉堡三件套 33.5
CODE_FRIES = "4810"           # 薯条           14.5
CODE_COLA = "9900008751"      # 可乐            9.5

# 套餐内含饮品的编码与菜单单点不同：三件套里的可乐是 3050（可乐中杯）。
# 判断「三样齐全」时两者都要认，否则套餐会被误判为不满足需求。
COLA_CODES = {CODE_COLA, "3050"}

BUDGET = 40.0

# 消费画像的观察窗口：既用于圈定统计区间，也用于推算点单频率
OBSERVATION_DAYS = 90


def load(name: str) -> dict:
    """读取 fixtures/ 下的一个 JSON 文件。"""
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


MENU = load("menu.json")
COUPONS = load("coupons.json")
ORDERS = load("orders.json")
PRICE_BOOK: dict = load("prices.json")["prices"]
# 券价值的「带券计价」实测结果，比菜单反查精确（详见 fixtures/README.md）
COUPON_PROBES: dict = load("coupon_probe.json")["probes"]
# 麦麦省券列表（含还没领取的）。该工具返回 Markdown 文本，不是 JSON
AVAILABLE = load("available_coupons.json")
# 套餐构成 —— 让套餐知道自己装了什么（详见 fixtures/meal_detail.json）
MEAL_DETAIL = load("meal_detail.json")
# 积分账户与积分商城（另一批采集：2026-10-14）
POINTS_ACCOUNT = load("points_account.json")
POINTS_PRODUCTS = load("points_products.json")


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
    """链路 A：走 saver.recommend() 的完整流水线 —— 枚举、计价、需求筛选、排序。"""
    print("=" * 66)
    print("链路 A · 单次省钱")
    print("=" * 66)
    print(f"门店：{STORE_NAME}（到店自取）")
    print(f"需求：想吃「汉堡 + 薯条 + 可乐」，预算 {BUDGET:.0f} 元")
    print()

    mains, sides = parse_menu(MENU)
    by_code = {i.code: i for i in [*mains, *sides]}

    burger = by_code[CODE_BURGER]
    fries = by_code[CODE_FRIES]
    cola = by_code[CODE_COLA]
    combo3 = by_code[CODE_COMBO3]

    # 让套餐知道自己装了什么 —— 内含单品来自真实的 query-meal-detail 响应。
    # 有了它，covered_codes() 才能把「套餐」和「单点组合」放在同一把尺子上量。
    combo3.parts = parse_combo_from_detail(MEAL_DETAIL)

    def wants(plan: Plan) -> bool:
        """需求：汉堡 + 薯条 + 可乐，三样齐全（套餐内含单品也算）。"""
        codes = plan.covered_codes()
        return (
            CODE_BURGER in codes
            and CODE_FRIES in codes
            and bool(codes & COLA_CODES)
        )

    # max_sides=2：需求要三样齐全，必须枚举出「主食 + 两个配餐」这条对照组合，
    # 否则只有套餐能满足需求，也就无从对比「同样三样，单点要多少钱」。
    result = recommend(
        mains=[burger],
        sides=[fries, cola],
        combos=[[combo3]],
        coupons=[],
        price_fn=recorded_price,
        budget=BUDGET,
        top_k=2,
        demand=wants,
        max_sides=2,
    )
    print(render(result))

    # 超预算的那条不是「备选」，是对照 —— 它承载了本项目最核心的结论
    if result["over_budget"]:
        print()
        print("  → 这个价差来自套餐结构本身，不需要任何优惠券 —— 这是最稳的省法。")
    print()


# --------------------------------------------------------------------------
# 链路 B · 消费审计
# --------------------------------------------------------------------------


def chain_b() -> None:
    """链路 B：从历史订单与券存量生成消费审计报告。"""
    print("=" * 66)
    print("链路 B · 消费审计")
    print("=" * 66)
    print()

    raw = ORDERS["data"]["list"]
    orders = parse_orders(ORDERS)
    refunded = len(raw) - len(orders)

    menu_prices = build_menu_price_table(MENU)
    # 券价值优先用「带券计价」实测，测不到再退回菜单反查
    stock = parse_coupon_stock(
        COUPONS,
        menu_prices=menu_prices,
        probes=parse_coupon_probes(COUPON_PROBES),
    )

    # 只统计近 90 天
    since = FIXTURE_TODAY - timedelta(days=OBSERVATION_DAYS)
    recent = [o for o in orders if o.order_time >= since]

    print(
        f"  历史订单 {len(raw)} 条 → 过滤「整单退款」{refunded} 条 "
        f"→ 近 90 天 {len(recent)} 条计入统计"
    )
    print()

    report = audit_report(recent, stock, months=3, today=FIXTURE_TODAY)
    print(render_audit(report))
    print()

    # 券包体检：在「告诉你正在白扔钱」之上再走一步 —— 这笔钱该怎么不白扔。
    # 消费基准用历史订单的客单价，这是一个保守近似，已在报告中标注。
    portfolio = audit_coupon_portfolio(
        stock,
        natural_spend=report["profile"]["avg_per_order"],
        today=FIXTURE_TODAY,
    )
    print(render_portfolio(portfolio))
    print()

    # 用券排程：可实现的券，未必用得上 —— 券过期前你只剩几次点单机会。
    # 频率由近 90 天的历史订单推算，与统计窗口保持一致。
    schedule = plan_redemption(
        stock,
        natural_spend=report["profile"]["avg_per_order"],
        frequency=order_frequency(recent, OBSERVATION_DAYS),
        today=FIXTURE_TODAY,
    )
    print(render_schedule(schedule))
    print()

    # 待领取的券：你的券源不止手里这些 —— 麦麦省里还有没领的。
    # 这批券没有 couponId（该工具不返回），因此「带券计价实测」用不上，
    # 只能靠菜单反查；查不到就如实标「未知」，不猜。
    all_coupons = parse_available_coupons(AVAILABLE)
    claimable = parse_claimable_coupons(AVAILABLE, menu_prices=menu_prices)
    claimed_count = sum(1 for c in all_coupons if c.claimed is True)

    print(
        f"  麦麦省共 {len(all_coupons)} 条券记录：已领取 {claimed_count} 条、"
        f"可领取 {len(claimable)} 条（同名去重后）"
    )
    print()

    claim_report = audit_claimable(
        claimable, natural_spend=report["profile"]["avg_per_order"]
    )
    # 排程已用掉的机会数 —— 用来提醒「领来的券同样要抢这些机会」
    remaining_slots = schedule.opportunities - len(schedule.scheduled)
    print(render_claimable(claim_report, remaining_slots=remaining_slots))
    print()


def points_section() -> None:
    """积分体检：余额、有效期、能换什么、够不够。"""
    account = parse_points_account(POINTS_ACCOUNT)
    items = parse_points_products(POINTS_PRODUCTS)
    print(render_points(audit_points(account, items, today=POINTS_TODAY)))
    print()


def main() -> None:
    """依次跑通两条链路，再做一次积分体检。"""
    print()
    print("麦麦省钱助手 · 零配置演示")
    print("数据：fixtures/ 中录制的真实 MCP 响应")
    print("      （主链路 2026-10-12 采集，积分部分 2026-10-14 采集）")
    print()
    chain_a()
    chain_b()
    points_section()


if __name__ == "__main__":
    main()
