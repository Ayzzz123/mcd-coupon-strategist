"""saver.py 的自测脚本。

用途：在没有真实 MCP Token 的情况下，验证方案枚举、排序与凑单陷阱检测的逻辑正确性。

注意：这里使用的 price_fn 是一个**本地模拟计价器**，仅用于测试算法逻辑，
绝不能用于生产环境 —— 生产环境的计价必须来自 MCP 的 calculate-price。

运行：
    python src/test_saver.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from saver import (  # noqa: E402
    Coupon,
    Item,
    PricedPlan,
    detect_filler_trap,
    recommend,
    render,
)


def mock_price_fn(plan):
    """模拟计价器：仅用于逻辑自测。"""
    goods = plan.goods_amount()
    discount = 0.0
    if plan.coupon and plan.coupon.applicable(goods):
        discount = plan.coupon.discount
    payable = round(max(goods - discount, 0), 2)
    return PricedPlan(plan=plan, payable=payable, discount=discount, source="MOCK")


def main():
    mains = [
        Item(code="M001", name="麦辣鸡腿堡", price=22.0, category="汉堡"),
        Item(code="M002", name="双层吉士汉堡", price=25.0, category="汉堡"),
    ]
    sides = [
        Item(code="S001", name="中薯条", price=12.0, is_side=True),
        Item(code="S002", name="可口可乐(中)", price=11.0, is_side=True),
        Item(code="S003", name="香芋派", price=8.0, is_side=True),
    ]
    combos = [
        [Item(code="M001", name="麦辣鸡腿堡", price=22.0),
         Item(code="S001", name="中薯条", price=12.0),
         Item(code="S002", name="可口可乐(中)", price=11.0)],
    ]
    coupons = [
        Coupon(id="C1", name="满20减3", threshold=20.0, discount=3.0),
        Coupon(id="C2", name="满50减12", threshold=50.0, discount=12.0),
    ]

    result = recommend(
        mains=mains,
        sides=sides,
        combos=combos,
        coupons=coupons,
        price_fn=mock_price_fn,
        budget=45.0,
        top_k=3,
    )

    print("=" * 60)
    print(render(result))
    print("=" * 60)

    # --- 断言：逻辑正确性 ---
    assert result["best"] is not None, "应当至少产出一个推荐方案"

    rec = result["recommended"]
    assert all(p.payable <= 45.0 for p in rec), "预算筛选失效：存在超预算方案"
    assert rec == sorted(rec, key=lambda p: p.payable), "推荐结果未按实付升序排列"

    warnings = result["warnings"]
    assert len(warnings) > 0, "应当检测出凑单陷阱（满50减12）"

    # 陷阱方案的商品数必须多于替代方案，且实付不低于替代方案
    for w in warnings:
        assert w["extra_items"] > 0, "陷阱判定错误：多买的商品数应为正"
        assert w["trap_payable"] >= w["alternative_payable"], "陷阱判定错误：陷阱方案应不更便宜"

    print(f"\n[PASS] 推荐方案 {len(rec)} 个，全部在预算内且已升序排列")
    print(f"[PASS] 检测到 {len(warnings)} 个凑单陷阱")
    print(f"[PASS] 最优方案实付：{result['best'].payable} 元 —— {result['best'].label}")


if __name__ == "__main__":
    main()
