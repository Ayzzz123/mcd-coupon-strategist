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
from datetime import date, timedelta
from typing import Optional


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass
class CouponStock:
    """一张券的库存状态（用于沉默成本计算与净价值判定）。"""

    coupon_id: str
    name: str
    value: float  # 券面价值（元）—— 即用券可省下的金额
    expire_date: Optional[date] = None  # 有效期截止日
    threshold: float = 0.0  # 满减门槛（元），仅满减券有意义
    required_spend: float = 0.0  # 触发这张券至少需要消费多少（元）
    # 以下三项来自 query-my-coupons（账户券），门店券接口不提供
    validity_text: str = ""  # 有效期原文（含时段与星期限制）
    day_window: str = ""  # 单日券的可用时段（如 "05:00-10:29"）；跨日券为空
    channel_tags: str = ""  # 可用渠道标签原文（如「到店专用、外送专用」）
    weekdays: frozenset = frozenset()  # 可用星期（0=周一）；空集=不限
    weekday_text: str = ""  # 星期限制的可读描述（如「周一至周五」）
    valid_from: Optional[date] = None  # 生效日（早餐券可能明天才开始生效）

    def is_usable_on(self, day: date) -> Optional[bool]:
        """这张券在指定日期能否使用。

        返回 None 表示**没有星期限制**（任何一天都能用），
        而不是「不能用」—— 两者含义完全不同，不要混。
        """
        if not self.weekdays:
            return None
        return day.weekday() in self.weekdays

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
    status: str = ""  # 原始 orderStatus，用于识别「部分退款」
    order_hour: Optional[int] = None  # 下单小时（0-23）。未知则为 None

    @property
    def time_of_day(self) -> Optional[str]:
        """这笔订单属于哪个供应时段。小时未知则返回 None。"""
        return time_of_day(self.order_hour)

    @property
    def is_spending(self) -> bool:
        """是否是**真实消费**（金额大于 0）。

        0 元订单（多为券核销或积分兑换）不算消费。它只在有券时才会出现，
        拿它去估「不看券时你本来会花多少钱」是循环论证，还会把基准拉低 ——
        而基准偏低会让所有券都显得更难用上。

        但它**确实是一次点单**，所以推算点单频率时仍然计入。
        这正是本项目区分「消费口径」与「点单口径」的原因。
        """
        return self.amount > 0


# --------------------------------------------------------------------------
# 供应时段
# --------------------------------------------------------------------------

# 麦当劳的菜单是分时段供应的（早餐卖麦满分，正餐卖汉堡），
# 所以「什么时候点的」和「点了多少钱」一样是消费结构的一部分。
TIME_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("早餐", 5, 10),   # 05:00 - 10:59
    ("午市", 11, 13),  # 11:00 - 13:59
    ("下午", 14, 16),  # 14:00 - 16:59
    ("晚市", 17, 21),  # 17:00 - 21:59
    ("夜宵", 22, 4),   # 22:00 - 04:59（跨零点）
)

# 一个时段至少要这么多**有实际支出**的订单，才允许给出该时段的客单价。
# 低于这个数，样本量支撑不起一个「基准」，报出来只是噪音 ——
# 宁可说「样本不足」，也不给一个看起来很精确的假数字。
MIN_ORDERS_PER_BUCKET = 3


def time_of_day(hour: Optional[int]) -> Optional[str]:
    """把 0-23 的小时映射到供应时段名。小时未知则返回 None。"""
    if hour is None:
        return None
    for name, start, end in TIME_BUCKETS:
        if start <= end:
            if start <= hour <= end:
                return name
        elif hour >= start or hour <= end:  # 跨零点的夜宵
            return name
    return None


# --------------------------------------------------------------------------
# 券的沉默成本
# --------------------------------------------------------------------------


