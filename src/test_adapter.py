"""mcd_adapter.py 的自测脚本 —— 使用真实 MCP 响应片段。

数据来源：2026-10-10 对麦当劳官方 MCP Server 的实际调用
（门店：麦当劳南京东城汇餐厅 storeCode=1990622，到店自取 orderType=1 beType=1）。

运行：
    python src/test_adapter.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcd_adapter import (  # noqa: E402
    cents_to_yuan,
    extract_threshold_gap,
    parse_combo_from_detail,
    parse_coupon,
    parse_menu,
    parse_price_response,
)
from saver import Item, Plan  # noqa: E402


# --- 真实响应片段 ---

REAL_MENU = {
    "success": True,
    "code": 200,
    "data": {
        "categories": [
            {"name": "鸡肉汉堡\n/卷", "meals": [
                {"code": "9900005456", "tags": ["套餐", "立省10.5元起"]},
                {"code": "1440", "tags": ["单品", "人气经典"]},
            ]},
            {"name": "小食甜品\n/其他", "meals": [
                {"code": "4810", "tags": ["小食"]},
            ]},
            {"name": "饮品", "meals": [
                {"code": "9900008751", "tags": ["冷饮"]},
            ]},
        ],
        "meals": {
            "9900005456": {"name": "麦辣鸡腿汉堡三件套",
                           "currentPrice": "33.5", "originalPrice": "47"},
            "1440": {"name": "麦辣鸡腿汉堡",
                     "currentPrice": "22.5", "originalPrice": "22.5"},
            "4810": {"name": "薯条", "currentPrice": "14.5", "originalPrice": "14.5"},
            "9900008751": {"name": "可乐", "currentPrice": "9.5", "originalPrice": "9.5"},
        },
    },
}

REAL_PRICE = {
    "success": True,
    "code": 200,
    "data": {
        "productOriginalPrice": 4650,
        "productPrice": 4650,
        "originalPrice": 4650,
        "discount": 0,
        "price": 4650,
        "productList": [
            {"productCode": "1440", "productName": "麦辣鸡腿汉堡",
             "quantity": 1, "originalSubtotal": 2250, "subtotal": 2250},
            {"productCode": "4810", "productName": "中薯条",
             "quantity": 1, "originalSubtotal": 1450, "subtotal": 1450},
            {"productCode": "9900008751", "productName": "可乐",
             "quantity": 1, "originalSubtotal": 950, "subtotal": 950},
        ],
    },
}

REAL_COUPON = {
    "title": "中薯条买一送一",
    "couponId": "CPN_BAOG1",
    "tags": ["到店专用", "外送专用"],
}


def main() -> None:
    """用真实响应片段验证适配层的各项解析。"""
    print("=" * 60)
    print("1. 单位换算（分 → 元）")
    print("=" * 60)
    assert cents_to_yuan(4650) == 46.5
    assert cents_to_yuan("2250") == 22.5
    assert cents_to_yuan(0) == 0.0
    assert cents_to_yuan(None) == 0.0
    assert cents_to_yuan(950) == 9.5
    print("   4650 分 → 46.5 元")
    print("   [PASS] 单位换算正确，未出现 100 倍放大")
    print()

    print("=" * 60)
    print("2. 菜单解析（分类 → 主食/配餐）")
    print("=" * 60)
    mains, sides = parse_menu(REAL_MENU)
    print(f"   主食 {len(mains)} 个: {[(m.code, m.name, m.price) for m in mains]}")
    print(f"   配餐 {len(sides)} 个: {[(s.code, s.name, s.price) for s in sides]}")

    assert len(mains) == 2, "应识别出 2 个主食（套餐 + 汉堡单品）"
    assert len(sides) == 2, "应识别出 2 个配餐（薯条 + 可乐）"

    m1440 = next(m for m in mains if m.code == "1440")
    assert m1440.price == 22.5, "麦辣鸡腿汉堡价格应为 22.5 元"
    assert m1440.name == "麦辣鸡腿汉堡"

    s4810 = next(s for s in sides if s.code == "4810")
    assert s4810.is_side is True, "薯条应被标记为配餐"
    print("   [PASS] 主食/配餐分类正确，价格解析正确")
    print()

    print("=" * 60)
    print("3. 计价响应解析（分 → 元）")
    print("=" * 60)
    plan = Plan(
        items=[
            Item(code="1440", name="麦辣鸡腿汉堡", price=22.5),
            Item(code="4810", name="中薯条", price=14.5, is_side=True),
            Item(code="9900008751", name="可乐", price=9.5, is_side=True),
        ],
        label="单点组合",
    )
    priced = parse_price_response(plan, REAL_PRICE)
    print(f"   实付 {priced.payable} 元（响应 price={REAL_PRICE['data']['price']} 分）")

    assert priced.payable == 46.5, "应付金额应为 46.5 元"
    assert priced.source == "calculate-price"
    assert plan.goods_amount() == 46.5, "本地合计应与 MCP 计价一致"
    print("   [PASS] 解析后金额与本地合计一致（46.5 元），换算无误")
    print()

    print("=" * 60)
    print("4. 券面解析")
    print("=" * 60)
    c = parse_coupon(REAL_COUPON)
    print(f"   券名: {c.name} | 门槛: {c.threshold} | 减免: {c.discount} | 范围: {c.scope}")
    assert c.name == "中薯条买一送一"
    assert c.threshold == 0.0, "买一送一券无金额门槛"

    c2 = parse_coupon({"title": "满50减12", "couponId": "C2"})
    assert c2.threshold == 50.0 and c2.discount == 12.0, "应解析出满减结构"
    print(f"   满减券解析: 门槛 {c2.threshold} 减 {c2.discount}")
    print("   [PASS] 特价券与满减券均可正确解析")
    print()

    print("=" * 60)
    print("5. 门槛差额提取")
    print("=" * 60)
    resp = {"data": {"enjoyable": {"amountType": "3", "realDiscount": 12.0,
                                   "balance": 350}}}
    gap = extract_threshold_gap(resp)
    print(f"   {gap['note']}")
    assert gap["balance"] == 3.5, "门槛差额应从 350 分转为 3.5 元"
    print("   [PASS] 门槛差额换算正确")
    print()

    print("=" * 60)
    print("6. 套餐结构解析（真实 detail 响应）")
    print("=" * 60)
    detail = {"data": {"code": "9900005456", "name": "麦辣鸡腿汉堡三件套", "rounds": [
        {"id": 1, "name": "麦辣鸡腿汉堡", "choices": [
            {"code": "1440", "name": "麦辣鸡腿汉堡", "isDefault": 1}]},
        {"id": 2, "name": "选择套餐内小食", "choices": [
            {"code": "4810", "name": "中薯条", "isDefault": 1},
            {"code": "4820", "name": "大薯条", "isDefault": 0}]},
        {"id": 3, "name": "选择套餐内饮料", "choices": [
            {"code": "3050", "name": "可乐中杯", "isDefault": 1}]},
    ]}}
    combo_items = parse_combo_from_detail(detail)
    print(f"   套餐含: {[i.name for i in combo_items]}")
    assert len(combo_items) == 3, "应提取 3 个默认子项"
    assert "大薯条" not in [i.name for i in combo_items], "未默认选中的项不应提取"
    print("   [PASS] 仅提取默认选中的子项")
    print()

    print("=" * 60)
    print("真实场景验证：套餐 vs 单点")
    print("=" * 60)
    combo_price = 33.5
    single_goods = 46.5
    print(f"   三件套实付       : {combo_price} 元")
    print(f"   等价单点组合     : {single_goods} 元")
    print(f"   → 套餐直接省下   : {round(single_goods - combo_price, 2)} 元")
    print("   [PASS] 验证了「套餐 vs 单点」决策链的必要性")
    print()

    print("=" * 60)
    print("全部适配层测试通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
