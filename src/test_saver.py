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
    Plan,
    PricedPlan,
    build_base_plans,
    recommend,
    render,
    within_budget,
)


def mock_price_fn(plan: Plan) -> PricedPlan:
    """模拟计价器：仅用于逻辑自测。"""
    goods = plan.goods_amount()
    discount = 0.0
    if plan.coupon and plan.coupon.applicable(goods):
        discount = plan.coupon.discount
    payable = round(max(goods - discount, 0), 2)
    return PricedPlan(plan=plan, payable=payable, discount=discount, source="MOCK")


def check_demand(mains, sides, combos, coupons) -> None:
    """校验需求约束在排序之前生效。"""
    print("=" * 60)
    print("2. 需求约束（demand）—— 必须在排序之前生效")
    print("=" * 60)

    def wants_burger_and_fries(plan: Plan) -> bool:
        """需求：麦辣鸡腿堡 + 中薯条，两样齐全。"""
        codes = plan.covered_codes()
        return "M001" in codes and "S001" in codes

    result = recommend(
        mains=mains, sides=sides, combos=combos, coupons=coupons,
        price_fn=mock_price_fn, demand=wants_burger_and_fries, top_k=5,
    )
    rec = result["recommended"]

    # 最便宜的候选是「香芋派 8 元」——不含主食。若不先按需求筛掉，
    # 它会挤进推荐，用户就会得到一个「便宜但吃不饱」的答案。
    assert rec, "应至少有一个满足需求的方案"
    assert all(wants_burger_and_fries(p.plan) for p in rec), \
        "推荐里出现了不满足需求的方案 —— demand 没有在排序前生效"
    assert rec[0].payable > 8.0, "不满足需求的便宜方案不应胜出"
    print(f"   {len(rec)} 个方案全部满足需求；最便宜 {rec[0].payable} 元（{rec[0].label}）")
    print("   [PASS] 更便宜但吃不饱的方案没有混进推荐")
    print()


def check_combo_parts() -> None:
    """校验套餐构成与枚举深度。"""
    print("=" * 60)
    print("3. 套餐构成（covered_codes）与枚举深度（max_sides）")
    print("=" * 60)

    combo = Item(
        code="COMBO1", name="某三件套", price=39.0,
        parts=[
            Item(code="M001", name="麦辣鸡腿堡", price=0.0),
            Item(code="S001", name="中薯条", price=0.0),
            Item(code="S002", name="可口可乐(中)", price=0.0),
        ],
    )
    assert combo.covered_codes() == {"COMBO1", "M001", "S001", "S002"}, \
        "套餐应覆盖「自身编码 + 内含单品编码」"
    plan = Plan(items=[combo])
    assert {"M001", "S001", "S002"} <= plan.covered_codes(), \
        "Plan.covered_codes() 应把套餐内含单品算进来"
    # 内含单品只是元数据，不参与计价
    assert plan.goods_amount() == 39.0, "套餐内含单品不应影响套餐计价"
    print(f"   套餐覆盖编码：{sorted(combo.covered_codes())}")
    print("   [PASS] 套餐知道自己在装什么，且不影响计价")

    one_main = [Item(code="M1", name="主食", price=10.0)]
    two_sides = [
        Item(code="S1", name="配餐A", price=3.0, is_side=True),
        Item(code="S2", name="配餐B", price=4.0, is_side=True),
    ]
    shallow = build_base_plans(one_main, two_sides, [], max_sides=1)
    deep = build_base_plans(one_main, two_sides, [], max_sides=2)
    assert len(shallow) == 3, f"max_sides=1 应产出「主食」+2 个两件套 = 3，实际 {len(shallow)}"
    assert len(deep) == 4, f"max_sides=2 应多出「主食+两个配餐」，实际 {len(deep)}"
    print(f"   max_sides=1 → {len(shallow)} 个方案；max_sides=2 → {len(deep)} 个")
    print("   [PASS] 需求要三样齐全时，必须 max_sides≥2 才枚举得出对照组合")
    print()


def check_budget(mains, sides) -> None:
    """校验预算语义：区分「预算内没有」与「有但更贵」。"""
    print("=" * 60)
    print("4. 预算：区分「预算内没有」与「有方案但更贵」")
    print("=" * 60)

    def wants_burger_and_fries(plan: Plan) -> bool:
        """需求：麦辣鸡腿堡 + 中薯条，两样齐全。"""
        codes = plan.covered_codes()
        return "M001" in codes and "S001" in codes

    # 满足需求的最便宜组合是 22 + 12 = 34 元，预算给 30 元
    tight = recommend(
        mains=mains, sides=sides, combos=[], coupons=[],
        price_fn=mock_price_fn, budget=30.0, demand=wants_burger_and_fries,
    )
    assert tight["recommended"] == [], "预算内没有方案时 recommended 应为空"
    assert tight["over_budget"], "超预算的对照方案应单独返回"
    assert tight["best"] is None, "预算内没有方案时不应给出 best"
    print(f"   预算 30：预算内 0 个；超预算对照 {len(tight['over_budget'])} 个"
          f"（最便宜 {tight['over_budget'][0].payable} 元）")

    render_txt = render(tight)
    assert "预算内没有可用方案" in render_txt, "应如实说明预算内没有方案"
    print("   [PASS] 渲染如实说明「预算内没有」，而不是拿超预算方案冒充推荐")

    # within_budget 不再兜底把超预算方案当成推荐
    single = [mock_price_fn(Plan(items=[mains[0]]))]
    assert within_budget(single, 1.0) == [], \
        "全部超预算时应返回空列表，而不是兜底返回最便宜的"
    print("   [PASS] within_budget 不再兜底（超预算 ≠ 推荐）")
    print()


def main() -> None:
    """跑一遍省钱枚举的完整场景并断言结果。"""
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

    print("=" * 60)
    print("1. 基础推荐（含预算筛选与凑单陷阱）")
    print("=" * 60)

    result = recommend(
        mains=mains,
        sides=sides,
        combos=combos,
        coupons=coupons,
        price_fn=mock_price_fn,
        budget=45.0,
        top_k=3,
    )

    print(render(result))

    # --- 断言：逻辑正确性 ---
    assert result["best"] is not None, "应当至少产出一个推荐方案"

    rec = result["recommended"]
    assert all(p.payable <= 45.0 for p in rec), "预算筛选失效：存在超预算方案"
    assert rec == sorted(rec, key=lambda p: p.payable), "推荐结果未按实付升序排列"
    assert all(p.payable > 45.0 for p in result["over_budget"]), \
        "over_budget 里混进了预算内的方案"

    warnings = result["warnings"]
    assert len(warnings) > 0, "应当检测出凑单陷阱（满50减12）"

    # 陷阱方案的商品数必须多于替代方案，且实付不低于替代方案
    for w in warnings:
        assert w["extra_items"] > 0, "陷阱判定错误：多买的商品数应为正"
        assert w["trap_payable"] >= w["alternative_payable"], "陷阱判定错误：陷阱方案应不更便宜"

    print(f"\n[PASS] 推荐方案 {len(rec)} 个，全部在预算内且已升序排列")
    print(f"[PASS] 超预算对照 {len(result['over_budget'])} 个，已与推荐分开")
    print(f"[PASS] 检测到 {len(warnings)} 个凑单陷阱")
    print(f"[PASS] 最优方案实付：{result['best'].payable} 元 —— {result['best'].label}")
    print()

    check_demand(mains, sides, combos, coupons)
    check_combo_parts()
    check_budget(mains, sides)

    print("=" * 60)
    print("全部省钱逻辑测试通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
