"""消费审计 —— 从「单次省钱」升级到「长期消费画像」。

这是本项目区别于其他省钱类工具的核心模块。

其他省钱工具都在回答同一个问题：**这一单怎么点最省？**
但那是一次性的、局部的。用户真正的痛点其实是长期的：

    「我一个月在麦当劳花了多少？」
    「我手里这些券，有多少张是白白过期的？」
    「我每次都在单点，如果改成套餐，一个月能省多少？」

本模块回答这些长期问题，数据来自两个 MCP 工具：
- `order-list`       历史订单（用于还原消费习惯）
- `query-my-coupons` 手中的券存量（用于计算券的沉默成本）

**核心指标：券的沉默成本（Coupon Silent Cost）**

用户手里躺着 N 张券，每张都有面值和有效期。如果过期前没用掉，
那张券对应的优惠金额就是「白扔的钱」。这个数字是可量化的，而且足够扎心。

    ┌────────────────────────────────────────────┐
    │  你手里有 13 张券，其中 5 张将在 7 天内过期  │
    │  若未使用，将白白损失 ¥47.5                 │
    └────────────────────────────────────────────┘

本模块只做计算，**不估算任何价格** —— 券面金额全部来自 MCP 返回的真实数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass
class CouponStock:
    """一张券的库存状态（用于沉默成本计算）。"""

    coupon_id: str
    name: str
    value: float  # 券面价值（元）—— 即用券可省下的金额
    expire_date: Optional[date] = None  # 有效期截止日
    threshold: float = 0.0  # 使用门槛（元）

    def days_left(self, today: Optional[date] = None) -> Optional[int]:
        """距过期还有几天。无有效期则返回 None。"""
        if self.expire_date is None:
            return None
        today = today or date.today()
        return (self.expire_date - today).days

    def is_expiring_soon(self, within_days: int = 7,
                         today: Optional[date] = None) -> bool:
        """是否即将过期（含已过期）。"""
        d = self.days_left(today)
        return d is not None and d <= within_days


@dataclass
class OrderRecord:
    """一笔历史订单（简化）。"""

    order_time: date
    amount: float  # 实付金额（元）
    items: list[str] = field(default_factory=list)
    channel: str = ""  # 外送 / 到店


# --------------------------------------------------------------------------
# 券的沉默成本
# --------------------------------------------------------------------------


def coupon_silent_cost(
    coupons: list[CouponStock],
    today: Optional[date] = None,
    expiring_within: int = 7,
) -> dict:
    """计算券的沉默成本 —— 本项目最核心的差异化指标。

    返回：
    {
      "total_value": float,        # 手中所有券的面值合计
      "at_risk_value": float,      # 即将过期券的面值合计（白扔风险）
      "at_risk_count": int,        # 即将过期的券张数
      "at_risk_items": [...],      # 逐张列出，按紧迫度排序
      "narrative": str,            # 一句话结论
    }

    「即将过期」的判定：距过期不足 `expiring_within` 天（含已过期）。
    """

    today = today or date.today()
    total_value = round(sum(c.value for c in coupons), 2)

    at_risk: list[CouponStock] = [
        c for c in coupons if c.is_expiring_soon(expiring_within, today)
    ]
    at_risk.sort(key=lambda c: (c.days_left(today) is None,
                                c.days_left(today) or 0))

    at_risk_value = round(sum(c.value for c in at_risk), 2)

    if not coupons:
        narrative = "你目前手里没有可用的券。"
    elif at_risk:
        # 分开统计「已过期」与「仍可抢救」，避免把今天到期的误说成已过期
        expired = [c for c in at_risk if (c.days_left(today) or 0) < 0]
        today_due = [c for c in at_risk if c.days_left(today) == 0]
        upcoming = [
            c for c in at_risk
            if (d := c.days_left(today)) is not None and d > 0
        ]

        expired_value = round(sum(c.value for c in expired), 2)
        today_value = round(sum(c.value for c in today_due), 2)
        upcoming_value = round(sum(c.value for c in upcoming), 2)

        parts: list[str] = []

        if expired:
            parts.append(
                f"{len(expired)} 张已经过期（面值 {expired_value} 元），"
                f"这部分钱已经扔掉了"
            )

        if today_due:
            # 今天到期是最紧迫的信号，单独强调
            parts.append(
                f"**{len(today_due)} 张今天到期（面值 {today_value} 元），"
                f"今天不用就作废**"
            )

        if upcoming:
            days_min = min(c.days_left(today) for c in upcoming)
            parts.append(
                f"{len(upcoming)} 张将在 {days_min} 天内陆续过期"
                f"（面值 {upcoming_value} 元）"
            )

        total_at_risk = round(expired_value + today_value + upcoming_value, 2)

        if len(parts) == 1:
            narrative = f"你有 {parts[0]}。"
        else:
            narrative = f"你的券有风险：" + "；".join(parts) + "。"
    else:
        narrative = (
            f"你手里有 {len(coupons)} 张券，面值合计 {total_value} 元，"
            f"近期没有即将过期的，可以放心。"
        )

    return {
        "total_value": total_value,
        "at_risk_value": at_risk_value,
        "at_risk_count": len(at_risk),
        "at_risk_items": [
            {
                "name": c.name,
                "value": c.value,
                "days_left": c.days_left(today),
                "threshold": c.threshold,
            }
            for c in at_risk
        ],
        "narrative": narrative,
    }


# --------------------------------------------------------------------------
# 消费习惯画像
# --------------------------------------------------------------------------


def consumption_profile(
    orders: list[OrderRecord],
    months: int = 1,
) -> dict:
    """从历史订单还原消费习惯，找出结构性浪费。

    返回：
    {
      "order_count": int,
      "total_spent": float,
      "avg_per_order": float,
      "daily_avg": float,
      "channel_mix": {...},         # 外送/到店占比
      "top_items": [...],           # 高频餐品
      "insights": [str, ...],       # 可执行的省钱洞察
    }
    """

    if not orders:
        return {
            "order_count": 0, "total_spent": 0.0, "avg_per_order": 0.0,
            "daily_avg": 0.0, "channel_mix": {}, "top_items": [],
            "insights": ["还没有历史订单，多下几单后就能给你做消费分析了。"],
        }

    total = round(sum(o.amount for o in orders), 2)
    count = len(orders)
    avg = round(total / count, 2) if count else 0.0

    # 渠道分布
    channel_count: dict[str, int] = {}
    for o in orders:
        channel_count[o.channel or "未知"] = channel_count.get(o.channel or "未知", 0) + 1

    # 高频餐品
    item_count: dict[str, int] = {}
    for o in orders:
        for it in o.items:
            item_count[it] = item_count.get(it, 0) + 1
    top_items = sorted(item_count.items(), key=lambda kv: -kv[1])[:5]

    days = max(months, 1) * 30
    daily = round(total / days, 2)

    insights: list[str] = []

    # 洞察 1：月度总支出
    insights.append(f"近 {months} 个月你在麦当劳共消费 {total} 元，平均每单 {avg} 元。")

    # 洞察 2：日均
    if daily >= 20:
        insights.append(
            f"折合每天 {daily} 元，一个月就是 {round(daily * 30, 2)} 元 —— "
            f"这笔钱值得认真优化一下。"
        )

    # 洞察 3：订单结构
    if avg >= 35:
        insights.append(
            f"你的平均客单价 {avg} 元偏高。可以试试用三件套替代单点组合，"
            f"同样内容通常能便宜 10 元以上。"
        )

    # 洞察 4：高频单品 → 建议改成套餐
    if top_items:
        name, times = top_items[0]
        if times >= 3:
            insights.append(
                f"你点过 {times} 次「{name}」。如果给它配上套餐，"
                f"每次通常能省 5~13 元，{times} 次下来就是一笔不小的数。"
            )

    # 洞察 5：渠道
    if len(channel_count) > 1:
        mix = "、".join(f"{k} {v} 单" for k, v in
                       sorted(channel_count.items(), key=lambda kv: -kv[1]))
        insights.append(f"下单渠道分布：{mix}。外送会额外产生配送费，计入成本会更准。")

    return {
        "order_count": count,
        "total_spent": total,
        "avg_per_order": avg,
        "daily_avg": daily,
        "channel_mix": channel_count,
        "top_items": [{"name": n, "times": t} for n, t in top_items],
        "insights": insights,
    }


# --------------------------------------------------------------------------
# 综合报告
# --------------------------------------------------------------------------


def audit_report(
    orders: list[OrderRecord],
    coupons: list[CouponStock],
    months: int = 1,
    today: Optional[date] = None,
) -> dict:
    """生成完整消费审计报告 —— 把「单次省钱」升级成「长期画像」。"""

    return {
        "coupons": coupon_silent_cost(coupons, today=today),
        "profile": consumption_profile(orders, months=months),
    }


def render_audit(report: dict) -> str:
    """渲染消费审计报告为面向用户的文本。"""

    lines: list[str] = []
    c = report.get("coupons") or {}
    p = report.get("profile") or {}

    lines.append("=" * 46)
    lines.append("你的麦当劳消费审计")
    lines.append("=" * 46)
    lines.append("")

    # 第一部分：券的沉默成本
    lines.append("【一】券的沉默成本")
    lines.append(f"  {c.get('narrative', '')}")
    at_risk = c.get("at_risk_items") or []
    if at_risk:
        lines.append("")
        lines.append("  优先处理这几张：")
        for it in at_risk[:5]:
            d = it.get("days_left")
            if d is None:
                when = "无期限"
            elif d < 0:
                when = "已过期"
            elif d == 0:
                when = "今天到期"
            else:
                when = f"还剩 {d} 天"
            th = f"（满 {it['threshold']} 元可用）" if it.get("threshold") else ""
            # 价值为 0 说明券面无法与菜单商品对应。按「宁可少报，不可编造」原则
            # 如实标注「价值未知」，而不是显示一个看起来坏掉的「省 0.0 元」。
            if it.get("value", 0) > 0:
                value_txt = f"省 {it['value']} 元"
            else:
                value_txt = "价值未知（菜单中无对应商品，未估算）"
            lines.append(f"    · {it['name']}  {value_txt}  {when}{th}")
    lines.append("")

    # 第二部分：消费画像
    lines.append("【二】消费习惯")
    if p.get("order_count"):
        lines.append(f"  订单数：{p['order_count']} 单")
        lines.append(f"  总支出：{p['total_spent']} 元")
        lines.append(f"  客单价：{p['avg_per_order']} 元")
        lines.append("")
        lines.append("  洞察：")
        for ins in p.get("insights", []):
            lines.append(f"    · {ins}")
    else:
        for ins in p.get("insights", []):
            lines.append(f"  {ins}")

    lines.append("")
    lines.append("=" * 46)

    # 汇总结论
    at_risk_value = c.get("at_risk_value", 0)
    if at_risk_value > 0:
        lines.append(f"本月最该做的事：先把 {at_risk_value} 元的券用掉，")
        lines.append("这笔钱不用就是纯损失。")
    else:
        lines.append("券管理得不错，没有即将浪费的券。")

    return "\n".join(lines)


__all__ = [
    "CouponStock",
    "OrderRecord",
    "coupon_silent_cost",
    "consumption_profile",
    "audit_report",
    "render_audit",
]
