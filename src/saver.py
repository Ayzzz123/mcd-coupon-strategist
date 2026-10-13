"""麦麦省钱助手 —— 省钱方案枚举与优化核心逻辑。

设计原则：
1. 本模块**不做任何估价**。所有金额一律由调用方（Skill 编排层）通过
   麦当劳 MCP 的 `calculate-price` 取得后回填，避免幻觉价格。
2. 本模块只负责「构造候选组合」与「对计价结果排序筛选」。
3. 对于「为用券凑单反而不划算」的情况，显式产出警告，而不是悄悄丢弃。

术语：
- item      : 单个餐品，形如 {"code": "xxx", "name": "xxx", "price": 0.0, "qty": 1}
- coupon    : 优惠券，形如 {"id": "xxx", "name": "xxx", "threshold": 50.0, "discount": 12.0}
              threshold 表示门槛金额（满额），discount 表示减免金额或折扣力度（元）
- plan      : 一个候选用餐方案，即 {"items": [...], "coupon": coupon | None, "label": str}
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Iterable, Optional


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass
class Item:
    """单个餐品。"""

    code: str
    name: str
    price: float
    qty: int = 1
    category: str = ""
    is_side: bool = False  # 是否为「凑单品」候选（小食、饮品等）

    @property
    def subtotal(self) -> float:
        """该餐品小计（单价 × 数量），保留两位小数。"""
        return round(self.price * self.qty, 2)


@dataclass
class Coupon:
    """优惠券。"""

    id: str
    name: str
    threshold: float = 0.0  # 使用门槛（满 X 元）
    discount: float = 0.0  # 减免金额（元）
    scope: str = ""  # 适用范围备注

    def applicable(self, amount: float) -> bool:
        """判断在给定金额下该券是否可用。"""
        return amount >= self.threshold


@dataclass
class Plan:
    """候选方案。"""

    items: list[Item]
    coupon: Optional[Coupon] = None
    label: str = ""

    def goods_amount(self) -> float:
        """商品原价合计。"""
        return round(sum(i.subtotal for i in self.items), 2)


@dataclass
class PricedPlan:
    """已计价方案 —— 由调用方回填 MCP calculate-price 的真实结果。"""

    plan: Plan
    payable: float  # 应付总价（含配送费，已减优惠）
    discount: float = 0.0  # 优惠金额
    delivery_fee: float = 0.0  # 配送费
    source: str = "calculate-price"  # 金额来源，便于审计

    @property
    def label(self) -> str:
        """方案展示名；调用方未指定时按商品构成自动生成。"""
        return self.plan.label or self._auto_label()

    def _auto_label(self) -> str:
        """按「商品名 + 商品名（使用「券名」）」的格式自动生成标签。"""
        names = " + ".join(i.name for i in self.plan.items)
        if self.plan.coupon:
            return f"{names}（使用「{self.plan.coupon.name}」）"
        return names

    def saving_vs(self, reference: float) -> float:
        """相对参考金额省了多少。"""
        return round(reference - self.payable, 2)


# --------------------------------------------------------------------------
# 方案枚举
# --------------------------------------------------------------------------


def build_base_plans(
    mains: list[Item],
    sides: list[Item],
    combos: list[list[Item]],
) -> list[Plan]:
    """构造基础候选方案：单点组合、套餐方案。"""

    plans: list[Plan] = []

    # 单点主餐
    for m in mains:
        plans.append(Plan(items=[m], label=f"单点：{m.name}"))

    # 主餐 + 配餐的单点组合
    for m in mains:
        for s in sides:
            plans.append(Plan(items=[m, s], label=f"单点组合：{m.name} + {s.name}"))

    # 套餐
    for idx, combo in enumerate(combos, start=1):
        plans.append(Plan(items=list(combo), label=f"套餐方案 {idx}"))

    return plans


def attach_coupons(plans: Iterable[Plan], coupons: list[Coupon]) -> list[Plan]:
    """为每个基础方案挂上所有可能适用的券，生成组合方案。"""

    out: list[Plan] = []
    for plan in plans:
        out.append(plan)  # 不使用券的版本
        amount = plan.goods_amount()
        for c in coupons:
            if c.applicable(amount):
                out.append(
                    Plan(items=plan.items, coupon=c, label=f"{plan.label} + 「{c.name}」")
                )
    return out


def build_filler_plans(
    mains: list[Item],
    sides: list[Item],
    coupons: list[Coupon],
    max_fillers: int = 2,
) -> list[Plan]:
    """构造「为触发高门槛券而凑单」的方案。

    这类方案是省钱场景中最容易踩坑的一类，因此单独枚举，交由后续
    `detect_filler_trap` 判断是否真的划算。
    """

    fillers = [s for s in sides if s.is_side or s.price <= 20]
    if not fillers:
        return []

    plans: list[Plan] = []
    for m in mains:
        for n in range(1, min(max_fillers, len(fillers)) + 1):
            for combo in itertools.combinations(fillers, n):
                items = [m, *combo]
                amount = round(sum(i.subtotal for i in items), 2)
                for c in coupons:
                    if c.applicable(amount):
                        names = " + ".join(i.name for i in items)
                        plans.append(
                            Plan(
                                items=items,
                                coupon=c,
                                label=f"凑单方案：{names} + 「{c.name}」",
                            )
                        )
    return plans


def dedupe_plans(plans: Iterable[Plan]) -> list[Plan]:
    """按 (商品编码组合, 券 id) 去重。"""

    seen: set[tuple] = set()
    out: list[Plan] = []
    for p in plans:
        key = (
            tuple(sorted((i.code, i.qty) for i in p.items)),
            p.coupon.id if p.coupon else None,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


# --------------------------------------------------------------------------
# 排序、筛选与陷阱检测
# --------------------------------------------------------------------------


def rank(priced: list[PricedPlan]) -> list[PricedPlan]:
    """按应付总价升序排列。"""
    return sorted(priced, key=lambda p: (p.payable, -len(p.plan.items)))


def within_budget(priced: list[PricedPlan], budget: float) -> list[PricedPlan]:
    """筛出预算内的方案（保持实付升序）；若全部超预算，返回最便宜的若干个。"""
    ok = [p for p in priced if p.payable <= budget]
    if ok:
        return rank(ok)
    return rank(priced)[:3]


def detect_filler_trap(priced: list[PricedPlan]) -> list[dict]:
    """检测「为用券凑单反而不划算」的情况。

    核心判定：某方案使用了券、且含凑单品，但**相比不使用该券的同款商品组合**
    反而更贵（或持平），则构成凑单陷阱。

    我们只比较「商品构成相同、仅券不同」的方案对，避免把完全不同商品组合的
    方案拿来互相比较（那会产生大量无意义的噪音警告）。

    返回值为警告列表，每条包含被质疑的方案、更优的替代方案与差额。
    """

    def items_key(p: PricedPlan) -> tuple:
        """商品构成的规范化指纹，用于把「同商品、仅券不同」的方案分到一组。"""
        return tuple(sorted((i.code, i.qty) for i in p.plan.items))

    # 按商品构成分组，同组内比较「用券」与「不用券」
    groups: dict[tuple, list[PricedPlan]] = {}
    for p in priced:
        groups.setdefault(items_key(p), []).append(p)

    warnings: list[dict] = []
    for key, group in groups.items():
        no_coupon = [p for p in group if p.plan.coupon is None]
        with_coupon = [p for p in group if p.plan.coupon is not None]
        if not no_coupon or not with_coupon:
            continue

        baseline = rank(no_coupon)[0]
        for cand in with_coupon:
            # 用了券却没有比不用券更便宜 —— 这张券对本组合没有价值
            if cand.payable >= baseline.payable:
                warnings.append(
                    {
                        "trap": cand.label,
                        "trap_payable": cand.payable,
                        "alternative": baseline.label,
                        "alternative_payable": baseline.payable,
                        "extra_items": 0,
                        "delta": round(cand.payable - baseline.payable, 2),
                        "message": (
                            f"「{cand.label}」用了券之后实付 {cand.payable} 元，"
                            f"并不比直接下单的 {baseline.payable} 元更便宜，"
                            f"这张券对这一单没有价值，不必为它凑单。"
                        ),
                    }
                )

    # 再检测跨组合的真·凑单陷阱：为用高门槛券而加购，实付反超更简方案的方案
    # 仅保留「凑单品方案」中的情况，避免对用户未主动要求的商品组合产生噪音提醒
    ranked = rank(priced)
    for p in ranked:
        if p.plan.coupon is None:
            continue
        if not p.label.startswith("凑单方案"):
            continue
        # 商品构成相同的不再重复检测
        if any(w["trap"] == p.label for w in warnings):
            continue
        # 找一个「商品数更少、且实付不高于它」的替代方案
        cheaper = [
            q
            for q in ranked
            if len(q.plan.items) < len(p.plan.items)
            and q.payable <= p.payable
            and q.plan.coupon is not None
        ]
        if cheaper:
            alt = cheaper[0]
            extra = len(p.plan.items) - len(alt.plan.items)
            warnings.append(
                {
                    "trap": p.label,
                    "trap_payable": p.payable,
                    "alternative": alt.label,
                    "alternative_payable": alt.payable,
                    "extra_items": extra,
                    "delta": round(p.payable - alt.payable, 2),
                    "message": (
                        f"为触发券的门槛多买 {extra} 件凑单品，实付 {p.payable} 元；"
                        f"而「{alt.label}」商品更少、实付仅 {alt.payable} 元。"
                        f"除非你本来就想吃凑单品，否则不建议为用券而凑单。"
                    ),
                }
            )

    # 按「多花了多少钱」降序，最值得提醒的排最前
    warnings.sort(key=lambda w: -w["delta"])
    return warnings[:3]


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------


def recommend(
    mains: list[Item],
    sides: list[Item],
    combos: list[list[Item]],
    coupons: list[Coupon],
    price_fn,
    budget: Optional[float] = None,
    top_k: int = 3,
) -> dict:
    """生成省钱推荐。

    参数
    ----
    mains   : 主食候选
    sides   : 配餐 / 凑单品候选
    combos  : 套餐候选（每个套餐是一个 Item 列表）
    coupons : 可用券
    price_fn: 计价函数，签名 price_fn(plan) -> PricedPlan。
              **必须**由调用方封装 MCP 的 calculate-price，禁止本地估算。
    budget  : 预算上限（可选）
    top_k   : 返回的推荐方案数量

    返回
    ----
    {
      "recommended": [PricedPlan, ...],
      "warnings":    [ {...}, ... ],
      "best":        PricedPlan | None,
    }
    """

    base = build_base_plans(mains, sides, combos)
    with_coupons = attach_coupons(base, coupons)
    fillers = build_filler_plans(mains, sides, coupons)
    candidates = dedupe_plans([*with_coupons, *fillers])

    # 逐方案真实计价（由 price_fn 调用 MCP calculate-price）
    priced: list[PricedPlan] = [price_fn(p) for p in candidates]

    ranked = rank(priced)
    if budget is not None:
        ranked = within_budget(priced, budget)
    else:
        ranked = ranked[:top_k]

    return {
        "recommended": ranked[:top_k],
        "warnings": detect_filler_trap(priced),
        "best": ranked[0] if ranked else None,
    }


def render(result: dict) -> str:
    """把 recommend() 的结果渲染成面向用户的文本。"""

    lines: list[str] = []
    rec = result.get("recommended") or []

    if rec:
        best = rec[0]
        lines.append("推荐方案")
        lines.append(f"  组合：{best.label}")
        # 优惠为 0 时不显示，避免「优惠 0.0 元」这种噪音
        if best.discount > 0:
            lines.append(f"  实付 {best.payable} 元，优惠 {best.discount} 元")
        else:
            lines.append(f"  实付 {best.payable} 元")
        if best.delivery_fee:
            lines.append(f"  （含配送费 {best.delivery_fee} 元）")
        lines.append("")

    if len(rec) > 1:
        lines.append("备选方案")
        for alt in rec[1:]:
            lines.append(f"  · {alt.label} —— 实付 {alt.payable} 元")
        lines.append("")

    warnings = result.get("warnings") or []
    if warnings:
        lines.append("需要提醒你")
        for w in warnings[:3]:
            lines.append(f"  {w['message']}")
        lines.append("")

    if not rec:
        lines.append("当前条件下没有可用方案，请检查门店或券信息。")

    return "\n".join(lines).rstrip()


__all__ = [
    "Item",
    "Coupon",
    "Plan",
    "PricedPlan",
    "build_base_plans",
    "attach_coupons",
    "build_filler_plans",
    "dedupe_plans",
    "rank",
    "within_budget",
    "detect_filler_trap",
    "recommend",
    "render",
]
