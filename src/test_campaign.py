"""campaign.py 与活动日历解析的自测。

重点验证三件事：

1. **只取结构化字段**（日期 + 标题）—— 活动内容介绍是营销文案，
   里面那些价格（「26.9 元起」「13.9r」）不可核实，必须被丢弃。
2. **「今日 / 往期回顾」标签不参与判断** —— 那是采集那一刻的相对标签，
   录下来之后就不再成立，相对时间一律按真实日期算。
3. **券与活动日的交叉是纯日期比较**，且券在活动当天到期仍算「撑得到」。

运行：
    python src/test_campaign.py
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auditor import CouponStock  # noqa: E402
from campaign import CampaignDay, audit_campaigns, render_campaigns  # noqa: E402
from mcd_adapter import parse_campaign_calendar  # noqa: E402


HERE = os.path.dirname(os.path.abspath(__file__))
CALENDAR_FILE = os.path.join(HERE, "..", "fixtures", "campaign_calendar.md")


def main() -> None:
    """跑一遍活动日历的全部用例并打印结论。"""
    with open(CALENDAR_FILE, encoding="utf-8") as f:
        raw = f.read()
    days = parse_campaign_calendar(raw)

    print("=" * 60)
    print("1. 解析：只取日期与标题，营销文案一律丢弃")
    print("=" * 60)

    assert len(days) == 14, f"应解析出 14 个日期分节，实际 {len(days)}"
    total_titles = sum(len(d.titles) for d in days)
    # 构成：10-12 两条 + 10-13 一条 + 10-14 十四条（今日活动最密）
    #       + 之后 11 天各一条 = 28
    assert total_titles == 28, f"应解析出 28 条活动标题，实际 {total_titles}"
    print(f"   分节 {len(days)} 个 · 活动 {total_titles} 条")

    first = days[0]
    assert first.day == date(2026, 10, 12), "首个分节日期应为 2026-10-12"
    assert first.label == "往期回顾"
    assert "超值" in first.titles[0], "活动标题应被完整取出"

    # 核心契约：活动内容介绍是营销文案，必须被丢掉。
    # 这一条保证「绝不从文案里抠数字」不是一句口号。
    joined = " ".join(t for d in days for t in d.titles)
    for leaked in ("速戳", "今天这堡", "两件套只要9.9元", "cms-cdn.mcd.cn"):
        assert leaked not in joined, \
            f"营销文案「{leaked}」不应进入解析结果（那是不可核实的文案）"
    print("   [PASS] 营销文案（含价格与图片）全部被丢弃，只留日期与标题")
    print()

    print("=" * 60)
    print("2. 「今日」标签是采集时的相对标签，不参与判断")
    print("=" * 60)

    d14 = next(d for d in days if d.day == date(2026, 10, 14))
    assert d14.label == "今日", "原文标签应原样记下"
    assert d14.status(date(2026, 10, 14)) == "today"
    assert d14.status(date(2026, 10, 20)) == "past", \
        "换个日期，「今日」就该是过去 —— 标签不是永久事实"
    assert d14.status(date(2026, 10, 1)) == "upcoming"
    print("   [PASS] label 只作记录；相对状态一律按真实日期计算")
    print()

    print("=" * 60)
    print("3. 券的有效期 × 活动日（纯日期比较）")
    print("=" * 60)

    today = date(2026, 10, 14)
    coupons = [
        # 10-16 到期，而下一个活动日是 10-15 → 撑得到
        CouponStock("A", "三天后到期的券", 10.0, date(2026, 10, 16)),
        # 今天到期 → 等不到明天的活动
        CouponStock("B", "今天到期的券", 10.0, date(2026, 10, 14)),
        # 无有效期 → 不受时间限制
        CouponStock("C", "无有效期的券", 10.0),
    ]
    report = audit_campaigns(days, coupons, today)

    assert report["next_upcoming"].day == date(2026, 10, 15), \
        "下一个活动日应为 10-15"
    checks = {c["name"]: c for c in report["coupon_checks"]}
    assert checks["三天后到期的券"]["reaches_next"] is True
    assert checks["今天到期的券"]["reaches_next"] is False, \
        "今天到期的券撑不到明天的活动"
    assert checks["无有效期的券"]["reaches_next"] is True, \
        "无有效期的券不受时间限制"
    print("   [PASS] 撑得到 / 撑不到 / 无有效期 三种情况判断正确")

    # 边界：券在活动当天到期，当天仍可用
    same_day = audit_campaigns(
        [CampaignDay(date(2026, 10, 16))],
        [CouponStock("D", "活动当天到期的券", 5.0, date(2026, 10, 16))],
        today,
    )
    assert same_day["coupon_checks"][0]["reaches_next"] is True, \
        "券在活动当天到期，当天仍可用 —— 边界应取 >="
    print("   [PASS] 券到期日与活动日同为一天时算「撑得到」（>= 而非 >）")
    print()

    print("=" * 60)
    print("4. 边界：空日历 / 无券 / 已过期的券")
    print("=" * 60)

    empty = audit_campaigns([], coupons, today)
    assert empty["next_upcoming"] is None
    assert empty["reaches_count"] == 0
    assert "没有查到活动" in render_campaigns(empty, today)
    print("   [PASS] 空日历不崩，如实说「没有查到活动」")

    no_coupon = render_campaigns(audit_campaigns(days, [], today), today)
    assert "活动节奏" in no_coupon, "没券时仍应输出活动时间线"
    assert "撑得到" not in no_coupon, "没券时不该输出「撑得到吗」那一段"
    print("   [PASS] 没券时只给时间线，不输出券的对照段")

    expired = audit_campaigns(
        days,
        [CouponStock("E", "已过期的券", 10.0, date(2026, 10, 10))],
        today,
    )
    assert expired["reaches_count"] == 0, "已过期的券不该被算作「等得到」"
    print("   [PASS] 已过期的券不参与统计（它已经没得等了）")
    print()

    print("=" * 60)
    print("5. 容错：非法日期整节跳过；渲染不含文案价格")
    print("=" * 60)

    broken = (
        "#### 2026年13月40日\n\n"
        "-   **活动标题**：不该被采纳\n\n"
        "#### 2026年10月20日\n\n"
        "-   **活动标题**：这条应该被采纳\n"
    )
    got = parse_campaign_calendar(broken)
    assert len(got) == 1, f"非法日期应整节跳过，实际得到 {len(got)} 个分节"
    assert got[0].day == date(2026, 10, 20)
    assert got[0].titles == ["这条应该被采纳"], "非法分节不应污染后续分节"
    print("   [PASS] 非法日期整节跳过，未污染后续分节")

    rendered = render_campaigns(report, today)
    for leaked in ("9.9元", "26.9", "13.9r", "39.9"):
        assert leaked not in rendered, f"渲染结果不应出现文案价格「{leaked}」"
    print("   [PASS] 渲染结果不含任何营销文案里的价格")
    print()

    print("=" * 60)
    print("全部活动日历测试通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
