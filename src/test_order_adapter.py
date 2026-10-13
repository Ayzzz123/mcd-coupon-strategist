"""订单与券库存解析的自测 —— 使用真实 MCP 响应片段。

数据来源：2026-10-10 对麦当劳官方 MCP Server 的实际调用
（order-list 返回 10 条历史订单；query-my-coupons 返回 13 张券）。

运行：
    python src/test_order_adapter.py
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auditor import audit_report, render_audit  # noqa: E402
from mcd_adapter import (  # noqa: E402
    build_menu_price_table,
    coupon_real_value,
    parse_coupon_probes,
    parse_coupon_stock,
    parse_orders,
)


# 真实菜单价格（摘自 query-meals 实际返回）
REAL_MENU_PRICES = {
    "中薯条": 14.5,
    "薯条": 14.5,
    "麦咖啡™美式": 11.9,
    "麦咖啡™奶铁": 13.9,
    "经典麦旋风": 14.5,
    "脆薯饼": 8.0,
    "圆筒冰淇淋": 5.0,
    "可乐": 9.5,
}


REAL_ORDERS = {
    "success": True,
    "data": {
        "list": [
            {
                "orderId": "A1",
                "orderType": "1",
                "createTime": "2026-10-02 15:57:36",
                "beType": "1",
                "storeCode": "1990622",
                "storeName": "麦当劳南京东城汇餐厅",
                "orderStatus": "订单已完成",
                "orderProductList": [
                    {
                        "productCode": "9900013476",
                        "productName": "随心配1+1",
                        "quantity": 1,
                        "comboItemList": [
                            {"productCode": "1600", "name": "麦香鱼", "quantity": 1},
                            {"productCode": "3050", "name": "可乐中杯", "quantity": 1},
                        ],
                    }
                ],
                "realTotalAmount": "1",
            },
            {
                "orderId": "A2",
                "orderType": "1",
                "createTime": "2026-09-24 18:23:57",
                "beType": "1",
                "storeCode": "1990068",
                "storeName": "得来速餐厅",
                "orderStatus": "整单退款",
                "orderProductList": [
                    {
                        "productCode": "521901",
                        "productName": "月下相思扭扭薯",
                        "quantity": 1,
                        "comboItemList": [],
                    }
                ],
                "realTotalAmount": "15.5",
            },
            {
                "orderId": "A3",
                "orderType": "1",
                "createTime": "2026-08-16 21:27:35",
                "beType": "1",
                "storeCode": "1990068",
                "storeName": "得来速餐厅",
                "orderStatus": "订单已完成",
                "orderProductList": [
                    {
                        "productCode": "1700",
                        "productName": "麦辣鸡翅2块",
                        "quantity": 1,
                        "comboItemList": [],
                    },
                    {
                        "productCode": "9900015264",
                        "productName": "蘸酱炸鸡五选一",
                        "quantity": 2,
                        "comboItemList": [],
                    },
                ],
                "realTotalAmount": "23.8",
            },
        ]
    },
}

REAL_COUPONS = {
    "success": True,
    "data": [
        {"title": "中薯条买一送一", "couponId": "C1",
         "tradeDateTime": "2026-10-12 10:30-2026-10-16 23:59"},
        {"title": "免费脆薯饼", "couponId": "C2"},
        {"title": "人气麦旋风买一送一", "couponId": "C3"},
        {"title": "9.9元中杯冰美式", "couponId": "C4"},
    ],
}


def main() -> None:
    """用真实订单/券响应验证解析、退款过滤与报告渲染。"""
    print("=" * 60)
    print("1. 订单解析（含退款过滤）")
    print("=" * 60)

    orders = parse_orders(REAL_ORDERS)
    for o in orders:
        print(f"   {o.order_time}  {o.amount:>6} 元  [{o.channel}]  {'/'.join(o.items)}")

    assert len(orders) == 2, "3 条订单中有 1 条整单退款，应被过滤，剩 2 条"
    assert not any(o.amount == 15.5 for o in orders), "退款单不应计入"

    a1 = next(o for o in orders if o.amount == 1.0)
    assert a1.channel == "到店", "beType=1 应映射为到店"
    assert "随心配1+1" in a1.items

    a3 = next(o for o in orders if o.amount == 23.8)
    assert a3.items.count("蘸酱炸鸡五选一") == 2, "数量为 2 的商品应展开两次"

    total = sum(o.amount for o in orders)
    assert total == 24.8, f"过滤退款后总额应为 1 + 23.8 = 24.8，实际 {total}"
    print(f"   [PASS] 退款单已过滤，总额 {total} 元（未包含退款的 15.5 元）")
    print()

    print("=" * 60)
    print("2. 券价值推算（从菜单反查原价）")
    print("=" * 60)

    # 满减券：直接取减免额
    assert coupon_real_value("满50减12", REAL_MENU_PRICES) == 12.0
    # 买一送一：价值 = 商品单价
    assert coupon_real_value("中薯条买一送一", REAL_MENU_PRICES) == 14.5, \
        "中薯条买一送一应价值 14.5 元（薯条单价）"
    # 固定价券：价值 = 原价 - 固定价
    v = coupon_real_value("9.9元中杯冰美式", {"中杯冰美式": 23.0})
    assert v == 13.1, f"9.9元冰美式应价值 13.1 元，实际 {v}"
    # 无法匹配的券：保守为 0，不编造
    assert coupon_real_value("神秘券", REAL_MENU_PRICES) == 0.0, \
        "无法匹配菜单的券价值应为 0，不可估算"
    print("   满50减12          → 12.0 元（直接减免）")
    print("   中薯条买一送一     → 14.5 元（菜单反查薯条单价）")
    print("   9.9元中杯冰美式    → 13.1 元（原价 23 - 9.9）")
    print("   神秘券            →  0.0 元（无匹配，保守不估算）")
    print("   [PASS] 三种券形态价值推算正确，无法推算时保守置 0")
    print()

    print("=" * 60)
    print("3. 券库存解析（带菜单价格表）")
    print("=" * 60)

    stock = parse_coupon_stock(REAL_COUPONS, menu_prices=REAL_MENU_PRICES)
    for c in stock:
        exp = c.expire_date.isoformat() if c.expire_date else "无期限"
        print(f"   {c.name:<20} 价值 {c.value:>5} 元  到期 {exp}")

    assert len(stock) == 4, "应解析出 4 张券"

    c1 = next(c for c in stock if c.coupon_id == "C1")
    assert c1.expire_date == date(2026, 10, 16), "应从区间中取最后一个日期作为截止日"
    assert c1.value == 14.5, "中薯条买一送一应价值 14.5 元"

    # 无法匹配菜单的券，价值必须为 0（不编造）
    c2 = next(c for c in stock if c.coupon_id == "C2")
    assert c2.value == 0.0, "「免费脆薯饼」需原价匹配，未提供时应为 0"

    total_value = sum(c.value for c in stock)
    print(f"   券面总价值: {total_value} 元")
    assert total_value == 14.5, f"仅买一送一券可算出价值，合计应为 14.5，实际 {total_value}"
    print("   [PASS] 有效期与价值解析正确")
    print()

    print("=" * 60)
    print("4. 完整菜单价格表构建")
    print("=" * 60)
    table = build_menu_price_table({
        "data": {"meals": {
            "4810": {"name": "薯条", "currentPrice": "14.5"},
            "9900008751": {"name": "可乐", "currentPrice": "9.5"},
            "bad": {"name": "", "currentPrice": "5"},
        }}
    })
    assert table == {"薯条": 14.5, "可乐": 9.5}, f"应构建出 2 项价格，实际 {table}"
    print(f"   {table}")
    print("   [PASS] 价格表构建正确，空名/异常项已跳过")
    print()

    print("=" * 60)
    print("5. 券价值实测（带券计价优先于菜单反查）")
    print("=" * 60)

    # 真实响应：把券自带的 productCode 与 couponId 一起传给 calculate-price
    real_probes = {
        "C_A": {"success": True, "data": {"discount": 1450, "price": 1350}},
        "C_B": {"success": True, "data": {"discount": 660, "price": 1590}},
    }
    probes = parse_coupon_probes(real_probes)
    assert probes["C_A"].value == 14.5 and probes["C_A"].price == 13.5
    assert probes["C_B"].value == 6.6 and probes["C_B"].price == 15.9
    print("   discount 1450 分 → 价值 14.5 元 · price 1350 分 → 用券实付 13.5 元")
    print("   discount  660 分 → 价值  6.6 元 · price 1590 分 → 用券实付 15.9 元")

    # 关键对比：「王牌小食拼」标题里没有数字，菜单反查识别不出券型
    reverse = coupon_real_value("王牌小食拼", REAL_MENU_PRICES)
    print(f"   同一张券：菜单反查 {reverse} 元（识别不出），实测 {probes['C_B'].value} 元")
    assert reverse == 0.0, "反查确实识别不了这张券，这正是要实测的原因"

    one_coupon = {"data": [{"title": "王牌小食拼", "couponId": "C_B"}]}

    # 有实测 → 必须优先用实测值
    via_probe = parse_coupon_stock(
        one_coupon, menu_prices=REAL_MENU_PRICES, probes=probes
    )
    assert via_probe[0].value == 6.6, "有实测值时不该退回反查"
    assert via_probe[0].required_spend == 15.9, "触发成本应取用券实付"

    # 没有实测 → 退回反查；反查也测不出则如实为 0，绝不估算
    via_reverse = parse_coupon_stock(one_coupon, menu_prices=REAL_MENU_PRICES)
    assert via_reverse[0].value == 0.0, "无实测且反查不出时应为 0，不估算"
    print("   [PASS] 实测优先、反查兜底，两条路都不估算")
    print()

    print("=" * 60)
    print("6. 端到端：真实数据生成消费审计报告")
    print("=" * 60)

    report = audit_report(orders, stock, months=1, today=date(2026, 10, 10))
    print(render_audit(report))
    print()

    assert report["profile"]["order_count"] == 2
    assert report["profile"]["total_spent"] == 24.8
    print("[PASS] 消费审计报告基于真实数据生成成功")
    print()
    print("=" * 60)
    print("全部订单/券解析测试通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
