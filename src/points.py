"""积分体检 —— 积分也是一种有保质期的钱。

麦当劳的积分（麦享会积分）不是「攒够就换」那么简单。有三件事与
`auditor.py` 里的「券的沉默成本」同构，但更容易被忽略：

1. **积分会过期。** 实测账户里已经过期过 17 积分 —— 那是真实发生的损失，
   而且用户通常毫无察觉。
2. **`status` 不能单独作为「可兑换」的依据。** 实测 24 个兑换品里有 15 个
   兑换截止日已过，但 `status` 仍报「上架」。只看 status 会告诉用户
   「你有 24 个可以换」，而实际只有 9 个。
3. **「能兑换」不等于「划算」。** 兑换来的是一张**券**，核销时还要再付钱
   （`extPrice`），而且那张券你可能根本用不上。

## 一件我们不做的事：不估算「积分值多少钱」

积分没有固定汇率 —— 它值多少，取决于你换的那张券能省多少；
而「券能省多少」要靠带券计价（`calculate-price`）实测，不是算出来的。

所以积分体检**只给事实**：余额、有效期、所需积分、能不能换、够不够。
不给一个看起来很合理的「约等于 ¥X」。宁可少报，不可编造。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional, Sequence

_RULE = "=" * 46


def _fmt_num(value: float) -> str:
    """积分数字的可读格式：整数不显示小数点（`50` 而不是 `50.0`）。

    积分本身可以是小数（实测可用积分 376.3），所以不能一律按整数输出。
    """
    return str(int(value)) if float(value).is_integer() else str(value)


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PointsAccount:
    """积分账户。所有字段单位均为「积分」，**可以是小数**。"""

    available: float = 0.0  # 可用积分
    accumulative: float = 0.0  # 累计获得
    used: float = 0.0  # 已使用
    expired: float = 0.0  # **已过期** —— 真实发生过的损失
    expiring_this_month: float = 0.0  # 本月将过期
    expiring_next_month: float = 0.0  # 下月将过期
    frozen: float = 0.0  # 冻结中

    @property
    def at_risk(self) -> float:
        """本月 + 下月将要过期的积分合计。"""
        return round(self.expiring_this_month + self.expiring_next_month, 2)


@dataclass(frozen=True)
class RedeemableItem:
    """一个可兑换的积分商品。"""

    spu_id: int
    name: str
    points: float
    deadline: Optional[date] = None  # 兑换截止日（downTime）
    status: str = ""  # 接口原始状态

    def days_left(self, today: Optional[date] = None) -> Optional[int]:
        """距兑换截止还有几天；无截止日返回 None。"""
        if self.deadline is None:
            return None
        return (self.deadline - (today or date.today())).days

    def is_open(self, today: Optional[date] = None) -> bool:
        """是否仍在可兑换期内。

        **按 `downTime` 判断，不看 `status`** —— 实测两者会不一致，
        以截止日期为准才是对的。
        """
        left = self.days_left(today)
        return left is None or left >= 0


# --------------------------------------------------------------------------
# 体检
# --------------------------------------------------------------------------


def audit_points(
    account: PointsAccount,
    items: Sequence[RedeemableItem],
    today: Optional[date] = None,
) -> dict:
    """对积分与可兑换清单做一次体检。

    返回：
    {
      "account":     PointsAccount,
      "open":        [...],  # 仍在兑换期内（按 downTime 判断）
      "stale":       [...],  # status 报「上架」但已过 downTime
      "affordable":  [...],  # 积分够，能立刻换（按所需积分升序）
      "short_of":    [...],  # 积分不够（按还差多少升序）
      "duplicates":  [...],  # 同名但所需积分不同 —— 该换便宜的那个
      "narrative":   str,
    }
    """

    open_items = [it for it in items if it.is_open(today)]
    stale = [it for it in items if not it.is_open(today)]

    affordable = sorted(
        [it for it in open_items if it.points <= account.available],
        key=lambda i: i.points,
    )
    short_of = sorted(
        [it for it in open_items if it.points > account.available],
        key=lambda i: i.points,
    )

    return {
        "account": account,
        "open": sorted(open_items, key=lambda i: i.points),
        "stale": stale,
        "affordable": affordable,
        "short_of": short_of,
        "duplicates": _find_duplicates(open_items),
        "narrative": _narrative(account, open_items, stale, affordable),
    }


def _find_duplicates(items: Sequence[RedeemableItem]) -> list[dict]:
    """找出**同名但所需积分不同**的兑换品。

    实测确实存在：「10.9元指定小食任选」有两个 SPU，一个要 200 积分、
    一个只要 100 积分 —— 换贵的那个等于白扔 100 积分。
    """
    by_name: dict[str, list[RedeemableItem]] = {}
    for it in items:
        by_name.setdefault(it.name, []).append(it)

    found: list[dict] = []
    for name, group in by_name.items():
        if len(group) < 2:
            continue
        cheapest = min(group, key=lambda i: i.points)
        dearest = max(group, key=lambda i: i.points)
        if dearest.points > cheapest.points:
            found.append(
                {
                    "name": name,
                    "cheapest": cheapest,
                    "dearest": dearest,
                    "waste": round(dearest.points - cheapest.points, 2),
                }
            )
    found.sort(key=lambda d: -d["waste"])
    return found


def _narrative(
    account: PointsAccount,
    open_items: list[RedeemableItem],
    stale: list[RedeemableItem],
    affordable: list[RedeemableItem],
) -> str:
    """生成一句话结论。"""
    parts = [
        f"可用 {_fmt_num(account.available)} 积分，"
        f"能兑换的 {len(open_items)} 个商品里有 {len(affordable)} 个换得起。"
    ]
    if account.expired > 0:
        parts.append(
            f"⚠️ 已经过期过 {_fmt_num(account.expired)} 积分 —— 那是真实损失。"
        )
    if account.at_risk > 0:
        parts.append(
            f"另有 {_fmt_num(account.at_risk)} 积分在本月/下月到期，尽快用掉。"
        )
    if stale:
        parts.append(
            f"被过滤掉 {len(stale)} 个已过兑换截止日的商品"
            f"（它们的状态仍显示「上架」）。"
        )
    return "".join(parts)


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def render_points(report: dict) -> str:
    """把积分体检结果渲染成面向用户的文本。"""
    account: PointsAccount = report["account"]
    open_items: list[RedeemableItem] = report.get("open") or []
    stale: list[RedeemableItem] = report.get("stale") or []
    affordable: list[RedeemableItem] = report.get("affordable") or []
    short_of: list[RedeemableItem] = report.get("short_of") or []
    duplicates: list[dict] = report.get("duplicates") or []

    lines: list[str] = [_RULE, "积分体检", _RULE, ""]

    lines.append(
        f"  可用 {_fmt_num(account.available)} 积分 · "
        f"累计 {_fmt_num(account.accumulative)} · 已用 {_fmt_num(account.used)}"
    )
    if account.expired > 0:
        lines.append(
            f"  ⚠️ 已过期 {_fmt_num(account.expired)} 积分 —— 已发生的损失"
        )
    if account.at_risk > 0:
        lines.append(
            f"  ⚠️ 另有 {_fmt_num(account.at_risk)} 积分将在本月/下月到期"
        )
    lines.append("")

    if not open_items:
        lines.append("  当前没有仍在兑换期内的积分商品。")
        lines += [_RULE, _no_item_conclusion(stale)]
        return "\n".join(lines).rstrip()

    total = len(open_items) + len(stale)
    summary = (
        f"  积分商城 {total} 个商品：{len(open_items)} 个仍在兑换期内"
        f"（其中 {len(affordable)} 个换得起）"
    )
    if stale:
        summary += f"，{len(stale)} 个已过兑换期"
    lines.append(summary)
    lines.append("")

    if affordable:
        lines.append("  【现在就能换】")
        for item in affordable:
            lines.append(
                f"    · {item.name}    需 {_fmt_num(item.points)} 积分"
                f"    {_deadline_text(item)}"
            )
        lines.append("")

    if short_of:
        lines.append(f"  【积分还不够】{len(short_of)} 个")
        for item in short_of[:3]:
            gap = _fmt_num(round(item.points - account.available, 2))
            lines.append(
                f"    · {item.name}    需 {_fmt_num(item.points)} 积分，还差 {gap}"
            )
        if len(short_of) > 3:
            lines.append(f"    …… 另有 {len(short_of) - 3} 个")
        lines.append("")

    if duplicates:
        lines.append("  【同款不同价 · 换便宜的那个】")
        for dup in duplicates:
            lines.append(
                f"    · {dup['name']} —— 一个要 {_fmt_num(dup['dearest'].points)} 积分，"
                f"一个只要 {_fmt_num(dup['cheapest'].points)} 积分，"
                f"差 {_fmt_num(dup['waste'])}"
            )
        lines.append("")

    if stale:
        lines.append(f"  【已过兑换期】{len(stale)} 个")
        lines.append(
            "    这些商品的接口状态仍标着「上架」，但兑换截止日已经过去 —— "
            "只看状态会以为还能换。"
        )
        lines.append("")

    lines += [_RULE, _conclusion(report)]
    return "\n".join(lines).rstrip()


def _deadline_text(item: RedeemableItem) -> str:
    """兑换截止日的可读描述。"""
    left = item.days_left()
    if left is None:
        return "无截止日"
    return f"还剩 {left} 天"


def _no_item_conclusion(stale: list[RedeemableItem]) -> str:
    """没有任何在兑换期内的商品时的结论文案。"""
    if stale:
        return (
            f"{len(stale)} 个积分商品都已过兑换截止日 —— "
            f"接口的 status 仍显示「上架」，不能只看它。"
        )
    return "积分商城当前没有返回任何商品。"


def _conclusion(report: dict) -> str:
    """生成结尾建议（可能多行）。"""
    account: PointsAccount = report["account"]
    lines: list[str] = []

    if account.expired > 0 or account.at_risk > 0:
        lines.append(
            "积分和券一样会过期 —— 已经过期的部分是真金白银的损失，"
            "别让剩下的也走这条路。"
        )

    lines.append(
        "但「能换」不等于「划算」：兑换来的是一张券，核销时还要再付钱，"
        "而且那张券你可能用不上。"
    )
    lines.append(
        "至于积分到底值多少钱 —— 取决于你换的那张券能省多少，"
        "那要靠带券计价实测。我们不猜。"
    )
    return "\n".join(lines)


__all__ = [
    "PointsAccount",
    "RedeemableItem",
    "audit_points",
    "render_points",
]
