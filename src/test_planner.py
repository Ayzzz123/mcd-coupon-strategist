"""planner.py 的自测脚本。

重点验证：四类净价值判定、临界值、券包汇总，以及用券排程的机会约束。

运行：
    python src/test_planner.py
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auditor import CouponStock, OrderRecord  # noqa: E402
from planner import (  # noqa: E402
    CATEGORY_DIRECT,
    CATEGORY_PAPER,
    CATEGORY_STRETCH,
    CATEGORY_UNKNOWN,
    audit_coupon_portfolio,
    classify_coupon,
    order_frequency,
    plan_redemption,
    render_portfolio,
    render_schedule,
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
    print("7. 用券排程：机会不足")
    print("=" * 60)

    # 窗口 20 天 × 频率 0.09 → floor(1.8) = 1 次机会；可用券有 2 张
    pool = [
        coupon("a", "满20减12", 12.0, 20.0, days=20),
        coupon("b", "免费脆薯饼", 5.0, 5.0, days=20),
        coupon("c", "满50减12", 12.0, 50.0, days=20),
    ]
    plan = plan_redemption(pool, natural_spend=13.0, frequency=0.09, today=TODAY)
    print(f"   窗口 {plan.window_days} 天 / 机会 {plan.opportunities} 次")
    print(f"   排上 {len(plan.scheduled)} 张，排不上 {len(plan.missed)} 张")
    print(f"   可拿到 ¥{plan.captured} / 可实现 ¥{plan.realizable_total}")

    assert plan.opportunities == 1, "20 × 0.09 应向下取整为 1"
    assert len(plan.scheduled) == 1 and len(plan.missed) == 1, (
        "3 张券里 1 张纸面富贵不参与排程，剩 2 张可用但只有 1 次机会"
    )
    assert plan.captured == 5.0 and plan.missed_value == 5.0
    assert plan.realizable_total == 10.0
    assert all(v.category != CATEGORY_PAPER for v in plan.missed), (
        "纸面富贵券不该出现在排程里"
    )
    print("   [PASS] 机会不足时正确区分「排得上」与「注定浪费」")
    print()

    print("=" * 60)
    print("8. 排程边界：机会充足 / 零频率 / 全部过期")
    print("=" * 60)

    rich = plan_redemption(pool, 13.0, frequency=1.0, today=TODAY)
    print(f"   频率 1.0 → 机会 {rich.opportunities} 次，排不上 {len(rich.missed)} 张")
    # 窗口 days_left=20 覆盖 today..today+20 共 21 个日历日，频率 1 单/天 → 21 次。
    # （原实现按 20 算，漏了今天那一天 —— 顺手修正。）
    assert rich.opportunities == 21 and not rich.missed
    assert rich.captured == 10.0

    none = plan_redemption(pool, 13.0, frequency=0.0, today=TODAY)
    print(f"   频率 0.0 → 机会 {none.opportunities} 次")
    assert none.opportunities == 0 and not none.scheduled
    assert "无法排程" in render_schedule(none), "无频率时应如实说明，而不是编一个排程"

    gone = plan_redemption(
        [coupon("d", "过期券", 10.0, 10.0, days=-1)], 13.0, frequency=1.0, today=TODAY
    )
    print(f"   全部过期 → 已过期 {len(gone.expired)} 张，可排 {len(gone.scheduled)} 张")
    assert len(gone.expired) == 1 and not gone.scheduled
    print("   [PASS] 三处边界处理正确")
    print()

    print("=" * 60)
    print("9. 点单频率推算")
    print("=" * 60)
    order = OrderRecord(TODAY, 10.0, ["某餐品"], "到店")
    freq = order_frequency([order] * 9, 90)
    print(f"   90 天 9 单 → {freq} 单/天")
    assert freq == 0.1
    assert order_frequency([], 90) == 0.0
    assert order_frequency([order], 0) == 0.0, "观察天数为 0 时不应除零"
    print("   [PASS] 频率推算与除零保护正确")
    print()

    print("=" * 60)
    print("10. 排程完整渲染")
    print("=" * 60)
    schedule_text = render_schedule(plan)
    print(schedule_text)
    print()

    for key in ("用券排程", "窗口", "建议安排", "排不上", "注定随券过期浪费"):
        assert key in schedule_text, f"排程渲染缺少「{key}」"
    print("[PASS] 排程渲染包含全部关键信息")
    print()

    print("=" * 60)
    print("11. 星期限制会削减可用机会")
    print("=" * 60)

    # 周五发一张「只能工作日用」的券，周日到期：窗口 3 个日历日（五、六、日），
    # 但券只有周五能用 → 能用券的天数是 1，不是 3。
    # 按自然日算机会数会高估「你还能用掉几张券」。
    fri = date(2026, 10, 16)
    weekday = plan_redemption(
        [CouponStock("w", "工作日券", 10.0, date(2026, 10, 18),
                     weekdays=frozenset({0, 1, 2, 3, 4}),
                     weekday_text="周一至周五",
                     valid_from=date(2026, 10, 16))],
        natural_spend=0.0, frequency=1.0, today=fri,
    )
    print(f"   工作日券：窗口 {weekday.window_days} 天 · "
          f"能用券 {weekday.usable_days} 天 → 机会 {weekday.opportunities} 次")
    assert weekday.usable_days == 1, \
        f"周五起 3 个日历日里券只有 1 天能用，实际 {weekday.usable_days}"
    assert weekday.opportunities == 1, "机会数应按时段可用的天数算"

    # 同样窗口，但券不限星期 → 3 天都能用
    anytime = plan_redemption(
        [CouponStock("x", "不限星期的券", 10.0, date(2026, 10, 18))],
        natural_spend=0.0, frequency=1.0, today=fri,
    )
    print(f"   不限星期：能用券 {anytime.usable_days} 天 → "
          f"机会 {anytime.opportunities} 次")
    assert anytime.usable_days == 3, "券不受星期限制时，3 个日历日都能用"
    assert anytime.opportunities == 3

    # 窗口里有不可用日时，渲染必须说明，否则用户不知道机会被削减了
    assert "只有 1 天能用券" in render_schedule(weekday), \
        "窗口里有不可用日时，渲染必须说明"
    print("   [PASS] 星期限制削减可用机会，并在渲染里如实说明")
    print()

    print("=" * 60)
    print("全部券包体检与用券排程测试通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
