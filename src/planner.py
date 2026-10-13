"""券的净价值审计 —— 回答「这些券该怎么用、哪些别用」。

链路 B 的 `auditor.py` 只**量化**损失（「你有 ¥33 要过期」）；本模块往前走一步：
给每张券算出**净价值**，据此产出可执行的用券建议。

核心公式（对所有券型统一成立）：

    净价值 = 券面价值 − 为用这张券而额外多花的钱

其中「额外多花的钱」= ``max(0, 触发这张券所需消费 − 你的自然消费基准)``。

举例：一张「满50减12」，你的客单价 13 元 ——

    券面价值 12 − 为达门槛多花的 37 = 净价值 −25

名义上省 12，实际倒贴 25。**这张券不该用。** 换句话说：
券的名义面值从来不是它对你的真实价值。

据此，整个券包会被切成四类：

========== ==========================================================
类别        含义
========== ==========================================================
direct     顺手可用 —— 你本来就会花到门槛，净价值等于券面价值
stretch    努一把 —— 差一点到门槛，凑单后净价值仍为正
paper      纸面富贵 —— 为用它要多花的钱 ≥ 券面价值，**建议放弃**
unknown    无法判断 —— 券面无法反查到原价，按铁律不估算
========== ==========================================================

汇总起来就是本模块最有价值的输出：

    券包名义 ¥150 → 按你的消费习惯只拿得到 ¥60

本模块**不估算任何金额**：券面价值与触发成本都来自菜单反查
（`mcd_adapter.coupon_value_and_cost`），消费基准来自真实历史订单
（`auditor.consumption_profile`）。

## 第二层：用券排程

上面算出的「可实现 ¥60」有个隐含前提 —— **这些券你都有机会用掉**。
但真实情况是：券会过期，而你点单的次数有限。

于是有了第二层：``plan_redemption()``。它先推算「券全部过期前你还会点几单」，
再在这个约束下分配券。结论往往更扎心：

    可实现的 ¥60，你其实只拿得到 ¥25 —— 剩下 ¥35 随券一起过期

**可实现的券，未必用得上。** 这是本模块区别于「券面价值加总」的关键一步。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional, Sequence

from auditor import CouponStock, OrderRecord

CATEGORY_DIRECT = "direct"
CATEGORY_STRETCH = "stretch"
CATEGORY_PAPER = "paper"
CATEGORY_UNKNOWN = "unknown"

CATEGORY_LABELS = {
    CATEGORY_DIRECT: "顺手可用",
    CATEGORY_STRETCH: "努一把",
    CATEGORY_PAPER: "纸面富贵",
    CATEGORY_UNKNOWN: "无法判断",
}

# 展示顺序：先给该用的，再给能努一把的，最后是劝退与无法判断的
_CATEGORY_ORDER = {
    CATEGORY_DIRECT: 0,
    CATEGORY_STRETCH: 1,
    CATEGORY_PAPER: 2,
    CATEGORY_UNKNOWN: 3,
}

_RULE = "=" * 46


@dataclass
class CouponVerdict:
    """单张券的净价值判定结果。"""

    coupon_id: str
    name: str
    face_value: float  # 券面价值（元）
    required_spend: float  # 触发这张券至少需要消费多少（元）
    extra_spend: float  # 超出自然消费基准的部分（元）
    net_value: float  # 净价值（元）
    category: str
    days_left: Optional[int] = None  # 距过期天数；None 表示无期限

    @property
    def label(self) -> str:
        """类别中文名。"""
        return CATEGORY_LABELS.get(self.category, self.category)

    @property
    def is_realizable(self) -> bool:
        """是否属于「真正拿得到」的券（顺手可用或努一把）。"""
        return self.category in (CATEGORY_DIRECT, CATEGORY_STRETCH)

    def expiry_text(self) -> str:
        """剩余有效期的可读描述。"""
        if self.days_left is None:
            return "无期限"
        if self.days_left < 0:
            return "已过期"
        if self.days_left == 0:
            return "今天到期"
        return f"还剩 {self.days_left} 天"


def classify_coupon(
    coupon: CouponStock,
    natural_spend: float,
    today: Optional[date] = None,
) -> CouponVerdict:
    """判定单张券的净价值与类别。

    参数
    ----
    coupon        : 券的库存状态，需含 `value` 与 `required_spend`
    natural_spend : **自然消费基准** —— 不看券的情况下，你本来就会花的钱。
                    调用方通常传历史订单的客单价（`profile["avg_per_order"]`）。
    today         : 用于计算剩余天数，默认取当天。

    判定规则
    --------
    1. 券面价值 ≤ 0（无法反查原价）→ ``unknown``，不估算
    2. 额外支出 = ``max(0, required_spend − natural_spend)``
       - 额外支出 = 0 → ``direct``
       - 净价值 > 0   → ``stretch``
       - 净价值 ≤ 0   → ``paper``
    """
    days_left = coupon.days_left(today)

    if coupon.value <= 0:
        return CouponVerdict(
            coupon_id=coupon.coupon_id,
            name=coupon.name,
            face_value=0.0,
            required_spend=round(coupon.required_spend, 2),
            extra_spend=0.0,
            net_value=0.0,
            category=CATEGORY_UNKNOWN,
            days_left=days_left,
        )

    baseline = max(natural_spend, 0.0)
    extra = round(max(0.0, coupon.required_spend - baseline), 2)
    net = round(coupon.value - extra, 2)

    if extra <= 0:
        category = CATEGORY_DIRECT
    elif net > 0:
        category = CATEGORY_STRETCH
    else:
        category = CATEGORY_PAPER

    return CouponVerdict(
        coupon_id=coupon.coupon_id,
        name=coupon.name,
        face_value=round(coupon.value, 2),
        required_spend=round(coupon.required_spend, 2),
        extra_spend=extra,
        net_value=net,
        category=category,
        days_left=days_left,
    )


def audit_coupon_portfolio(
    coupons: list[CouponStock],
    natural_spend: float,
    today: Optional[date] = None,
) -> dict:
    """对整本券包做净价值审计。

    返回：
    {
      "verdicts":      [CouponVerdict, ...],  # 按「该不该用」排序
      "by_category":   {category: [CouponVerdict, ...]},
      "counts":        {category: int},
      "face_total":    float,  # 券包名义总额
      "realizable":    float,  # 可实现净价值（direct 与 stretch 的净价值之和）
      "gap":           float,  # 名义总额 − 可实现，也含「兑现努一把需额外消费」的部分
      "stretch_cost":  float,  # 「努一把」需要额外消费的合计
      "paper_face":    float,  # 「纸面富贵」那一类的名义金额（才是真正的纸面富贵）
      "narrative":     str,    # 一句话结论
    }
    """
    verdicts = [classify_coupon(c, natural_spend, today) for c in coupons]
    verdicts.sort(
        key=lambda v: (
            _CATEGORY_ORDER.get(v.category, 9),
            v.days_left if v.days_left is not None else 9999,
            -v.net_value,
        )
    )

    by_category: dict[str, list[CouponVerdict]] = {k: [] for k in CATEGORY_LABELS}
    for verdict in verdicts:
        by_category[verdict.category].append(verdict)

    face_total = round(sum(v.face_value for v in verdicts), 2)
    realizable = round(sum(v.net_value for v in verdicts if v.is_realizable), 2)
    gap = round(face_total - realizable, 2)
    stretch_cost = round(
        sum(v.extra_spend for v in by_category[CATEGORY_STRETCH]), 2
    )
    paper_face = round(sum(v.face_value for v in by_category[CATEGORY_PAPER]), 2)

    return {
        "verdicts": verdicts,
        "by_category": by_category,
        "counts": {k: len(v) for k, v in by_category.items()},
        "face_total": face_total,
        "realizable": realizable,
        "gap": gap,
        "stretch_cost": stretch_cost,
        "paper_face": paper_face,
        "narrative": _narrative(len(coupons), face_total, realizable, gap),
    }


def _narrative(count: int, face_total: float, realizable: float, gap: float) -> str:
    """生成一句话结论。"""
    if count == 0:
        return "你目前手里没有可用的券。"
    if face_total <= 0:
        return f"你有 {count} 张券，但券面价值都无从反查，无法判断（不估算）。"
    if realizable <= 0:
        return (
            f"券包名义 ¥{face_total}，但按你的消费习惯一张都拿不到 —— "
            f"全部是纸面富贵，不建议为它们改变消费习惯。"
        )
    return (
        f"券包名义 ¥{face_total}，按你的消费习惯可拿到 ¥{realizable}"
        f"（差额 ¥{gap} 是纸面富贵）。"
    )


def _format_direct(v: CouponVerdict) -> str:
    """渲染「顺手可用」的一条。"""
    return f"  · {v.name}    净省 {v.net_value} 元    {v.expiry_text()}"


def _format_stretch(v: CouponVerdict) -> str:
    """渲染「努一把」的一条。

    措辞保持券型无关 —— 满减券看门槛，买一送一/固定价券看商品价，
    对用户而言都是「比平时的消费多掏多少」。
    """
    return (
        f"  · {v.name}    需额外消费 {v.extra_spend} 元，"
        f"净赚 {v.net_value} 元    {v.expiry_text()}"
    )


def _format_paper(v: CouponVerdict) -> str:
    """渲染「纸面富贵」的一条 —— 说清为什么劝退。"""
    return (
        f"  · {v.name}    名义省 {v.face_value} 元，"
        f"但需额外消费 {v.extra_spend} 元 → 净亏 {abs(v.net_value)} 元"
        f"    {v.expiry_text()}"
    )


def _format_unknown(v: CouponVerdict) -> str:
    """渲染「无法判断」的一条。"""
    return f"  · {v.name}    菜单中无对应商品，未估算    {v.expiry_text()}"


_FORMATTERS = {
    CATEGORY_DIRECT: _format_direct,
    CATEGORY_STRETCH: _format_stretch,
    CATEGORY_PAPER: _format_paper,
    CATEGORY_UNKNOWN: _format_unknown,
}

_HEADINGS = {
    CATEGORY_DIRECT: "顺手可用",
    CATEGORY_STRETCH: "努一把",
    CATEGORY_PAPER: "纸面富贵 · 建议放弃",
    CATEGORY_UNKNOWN: "无法判断",
}


def _category_summary(category: str, report: dict) -> str:
    """各类别标题后缀 —— 直接把这类券涉及的钱标出来。"""
    items = (report.get("by_category") or {}).get(category) or []
    count = len(items)

    if category == CATEGORY_DIRECT:
        money = round(sum(v.net_value for v in items), 2)
        return f"【{_HEADINGS[category]}】{count} 张 · 净省 ¥{money}"
    if category == CATEGORY_STRETCH:
        gain = round(sum(v.net_value for v in items), 2)
        cost = round(sum(v.extra_spend for v in items), 2)
        return f"【{_HEADINGS[category]}】{count} 张 · 净赚 ¥{gain}（需额外消费 ¥{cost}）"
    if category == CATEGORY_PAPER:
        face = round(sum(v.face_value for v in items), 2)
        return f"【{_HEADINGS[category]}】{count} 张 · 名义 ¥{face}"
    return f"【{_HEADINGS[category]}】{count} 张"


def render_portfolio(report: dict) -> str:
    """把券包体检结果渲染成面向用户的文本。"""
    by_category = report.get("by_category") or {}

    if not report.get("verdicts"):
        return "你目前手里没有可用的券，暂时不需要做券包体检。"

    lines: list[str] = [_RULE, "券包体检", _RULE, ""]
    lines.append(
        f"  名义总额 ¥{report['face_total']}（{len(report['verdicts'])} 张）"
        f" → 可实现 ¥{report['realizable']}"
    )
    # 只把「纸面富贵」那一类的名义金额称作纸面富贵。
    # `gap`（名义 − 可实现）还含「兑现努一把需额外消费」的部分，混在一起会夸大。
    if report.get("paper_face", 0) > 0:
        lines.append(
            f"  其中 ¥{report['paper_face']} 是纸面富贵"
            f" —— 为用它要多花的钱 ≥ 券面价值"
        )
    lines.append("")

    for category in (
        CATEGORY_DIRECT,
        CATEGORY_STRETCH,
        CATEGORY_PAPER,
        CATEGORY_UNKNOWN,
    ):
        items = by_category.get(category) or []
        if not items:
            continue
        lines.append(_category_summary(category, report))
        formatter = _FORMATTERS[category]
        for verdict in items:
            lines.append(formatter(verdict))
        lines.append("")

    lines += [_RULE, _conclusion(report)]
    return "\n".join(lines).rstrip()


def _conclusion(report: dict) -> str:
    """生成结尾的行动建议（可能多行）。"""
    counts = report.get("counts") or {}
    realizable = report.get("realizable", 0.0)
    direct = counts.get(CATEGORY_DIRECT, 0)
    paper = counts.get(CATEGORY_PAPER, 0)

    parts: list[str] = []
    if realizable > 0:
        if direct > 0:
            parts.append(
                f"把能拿到的 ¥{realizable} 用掉（其中 {direct} 张顺手可用），"
                f"不用掉就是纯损失。"
            )
        else:
            parts.append(
                f"把能拿到的 ¥{realizable} 用掉 —— 虽要一点额外消费，"
                f"但净赚仍为正，不用掉才是纯损失。"
            )
    if paper:
        pronoun = "它" if paper == 1 else "它们"
        parts.append(f"其余 {paper} 张建议放弃：为{pronoun}多花的钱 ≥ 券面价值。")

    return "\n".join(parts) if parts else "券包整体没有可利用空间，可以放心。"


# --------------------------------------------------------------------------
# 用券排程
# --------------------------------------------------------------------------


@dataclass
class ScheduledCoupon:
    """排程中安排到的一次用券机会。"""

    slot: int  # 第几单（1 起）
    verdict: CouponVerdict


@dataclass
class RedemptionPlan:
    """用券排程结果。"""

    window_days: int  # 窗口天数（券全部过期前的天数）
    frequency: float  # 点单频率（单/天）
    opportunities: int  # 窗口内还会点几单
    scheduled: list[ScheduledCoupon]  # 安排得上的
    missed: list[CouponVerdict]  # 有净价值却排不上（注定浪费）
    unjudgeable: list[CouponVerdict]  # 券面价值无法反查
    expired: list[CouponVerdict]  # 已过期

    @property
    def captured(self) -> float:
        """排程之后真正能拿到的净价值。"""
        return round(sum(s.verdict.net_value for s in self.scheduled), 2)

    @property
    def missed_value(self) -> float:
        """有净价值、但因机会不够而注定拿不到的金额。"""
        return round(sum(v.net_value for v in self.missed), 2)

    @property
    def realizable_total(self) -> float:
        """排程前的「可实现」总额（排得上 + 排不上）。"""
        return round(self.captured + self.missed_value, 2)


def order_frequency(orders: Sequence[OrderRecord], observation_days: int) -> float:
    """由历史订单推算点单频率（单/天）。

    这是排程的前提 —— 没有它就估不出「券过期前你还会点几单」。
    观察窗口由调用方给出（本项目默认取近 90 天）。
    """
    if observation_days <= 0:
        return 0.0
    return len(orders) / observation_days


def plan_redemption(
    coupons: list[CouponStock],
    natural_spend: float,
    frequency: float,
    today: Optional[date] = None,
    default_window_days: int = 30,
) -> RedemptionPlan:
    """在有限的点单机会里，安排该用哪几张券。

    模型（简化过的 —— 三个假设都写在这里，不藏在代码里）

    1. 券全部过期前，你还会点 ``K = floor(窗口天数 × 点单频率)`` 单
    2. 每单最多用 1 张券
    3. **用券不改变你的点单次数**，只改变每单用什么券

    在这个模型下，最优策略就是「按净价值从高到低取前 K 张」：
    各次机会等价、每张券只消耗一次机会，因此贪心即最优，无需复杂调度。

    由此得到本函数最关键的结论：**可实现的券，未必用得上。**
    你可能有 5 张可用券，但只剩 2 次点单机会 —— 另外 3 张注定浪费。

    「纸面富贵」（净价值 ≤ 0）不参与排程 —— 那些券本就建议放弃。
    """
    verdicts = [classify_coupon(c, natural_spend, today) for c in coupons]

    expired: list[CouponVerdict] = []
    unjudgeable: list[CouponVerdict] = []
    usable: list[CouponVerdict] = []

    for verdict in verdicts:
        if verdict.days_left is not None and verdict.days_left < 0:
            expired.append(verdict)
        elif verdict.category == CATEGORY_UNKNOWN:
            unjudgeable.append(verdict)
        elif verdict.is_realizable:
            usable.append(verdict)

    window_days = 0
    if usable:
        window_days = max(
            v.days_left if v.days_left is not None else default_window_days
            for v in usable
        )

    opportunities = 0
    if usable and window_days > 0 and frequency > 0:
        opportunities = max(1, int(window_days * frequency))

    # 净价值从高到低；价值相同时，先安排更早过期的
    usable.sort(key=lambda v: (-v.net_value, v.days_left or 9999))

    scheduled = [
        ScheduledCoupon(slot=index, verdict=verdict)
        for index, verdict in enumerate(usable[:opportunities], start=1)
    ]

    return RedemptionPlan(
        window_days=window_days,
        frequency=frequency,
        opportunities=opportunities,
        scheduled=scheduled,
        missed=usable[opportunities:],
        unjudgeable=unjudgeable,
        expired=expired,
    )


def _schedule_conclusion(plan: RedemptionPlan) -> str:
    """生成排程结论 —— 机会不够时点明注定浪费多少。"""
    if plan.missed_value > 0:
        return (
            f"券包可实现 ¥{plan.realizable_total}，但机会只够用掉 "
            f"¥{plan.captured} —— 剩下 ¥{plan.missed_value} 注定随券过期浪费。"
        )
    if plan.captured > 0:
        return f"机会够用：可实现的 ¥{plan.captured} 都能安排上。"
    return "没有可安排的券。"


def render_schedule(plan: RedemptionPlan) -> str:
    """把用券排程渲染成面向用户的文本。"""
    lines: list[str] = [_RULE, "用券排程", _RULE, ""]

    if not plan.scheduled and plan.opportunities <= 0:
        lines.append("  无法排程：缺少点单频率（没有可用的历史订单）。")
        lines.append("")
        lines += [_RULE, "先积累几单历史，才能推算你还会点几次。"]
        return "\n".join(lines).rstrip()

    lines.append(
        f"  窗口 {plan.window_days} 天（券全部过期前）"
        f" · 点单频率 {round(plan.frequency, 3)} 单/天"
    )
    lines.append(
        f"  → 这期间你大概只会点 {plan.opportunities} 单，"
        f"最多用掉 {plan.opportunities} 张券"
    )
    lines.append("")

    if plan.scheduled:
        lines.append("  建议安排")
        for step in plan.scheduled:
            lines.append(
                f"    第 {step.slot} 单 → {step.verdict.name}"
                f"    净赚 {step.verdict.net_value} 元"
            )
        lines.append("")

    if plan.missed:
        lines.append(
            f"  排不上（{len(plan.missed)} 张 · 可实现 ¥{plan.missed_value}）"
        )
        for verdict in plan.missed:
            lines.append(
                f"    · {verdict.name}    净价值 {verdict.net_value} 元"
                f"    {verdict.expiry_text()}"
            )
        lines.append("")

    if plan.expired:
        lines.append(f"  已过期（{len(plan.expired)} 张）")
        for verdict in plan.expired:
            lines.append(f"    · {verdict.name}")
        lines.append("")

    if plan.unjudgeable:
        lines.append(f"  无法判断（{len(plan.unjudgeable)} 张）")
        for verdict in plan.unjudgeable:
            lines.append(f"    · {verdict.name}    菜单中无对应商品，未估算")
        lines.append("")

    lines += [_RULE, _schedule_conclusion(plan)]
    return "\n".join(lines).rstrip()


__all__ = [
    "CATEGORY_DIRECT",
    "CATEGORY_LABELS",
    "CATEGORY_PAPER",
    "CATEGORY_STRETCH",
    "CATEGORY_UNKNOWN",
    "CouponVerdict",
    "RedemptionPlan",
    "ScheduledCoupon",
    "audit_coupon_portfolio",
    "classify_coupon",
    "order_frequency",
    "plan_redemption",
    "render_portfolio",
    "render_schedule",
]
