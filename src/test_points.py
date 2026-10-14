"""积分体检的自测 —— 用真实 MCP 响应片段。

覆盖三件事：

1. 积分账户与兑换清单的解析（字符串、小数、截止日）
2. **可兑换性必须按 `downTime` 判断，不能只看 `status`** —— 这是本模块的立身之本
3. 「同款不同积分」的检测，以及一条硬约束：**不估算积分值多少钱**

运行：
    python src/test_points.py
"""

import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcd_adapter import parse_points_account, parse_points_products  # noqa: E402
from points import (  # noqa: E402
    PointsAccount,
    RedeemableItem,
    audit_points,
    render_points,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fixtures")
# 积分数据的采集日（与主链路的 fixture 不是同一批）
TODAY = date(2026, 10, 14)


def load(name: str) -> dict:
    """读取 fixtures/ 下的一个 JSON 文件。"""
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


def check_parsing() -> tuple:
    """校验账户与兑换清单的解析。"""
    print("=" * 60)
    print("1. 解析积分账户与兑换清单")
    print("=" * 60)

    account = parse_points_account(load("points_account.json"))
    print(f"   可用 {account.available} · 累计 {account.accumulative}"
          f" · 已用 {account.used} · 已过期 {account.expired}")

    # 积分字段是字符串且可以有小数，不能按整数解析
    assert account.available == 376.3, "可用积分应解析出小数 376.3"
    assert isinstance(account.available, float)
    assert account.expired == 17.0, "已过期积分应为 17"
    assert account.at_risk == 0.0, "本月/下月到期为 0"

    items = parse_points_products(load("points_products.json"))
    assert len(items) == 24, f"应解析出 24 个商品，实际 {len(items)}"
    first = next(i for i in items if i.spu_id == 17081)
    assert first.name == "21.9元巨无霸可乐组合"
    assert first.points == 800.0
    assert first.deadline == date(2026, 12, 30), "应从 downTime 解析出截止日"
    print(f"   兑换清单 {len(items)} 个；样例：{first.name} 需 {first.points} 积分"
          f"，截止 {first.deadline}")
    print("   [PASS] 字符串/小数积分与截止日均解析正确")
    print()
    return account, items


def check_open_filter(account, items) -> dict:
    """校验「按 downTime 过滤，而不是按 status」。"""
    print("=" * 60)
    print("2. 可兑换性按 downTime 判断（不看 status）")
    print("=" * 60)

    report = audit_points(account, items, today=TODAY)
    stale = report["stale"]

    assert len(report["open"]) == 9, f"应有 9 个仍在兑换期内，实际 {len(report['open'])}"
    assert len(stale) == 15, f"应有 15 个已过兑换期，实际 {len(stale)}"

    # ★ 关键：被过滤掉的这些，接口状态全都还是「上架」(2)
    assert all(it.status == "2" for it in stale), \
        "被过滤的商品状态应仍是「上架」—— 这正是不能只看 status 的原因"
    print(f"   仍在兑换期 {len(report['open'])} 个 / 已过兑换期 {len(stale)} 个")
    print(f"   被过滤的这 {len(stale)} 个，接口状态全是 status=2（上架）")
    print("   [PASS] 只看 status 会误报「24 个可兑换」，按 downTime 才是 9 个")

    # 恰好卡在截止日当天应算「仍可兑换」
    edge = RedeemableItem(spu_id=1, name="当天到期", points=10.0, deadline=TODAY)
    assert edge.is_open(TODAY) is True, "截止日当天应仍可兑换"
    assert edge.days_left(TODAY) == 0
    print("   [PASS] 截止日当天算「仍可兑换」")
    print()
    return report


def check_affordable(report, account) -> None:
    """校验积分够不够的分类。"""
    print("=" * 60)
    print("3. 积分够不够")
    print("=" * 60)

    affordable = report["affordable"]
    short_of = report["short_of"]

    assert len(affordable) + len(short_of) == len(report["open"]), \
        "「换得起 + 换不起」应等于在兑换期内的总数"
    assert all(it.points <= account.available for it in affordable)
    assert all(it.points > account.available for it in short_of)
    assert affordable == sorted(affordable, key=lambda i: i.points), "换得起的应按所需积分升序"

    print(f"   可用 {account.available} 积分 → 换得起 {len(affordable)} 个、"
          f"还差积分 {len(short_of)} 个")
    print(f"   最便宜的：{affordable[0].name} 需 {affordable[0].points} 积分")
    print("   [PASS] 分类正确且排序稳定")
    print()


def check_duplicates(report) -> None:
    """校验「同款不同积分」的检测。"""
    print("=" * 60)
    print("4. 同款不同价")
    print("=" * 60)

    dups = report["duplicates"]
    assert len(dups) >= 1, "真实数据里有同名但所需积分不同的商品，应当被检出"

    dup = dups[0]
    assert dup["name"] == "10.9元指定小食任选"
    assert dup["dearest"].points == 200.0 and dup["cheapest"].points == 100.0
    assert dup["waste"] == 100.0, "换贵的那一个等于白扔 100 积分"
    print(f"   {dup['name']}：{dup['dearest'].points} vs {dup['cheapest'].points} 积分"
          f"，差 {dup['waste']}")
    print("   [PASS] 检出同名但积分不同的兑换品")
    print()


def check_rendering(report) -> None:
    """校验渲染内容与「不估算积分价值」这条硬约束。"""
    print("=" * 60)
    print("5. 渲染与边界")
    print("=" * 60)

    text = render_points(report)
    print(text)
    print()

    for key in ("积分体检", "已过期 17", "同款不同价", "15 个已过兑换期", "我们不猜"):
        assert key in text, f"渲染结果缺少「{key}」"

    # ★ 硬约束：积分没有固定汇率，我们不给「约等于多少钱」这类估算
    assert "¥" not in text, "渲染结果不应出现金额符号 —— 积分价值我们不估算"
    assert "约等于" not in text and "相当于" not in text, "不应把积分折算成金额"
    print("   [PASS] 渲染不含任何金额估算（积分值多少钱我们不猜）")
    print()

    # 边界：全部已过兑换期
    all_stale = [
        RedeemableItem(spu_id=i, name=f"过期品{i}", points=10.0,
                       deadline=TODAY - timedelta(days=1), status="2")
        for i in range(3)
    ]
    r2 = audit_points(PointsAccount(available=100.0), all_stale, today=TODAY)
    assert r2["open"] == [] and len(r2["stale"]) == 3
    t2 = render_points(r2)
    assert "当前没有仍在兑换期内的积分商品" in t2, "全部过期时应有明确说明"
    assert "不能只看它" in t2, "应提醒「状态仍显示上架」"
    print("   全部过期 → 明确说明，且点出「状态仍显示上架」")

    # 边界：空清单
    r3 = audit_points(PointsAccount(), [], today=TODAY)
    t3 = render_points(r3)
    assert "当前没有仍在兑换期内的积分商品" in t3
    print("   空清单 → 不崩溃，给出明确说明")

    # 边界：无截止日的商品视为长期有效
    no_deadline = RedeemableItem(spu_id=9, name="长期有效", points=5.0)
    assert no_deadline.is_open(TODAY) is True
    assert no_deadline.days_left(TODAY) is None
    print("   无截止日 → 视为长期有效")
    print("   [PASS] 边界处理正确")
    print()


def main() -> None:
    """依次跑完全部用例。"""
    account, items = check_parsing()
    report = check_open_filter(account, items)
    check_affordable(report, account)
    check_duplicates(report)
    check_rendering(report)

    print("=" * 60)
    print("全部积分测试通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
