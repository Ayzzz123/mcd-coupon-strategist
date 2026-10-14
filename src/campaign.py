"""营销活动日历 —— 回答「等活动再买」到底等不等得起。

`campaign-calendar` 返回的是**当月哪些日子有活动**，以及每个活动的标题。
本模块只用这两样（日期 + 标题），做一件能证明的事：

    把你的券的有效期，和活动的时间线对起来 —— 券撑不撑得到下一次活动。

刻意不做的事（重要）
--------------------

活动内容介绍里躺着一堆价格：「9.9元早餐两件套」「26.9 元起」「13.9r」……
这些是**营销文案**，既无法与门店菜单核实，也不是结构化字段。
本项目一律不拿它们做计算或比较 —— 与 `mall-product-detail` 的 `note`
是同一类东西（接口自己也明确要求「严禁从 note 提取数值用于价格计算」）。

因此本模块**不会**说「等那天更便宜」。「有活动」和「更便宜」是两回事，
把它们划等号是骗人的。这里只给时间线，以及一个纯日期的事实：
你的券能不能撑到那天。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from auditor import CouponStock


@dataclass
class CampaignDay:
    """日历里的一天 —— 当天有 N 个活动在推。"""

    day: date
    label: str = ""  # 原文标签（往期回顾 / 今日 / 空）。**仅作记录，不用它判断**
    titles: list[str] = field(default_factory=list)

    def status(self, today: date) -> str:
        """相对 `today` 的位置：past / today / upcoming。

        **不用原文标签判断** —— 「今日」是采集那一刻的相对标签，
        录下来之后就不再成立。相对时间必须拿真实日期去比。
        """
        if self.day < today:
            return "past"
        if self.day == today:
            return "today"
        return "upcoming"


def audit_campaigns(
    days: list[CampaignDay],
    coupons: list[CouponStock],
    today: date,
) -> dict:
    """把活动时间线与券的有效期对起来（**纯日期比较，不涉及金额**）。

    返回：
    {
      "past":      [CampaignDay, ...],
      "today":     [CampaignDay, ...],
      "upcoming":  [CampaignDay, ...],
      "next_upcoming": CampaignDay | None,   # 今天之后最近的一次
      "coupon_checks": [
          {"name", "expire_date", "days_left", "reaches_next"}
      ],
      "reaches_count": int,   # 有几张券撑得到最近的活动日
    }
    """

    past = [d for d in days if d.status(today) == "past"]
    today_days = [d for d in days if d.status(today) == "today"]
    upcoming = sorted(
        (d for d in days if d.status(today) == "upcoming"),
        key=lambda d: d.day,
    )
    next_upcoming = upcoming[0] if upcoming else None

    checks: list[dict] = []
    for c in coupons:
        days_left = c.days_left(today)
        if next_upcoming is None:
            reaches: Optional[bool] = None
        elif c.expire_date is None:
            reaches = True  # 无有效期的券不受时间限制
        else:
            # 到期当天仍可用，所以「撑得到」是 >= 而不是 >
            reaches = c.expire_date >= next_upcoming.day
        checks.append({
            "name": c.name,
            "expire_date": c.expire_date,
            "days_left": days_left,
            "reaches_next": reaches,
        })

    # 已过期的券不参与「等不等得起」的统计 —— 它已经没得等了
    live = [c for c in checks if c["days_left"] is None or c["days_left"] >= 0]

    return {
        "past": past,
        "today": today_days,
        "upcoming": upcoming,
        "next_upcoming": next_upcoming,
        "coupon_checks": checks,
        "reaches_count": sum(1 for c in live if c["reaches_next"]),
    }


def render_campaigns(report: dict, today: date) -> str:
    """把活动节奏渲染成面向用户的文本。"""

    lines: list[str] = []
    past = report.get("past") or []
    today_days = report.get("today") or []
    upcoming = report.get("upcoming") or []
    next_day = report.get("next_upcoming")

    total = len(past) + len(today_days) + len(upcoming)
    lines.append("活动节奏")
    if not total:
        lines.append("  日历里没有查到活动。")
        return "\n".join(lines).rstrip()

    lines.append(
        f"  {today.month} 月日历里有 {total} 天有活动："
        f"已过 {len(past)} 天 · 今天 {len(today_days)} 天 · 之后 {len(upcoming)} 天"
    )

    if next_day is not None:
        gap = (next_day.day - today).days
        when = "明天" if gap == 1 else f"{gap} 天后"
        lines.append(f"  最近一次：{_fmt_date(next_day.day)}（{when}）")
        for title in next_day.titles[:3]:
            lines.append(f"    · {title}")
    elif today_days:
        lines.append("  之后没有更多活动了，今天就是最后一天。")
    lines.append("")

    # 必须声明的边界 —— 否则用户会把「有活动」读成「更便宜」
    lines.append("  说明：「有活动」不等于「更便宜」。")
    lines.append("       活动内容里的价格是营销文案，无法核实，本工具不拿它做计算。")
    lines.append("       所以这里只给时间线，值不值得等要你结合活动内容自己判断。")
    lines.append("")

    checks = report.get("coupon_checks") or []
    if checks and next_day is not None:
        lines.append(f"  你手里的券撑得到 {_fmt_date(next_day.day)} 吗（纯日期比较）：")
        for c in checks:
            if c["days_left"] is None:
                expiry = "无有效期"
            elif c["days_left"] < 0:
                expiry = f"{_fmt_date(c['expire_date'])}已过期"
            else:
                expiry = f"{_fmt_date(c['expire_date'])}到期（还剩 {c['days_left']} 天）"
            mark = "等得到" if c["reaches_next"] else "等不到 —— 它会先过期"
            lines.append(f"    · {c['name']}    {expiry}    → {mark}")
        lines.append("")

    return "\n".join(lines).rstrip()


def _fmt_date(d: Optional[date]) -> str:
    """把日期格式化成「10月15日」。日期缺失时返回「?」。"""
    if d is None:
        return "?"
    return f"{d.month}月{d.day}日"


__all__ = [
    "CampaignDay",
    "audit_campaigns",
    "render_campaigns",
]
