"""planner.py 的自测脚本。

重点验证：四类净价值判定、临界值、以及券包汇总数字。

运行：
    python src/test_planner.py
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auditor import CouponStock  # noqa: E402
from planner import (  # noqa: E402
    CATEGORY_DIRECT,
    CATEGORY_PAPER,
    CATEGORY_STRETCH,
    CATEGORY_UNKNOWN,
    audit_coupon_portfolio,
    classify_coupon,
    render_portfolio,
)

TODAY = date(2026, 10, 13)


def coupon(
    cid: str,
    name: str,
    value: float,
    required: float,
    days: int = 7,
    threshold: float = 0.0,
) -> CouponStock:
    """构造一张券，有效期按 `days` 天后计算。"""
    return CouponStock(cid, name, value, TODAY + timedelta(days=days),
                       threshold, required)


def main() -> None:
    """跑一遍券包体检的全部用例。"""
    print("=" * 60)
    print("1. 四类净价值判定（自然消费基准 13 元）")
    print("=" * 60)

    cases = [
        ("免费脆薯饼", 5.0, 5.0, CATEGORY_DIRECT, 5.0),
        ("满20减12", 12.0, 20.0, CATEGORY_STRETCH, 5.0),
        ("满50减12", 12.0, 50.0, CATEGORY_PAPER, -25.0),
        ("神秘券", 0.0, 0.0, CATEGORY_UNKNOWN, 0.0),
    ]

    for name, value, required, expect_category, expect_net in cases:
        verdict = classify_coupon(
            coupon("x", name, value, required), natural_spend=13.0, today=TODAY
        )
        print(
            f"   {name:<12} 面值 {verdict.face_value:>5}"
            f"  多花 {verdict.extra_spend:>5}"
            f"  净价值 {verdict.net_value:>6}  → {verdict.label}"
        )
        assert verdict.category == expect_category, (
            f"{name} 应判为 {expect_category}，实际 {verdict.category}"
        )
        assert verdict.net_value == expect_net, (
            f"{name} 净价值应为 {expect_net}，实际 {verdict.net_value}"
        )

    print("   [PASS] 四类判定与净价值计算均正确")
    print()

    print("=" * 60)
    print("2. 临界值：净价值恰好为 0")
    print("=" * 60)

    # 面值 10，门槛 23，基准 13 → 多花 10 → 净价值 0
    zero = classify_coupon(
        coupon("z", "满23减10", 10.0, 23.0), natural_spend=13.0, today=TODAY
    )
    print(f"   面值 10 / 多花 10 / 净价值 {zero.net_value} → {zero.label}")
    assert zero.category == CATEGORY_PAPER, (
        "净价值为 0 应归入纸面富贵（不赚就是劝退），而不是努一把"
    )
    print("   [PASS] 净价值为 0 时归入「纸面富贵」，不误判为可用")
    print()

    print("=" * 60)
    print("3. 边界：基准高于门槛 / 基准为 0 / 空券包")
    print("=" * 60)

    # 客单价 25 已超过门槛 20 → 无需为券多花一分钱
    rich = classify_coupon(
        coupon("r", "满20减12", 12.0, 20.0), natural_spend=25.0, today=TODAY
    )
    print(f"   基准 25 ≥ 门槛 20  → 多花 {rich.extra_spend}，{rich.label}")
    assert rich.category == CATEGORY_DIRECT
    assert rich.net_value == 12.0, "顺手可用时净价值应等于券面价值"

    # 基准为 0：整单都要为券而消费
    poor = classify_coupon(
        coupon("p", "满20减12", 12.0, 20.0), natural_spend=0.0, today=TODAY
    )
    print(f"   基准  0 < 门槛 20  → 多花 {poor.extra_spend}，净价值 {poor.net_value}")
    assert poor.category == CATEGORY_PAPER and poor.net_value == -8.0

    empty = audit_coupon_portfolio([], natural_spend=13.0, today=TODAY)
    print(f"   空券包 → {empty['narrative']}")
    assert empty["face_total"] == 0.0 and empty["realizable"] == 0.0
    assert len(empty["verdicts"]) == 0
    print("   [PASS] 三处边界处理正确，无除零/空指针")
    print()

    print("=" * 60)
    print("4. 有效期文案")
    print("=" * 60)

    for days, expect in ((-1, "已过期"), (0, "今天到期"), (3, "还剩 3 天")):
        verdict = classify_coupon(
            coupon("d", "某券", 5.0, 5.0, days=days),
            natural_spend=13.0,
            today=TODAY,
        )
        print(f"   days_left={days:>2} → {verdict.expiry_text()}")
        assert verdict.expiry_text() == expect

    forever = CouponStock("f", "无期限券", 5.0, None, 0.0, 5.0)
    assert classify_coupon(forever, 13.0, TODAY).expiry_text() == "无期限"
    print("   [PASS] 已过期 / 今天到期 / 剩余天数 / 无期限 均正确")
    print()

    print("=" * 60)
    print("5. 券包汇总与排序")
    print("=" * 60)

    portfolio = [
        coupon("a", "免费脆薯饼", 5.0, 5.0),
        coupon("b", "满20减12", 12.0, 20.0),
        coupon("c", "满50减12", 12.0, 50.0),
        coupon("d", "神秘券", 0.0, 0.0),
    ]
    report = audit_coupon_portfolio(portfolio, natural_spend=13.0, today=TODAY)

    print(f"   名义总额 {report['face_total']} / 可实现 {report['realizable']}"
          f" / 差额 {report['gap']}")
    assert report["face_total"] == 29.0, "5+12+12+0 = 29"
    assert report["realizable"] == 10.0, "顺手 5 + 努一把 5 = 10"
    assert report["gap"] == 19.0, "29 − 10 = 19"
    assert report["stretch_cost"] == 7.0, "「满20减12」需额外消费 7"
    assert report["paper_face"] == 12.0

    order = [v.category for v in report["verdicts"]]
    print(f"   排序：{order}")
    assert order == [
        CATEGORY_DIRECT,
        CATEGORY_STRETCH,
        CATEGORY_PAPER,
        CATEGORY_UNKNOWN,
    ], "应按「该用→能努→劝退→无法判断」排序"
    print("   [PASS] 汇总数字与展示顺序均正确")
    print()

    print("=" * 60)
    print("6. 完整报告渲染")
    print("=" * 60)
    text = render_portfolio(report)
    print(text)
    print()

    for key in ("券包体检", "名义总额", "纸面富贵", "净亏", "建议放弃", "未估算"):
        assert key in text, f"渲染结果缺少「{key}」"
    print("[PASS] 报告渲染包含全部关键信息")
    print()
    print("=" * 60)
    print("全部券包体检测试通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