def count_unusable_days(coupon: CouponStock, today: date) -> int:
    """券的**有效期内**，有多少天因星期限制而用不了。

    券常带星期限制（如「周一至周五」），周末就用不了。只报「还剩 N 天」
    会让周五的用户以为周末还能用 —— 实际只剩当天。

    ⚠️ 必须从「今天与生效日中的较晚者」开始数。单日券（如只有 10-15 当天
    有效）在今天看来「用不了」，是因为它**还没生效**，不是因为它不生效 ——
    这两种情况必须分开，否则会报出「剩余期里有 1 天用不了」这种误导。

    无星期限制、或没有截止日时返回 0。
    """
    if not coupon.weekdays or coupon.expire_date is None:
        return 0

    start = today
    if coupon.valid_from is not None and coupon.valid_from > start:
        start = coupon.valid_from

    unusable = 0
    day = start
    while day <= coupon.expire_date:
        if day.weekday() not in coupon.weekdays:
            unusable += 1
        day += timedelta(days=1)
    return unusable


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
                f"{len(today_due)} 张今天到期（面值 {today_value} 元），"
                f"今天不用就作废"
            )

        if upcoming:
            # 「N 天内」取窗口上限：9 张券散布在 1~7 天时，写「1 天内」会让人
            # 误以为全部明天就没了，「7 天内」才是准确的紧迫度上界。
            days_max = max(c.days_left(today) for c in upcoming)
            parts.append(
                f"{len(upcoming)} 张将在 {days_max} 天内陆续过期"
                f"（面值 {upcoming_value} 元）"
            )

        total_at_risk = round(expired_value + today_value + upcoming_value, 2)

        # 价值为 0 说明券面无法与菜单对应 —— 它们同样在过期，只是金额算不出来。
        # 必须说出来，否则「面值 X 元」会被读成「全部损失只有 X 元」。
        unknown = [c for c in at_risk if c.value <= 0]
        if unknown:
            parts.append(
                f"另有 {len(unknown)} 张的券面价值无法从菜单推算，未计入面值"
            )

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
                # 账户券才有这几项（时段 / 星期 / 渠道），门店券接口不提供。
                # 早餐券常常只有当天早上两三个小时能用，这个限制必须让用户看见。
                "day_window": c.day_window,
                "channel_tags": c.channel_tags,
                "weekday_text": c.weekday_text,
                "unusable_days": count_unusable_days(c, today),
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
      "order_count": int,            # 点单口径：全部订单（含 0 元单）
      "spending_order_count": int,   # 消费口径：有实际支出的订单
      "free_order_count": int,       # 0 元订单（券核销 / 积分兑换）
      "partial_refund_count": int,   # 部分退款订单（退款金额接口未提供）
      "total_spent": float,
      "avg_per_order": float,        # 客单价 = 总支出 / 消费性订单数
      "avg_all_orders": float,       # 总支出 / 全部订单数（仅作对照）
      "daily_avg": float,
      "channel_mix": {...},          # 外送/到店占比
      "top_items": [...],            # 高频餐品
      "time_buckets": [...],         # 供应时段分布（见下方「时段」说明）
      "main_bucket": str | None,     # 主力时段；占比不够高则为 None
      "insights": [str, ...],        # 可执行的省钱洞察
    }

    两个口径必须分开，否则结论会系统性失真：

    - **消费口径**（`avg_per_order`）—— 算钱、当「自然消费基准」。
      0 元订单不参与：它只在有券时才会出现，拿它当「不看券时会花多少」
      是循环论证，还会把基准拉低，让所有券都显得更难用上。
    - **点单口径**（`order_count`）—— 算机会。0 元订单也算一次点单，
      所以推算「券过期前你还会点几单」时应当计入。

    时段说明
    --------
    麦当劳的菜单是**分时段供应**的（早餐卖麦满分，正餐卖汉堡），
    所以「几点点的」和「花了多少」一样，都是消费结构的一部分。

    但每个时段的样本往往很少，直接用它会得到一堆噪音。因此
    **只有该时段有支出的订单数达到 `MIN_ORDERS_PER_BUCKET`，才给出
    该时段的客单价**；达不到就只报单数并标注样本不足，`avg` 为 None。
    宁可说「样本不足」，也不给一个看起来很精确的假数字。
    """

    if not orders:
        return {
            "order_count": 0, "spending_order_count": 0, "free_order_count": 0,
            "partial_refund_count": 0, "total_spent": 0.0,
            "avg_per_order": 0.0, "avg_all_orders": 0.0,
            "daily_avg": 0.0, "channel_mix": {}, "top_items": [],
            "time_buckets": [], "main_bucket": None,
            "insights": ["还没有历史订单，多下几单后就能给你做消费分析了。"],
        }

    total = round(sum(o.amount for o in orders), 2)
    count = len(orders)

    # 消费口径与点单口径分离
    spending_count = sum(1 for o in orders if o.is_spending)
    free_count = count - spending_count
    partial_refund_count = sum(1 for o in orders if "部分退款" in o.status)

    avg = round(total / spending_count, 2) if spending_count else 0.0
    avg_all = round(total / count, 2) if count else 0.0

    # 渠道分布
    channel_count: dict[str, int] = {}
    for o in orders:
        channel_count[o.channel or "未知"] = channel_count.get(o.channel or "未知", 0) + 1

    # 供应时段分布。小时未知的订单**不参与**，也不凭空归到某一类。
    bucket_stat: dict[str, dict] = {}
    for o in orders:
        name = o.time_of_day
        if name is None:
            continue
        b = bucket_stat.setdefault(name, {"orders": 0, "spending": 0, "total": 0.0})
        b["orders"] += 1
        if o.is_spending:
            b["spending"] += 1
            b["total"] += o.amount

    # 按时间顺序输出（早 → 晚），而不是按单数排 —— 时段本身是有序的
    time_buckets: list[dict] = []
    for name, _start, _end in TIME_BUCKETS:
        b = bucket_stat.get(name)
        if not b:
            continue
        bucket_total = round(b["total"], 2)
        reliable = b["spending"] >= MIN_ORDERS_PER_BUCKET
        time_buckets.append({
            "name": name,
            "orders": b["orders"],
            "spending_orders": b["spending"],
            "total": bucket_total,
            "avg": round(bucket_total / b["spending"], 2) if reliable else None,
            "reliable": reliable,
        })

    # 已识别时段的订单数 —— 占比的分母用它，而不是全部订单数，
    # 否则小时未知的订单会把每个时段的占比都压低。
    bucketed = sum(b["orders"] for b in time_buckets)

    # 主力时段：既要单数够多，也要占比够高，否则「最集中的那个」可能只是
    # 一堆 1 单里挑出来的最大者，没有意义。
    main_bucket: Optional[str] = None
    if time_buckets:
        top = max(time_buckets, key=lambda b: b["orders"])
        if top["orders"] >= MIN_ORDERS_PER_BUCKET and top["orders"] >= bucketed * 0.4:
            main_bucket = top["name"]

    # 高频餐品
    item_count: dict[str, int] = {}
    for o in orders:
        for it in o.items:
            item_count[it] = item_count.get(it, 0) + 1
    top_items = sorted(item_count.items(), key=lambda kv: -kv[1])[:5]

    days = max(months, 1) * 30
    daily = round(total / days, 2)

    insights: list[str] = []

    # 洞察 1：月度总支出 + 两个口径的说明
    if free_count:
        insights.append(
            f"近 {months} 个月你在麦当劳共消费 {total} 元，"
            f"其中 {spending_count} 笔有实际支出，平均每单 {avg} 元。"
        )
        insights.append(
            f"另有 {free_count} 笔 0 元订单（多为券核销或积分兑换）—— "
            f"不计入客单价，但算作一次点单机会。"
        )
    else:
        insights.append(
            f"近 {months} 个月你在麦当劳共消费 {total} 元，"
            f"共 {count} 单，平均每单 {avg} 元。"
        )

    # 部分退款：接口不给退款金额，所以只能如实说明不确定，不能假装没这回事
    if partial_refund_count:
        insights.append(
            f"还有 {partial_refund_count} 笔「部分退款」订单 —— 接口不返回退款金额，"
            f"无法确认 realTotalAmount 是退款前还是退款后的口径，上面的支出可能略偏高。"
        )

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

    # 洞察 6：主力时段
    if main_bucket and bucketed:
        top = next(b for b in time_buckets if b["name"] == main_bucket)
        share = round(top["orders"] * 100 / bucketed)
        insights.append(
            f"你的点单集中在「{main_bucket}」时段：{bucketed} 单里有 "
            f"{top['orders']} 单（{share}%）落在这里 —— "
            f"菜单是分时段供应的，选券时要优先挑这个时段用得上的。"
        )

    # 洞察 7：口径提醒 —— 全局客单价是混合口径。
    # 这是「口径」问题在本项目里的第二次出现（第一次是 0 元订单污染基准）。
    unreliable = [b["name"] for b in time_buckets if not b["reliable"]]
    if len(time_buckets) > 1 and unreliable:
        names = "、".join(unreliable)
        insights.append(
            f"口径提醒：{avg} 元这个客单价是「全时段混合」的平均值。"
            f"「{names}」时段有支出的订单还不到 {MIN_ORDERS_PER_BUCKET} 笔，"
            f"样本不足以单独估算 —— 如果那里实际花得和整体不一样，"
            f"用这个基准去判断对应时段的券就会偏。"
        )

    # 洞察 8：某个样本充足的时段，客单价与整体差异明显
    for b in time_buckets:
        if not b["reliable"] or not avg:
            continue
        gap = round(abs(b["avg"] - avg), 2)
        if gap >= max(5.0, avg * 0.3):
            direction = "高" if b["avg"] > avg else "低"
            insights.append(
                f"「{b['name']}」时段客单价 {b['avg']} 元，比整体 {avg} 元"
                f"{direction} {gap} 元 —— 判断这个时段能用的券时，"
                f"用整体客单价会偏。"
            )

    return {
        "order_count": count,
        "spending_order_count": spending_count,
        "free_order_count": free_count,
        "partial_refund_count": partial_refund_count,
        "total_spent": total,
        "avg_per_order": avg,
        "avg_all_orders": avg_all,
        "daily_avg": daily,
        "channel_mix": channel_count,
        "top_items": [{"name": n, "times": t} for n, t in top_items],
        "time_buckets": time_buckets,
        "main_bucket": main_bucket,
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

            # 时段 / 星期 / 渠道限制只对**真的有限制**的券显示 ——
            # 同时标着「到店专用、外送专用」的券其实是通用的，写了也没信息量。
            # 而早餐券常常只有当天早上两三个小时能用，这个限制必须让用户看见。
            limits: list[str] = []
            window = it.get("day_window") or ""
            weekdays = it.get("weekday_text") or ""
            if window:
                when = f"{weekdays} {window}".strip() if weekdays else window
                limits.append(f"仅 {when} 可用")
            elif weekdays and weekdays != "每天":
                limits.append(f"{weekdays} 可用")

            tags = it.get("channel_tags") or ""
            if tags and not ("到店" in tags and "外送" in tags):
                limits.append(tags)

            if limits:
                line = " · ".join(limits)
                unusable = it.get("unusable_days") or 0
                if unusable:
                    # 「还剩 N 天」里有几天券根本不生效 —— 必须说清，
                    # 否则用户会按剩余天数安排，最后发现那些天用不了。
                    line += f"（剩余期里有 {unusable} 天用不了）"
                lines.append(f"      {line}")

        if len(at_risk) > 5:
            lines.append(f"    …… 另有 {len(at_risk) - 5} 张（为控制篇幅未展开）")
    lines.append("")

    # 第二部分：消费画像
    lines.append("【二】消费习惯")
    if p.get("order_count"):
        free = p.get("free_order_count", 0)
        spending = p.get("spending_order_count", p["order_count"])

        count_txt = f"  订单数：{p['order_count']} 单"
        if free:
            count_txt += f"（其中 {free} 笔 0 元）"
        lines.append(count_txt)

        lines.append(f"  总支出：{p['total_spent']} 元")

        # 客单价按「有支出的订单」算：0 元订单不是消费，计入会把基准拉低
        avg_txt = f"  客单价：{p['avg_per_order']} 元"
        if free:
            avg_txt += f"（按 {spending} 笔有支出的订单计算）"
        lines.append(avg_txt)

        # 时段分布：只在订单带时段信息时输出。样本不足的时段只报单数，
        # 不给客单价 —— 阈值判断在 consumption_profile 里，这里只负责如实呈现。
        buckets = p.get("time_buckets") or []
        if buckets:
            lines.append("")
            lines.append("  时段分布：")
            for b in buckets:
                if b["spending_orders"] != b["orders"]:
                    scope = f"{b['orders']} 单（{b['spending_orders']} 笔有支出）"
                else:
                    scope = f"{b['orders']} 单"
                if b["reliable"]:
                    tail = f"客单价 {b['avg']} 元"
                else:
                    tail = f"样本不足 {MIN_ORDERS_PER_BUCKET} 笔，未估算客单价"
                lines.append(
                    f"    {b['name']} · {scope} · 支出 {b['total']} 元 · {tail}"
                )

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
    at_risk_items = c.get("at_risk_items") or []
    unknown = sum(1 for it in at_risk_items if it.get("value", 0) <= 0)
    if at_risk_value > 0:
        lines.append(f"本月最该做的事：先把 {at_risk_value} 元的券用掉，")
        lines.append("这笔钱不用就是纯损失。")
        if unknown:
            # 价值未知的券同样在流失，只是金额算不出来 —— 不能让它们在
            # 汇总里隐身，否则用户会以为「处理完 21.1 元就完事了」。
            lines.append(
                f"另外 {unknown} 张券同样要过期，但券面价值还没算出来 —— "
                f"看清它们的时间和限制，别让它们悄悄走掉。"
            )
    elif unknown:
        lines.append(
            f"有 {unknown} 张券即将过期，但券面价值都还估算不出来 —— "
            f"先看清它们的时间和限制。"
        )
    else:
        lines.append("券管理得不错，没有即将浪费的券。")

    return "\n".join(lines)


__all__ = [
    "MIN_ORDERS_PER_BUCKET",
    "TIME_BUCKETS",
    "CouponStock",
    "OrderRecord",
    "coupon_silent_cost",
    "consumption_profile",
    "audit_report",
    "count_unusable_days",
    "render_audit",
    "time_of_day",
]
