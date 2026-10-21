"""auditor.py 的自测脚本。

重点验证：沉默成本的日期判定、临界值处理、以及消费画像的洞察生成。

运行：
    python src/test_auditor.py
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from auditor import (  # noqa: E402
    MIN_ORDERS_PER_BUCKET,
    CouponStock,
    OrderRecord,
    audit_report,
    consumption_profile,
    count_unusable_days,
    coupon_silent_cost,
    render_audit,
    time_of_day,
)


TODAY = date(2026, 10, 10)


def main() -> None:
    """跑一遍审计模块的全部用例并打印报告。"""
    print("=" * 60)
    print("1. 券的沉默成本 —— 基础计算")
    print("=" * 60)

    coupons = [
        CouponStock("c1", "免费脆薯饼", 8.0, TODAY + timedelta(days=2)),
        CouponStock("c2", "9.9元中杯冰美式", 13.1, TODAY + timedelta(days=5)),
        CouponStock("c3", "中薯条买一送一", 14.5, TODAY + timedelta(days=30)),
        CouponStock("c4", "满50减12", 12.0, TODAY + timedelta(days=1), threshold=50.0),
    ]

    result = coupon_silent_cost(coupons, today=TODAY, expiring_within=7)
    print(f"   总面值      : {result['total_value']} 元")
    print(f"   风险面值    : {result['at_risk_value']} 元")
    print(f"   风险张数    : {result['at_risk_count']} 张")
    print(f"   结论        : {result['narrative']}")

    assert result["total_value"] == 47.6, "总面值应为 8+13.1+14.5+12 = 47.6"
    assert result["at_risk_count"] == 3, "应有 3 张在 7 天内过期（c1/c2/c4）"
    assert result["at_risk_value"] == 33.1, "风险面值应为 8+13.1+12 = 33.1"

    # 排序：最紧迫的（1 天）应排最前
    assert result["at_risk_items"][0]["days_left"] == 1, "应优先展示最先过期的券"
    print("   [PASS] 沉默成本计算与紧迫度排序正确")
    print()

    print("=" * 60)
    print("2. 临界值：今天到期 / 已过期")
    print("=" * 60)

    edge = [
        CouponStock("e1", "今天到期券", 10.0, TODAY),
        CouponStock("e2", "昨天过期券", 5.0, TODAY - timedelta(days=1)),
    ]
    r2 = coupon_silent_cost(edge, today=TODAY)
    print(f"   {r2['narrative']}")
    assert r2["at_risk_count"] == 2, "今天到期与已过期都应计入风险"
    assert r2["at_risk_items"][0]["days_left"] == -1, "已过期的应排最前"

    # 已过期优先于今天到期的叙述，且两者必须被区分开
    assert "过期" in r2["narrative"], "存在已过期券时应如实说明"
    assert "今天到期" in r2["narrative"], "今天到期的券应单独强调，不能被归入已过期"
    print("   [PASS] 今天到期与已过期正确区分，未被错误合并")
    print()

    print("=" * 60)
    print("3. 边界：无券 / 无临近过期券")
    print("=" * 60)

    r3 = coupon_silent_cost([], today=TODAY)
    print(f"   空券: {r3['narrative']}")
    assert r3["at_risk_count"] == 0 and r3["total_value"] == 0.0

    safe = [CouponStock("s1", "远处的券", 10.0, TODAY + timedelta(days=60))]
    r4 = coupon_silent_cost(safe, today=TODAY)
    print(f"   安全: {r4['narrative']}")
    assert r4["at_risk_count"] == 0, "60 天后过期不应计入风险"
    print("   [PASS] 边界情况处理正确，无除零/空指针")
    print()

    print("=" * 60)
    print("4. 消费画像")
    print("=" * 60)

    orders = [
        OrderRecord(TODAY - timedelta(days=1), 46.5,
                    ["麦辣鸡腿汉堡", "中薯条", "可乐"], "到店"),
        OrderRecord(TODAY - timedelta(days=3), 33.5,
                    ["麦辣鸡腿汉堡", "中薯条", "可乐"], "到店"),
        OrderRecord(TODAY - timedelta(days=5), 42.0,
                    ["麦辣鸡腿汉堡", "可乐"], "外送"),
        OrderRecord(TODAY - timedelta(days=8), 22.5,
                    ["麦辣鸡腿汉堡"], "到店"),
    ]

    profile = consumption_profile(orders, months=1)
    top = profile["top_items"][0]
    print(f"   订单数  : {profile['order_count']}")
    print(f"   总支出  : {profile['total_spent']} 元")
    print(f"   客单价  : {profile['avg_per_order']} 元")
    print(f"   高频餐品: {top['name']} x{top['times']}")
    print("   洞察：")
    for ins in profile["insights"]:
        print(f"     · {ins}")

    assert profile["order_count"] == 4
    assert profile["total_spent"] == 144.5, "总额应为 46.5+33.5+42+22.5 = 144.5"
    assert profile["top_items"][0]["name"] == "麦辣鸡腿汉堡"
    assert profile["top_items"][0]["times"] == 4
    assert len(profile["insights"]) >= 2, "应产出多条可执行洞察"
    # 高频单品应触发「改套餐」建议
    assert any("套餐" in i for i in profile["insights"]), "高频单品应产出改套餐建议"
    print("   [PASS] 消费画像计算与洞察生成正确")
    print()

    print("=" * 60)
    print("5. 口径分离（0 元订单与部分退款）")
    print("=" * 60)

    # 真实数据里就有这两类：0 元订单（券核销）与「部分退款」
    mixed = [
        OrderRecord(TODAY - timedelta(days=1), 36.5, ["猪柳蛋麦满分组合"], "到店",
                    "部分退款"),
        OrderRecord(TODAY - timedelta(days=2), 23.8, ["麦辣鸡翅"], "到店",
                    "订单已完成"),
        OrderRecord(TODAY - timedelta(days=3), 0.0, ["免费脆薯饼"], "到店",
                    "订单已完成"),
        OrderRecord(TODAY - timedelta(days=4), 0.0, ["免费小薯条"], "到店",
                    "订单已完成"),
    ]
    p2 = consumption_profile(mixed, months=1)

    print(f"   订单数      : {p2['order_count']} 单"
          f"（消费性 {p2['spending_order_count']} / 0 元 {p2['free_order_count']}）")
    print(f"   总支出      : {p2['total_spent']} 元")
    print(f"   客单价      : {p2['avg_per_order']} 元  ← 消费口径（算钱用）")
    print(f"   全部订单均值: {p2['avg_all_orders']} 元  ← 仅作对照")
    print("   洞察：")
    for ins in p2["insights"]:
        print(f"     · {ins}")

    assert OrderRecord(TODAY, 0.0).is_spending is False, "0 元订单不是消费"
    assert OrderRecord(TODAY, 1.0).is_spending is True, "1 元订单也是消费"

    assert p2["order_count"] == 4, "点单口径应包含 0 元订单"
    assert p2["spending_order_count"] == 2, "消费口径只算有实际支出的订单"
    assert p2["free_order_count"] == 2
    assert p2["partial_refund_count"] == 1
    assert p2["total_spent"] == 60.3, "0 元订单不影响总额"
    assert abs(p2["avg_per_order"] - 30.15) < 0.02, \
        f"客单价应为 60.3/2=30.15（不按 4 单摊薄），实际 {p2['avg_per_order']}"
    assert abs(p2["avg_all_orders"] - 15.08) < 0.02, "对照口径按全部订单算"

    # 关键：0 元单与部分退款都必须被如实交代，不能静默
    assert any("0 元订单" in i for i in p2["insights"]), "应说明 0 元订单的口径"
    refund_line = next(i for i in p2["insights"] if "部分退款" in i)
    assert "不返回退款金额" in refund_line, \
        "部分退款必须说明「接口没给金额」，而不是猜一个数出来"
    assert "可能略偏高" in refund_line, "应给出方向性提示"
    print("   [PASS] 两个口径分离，0 元单与部分退款都被如实交代（未猜金额）")
    print()

    print("=" * 60)
    print("6. 时段口径（样本不足就不给数字）")
    print("=" * 60)

    # 菜单是分时段供应的（早餐卖麦满分、正餐卖汉堡），
    # 所以「几点点的」和「花了多少」一样，都是消费结构的一部分。
    by_time = [
        OrderRecord(TODAY - timedelta(days=1), 36.5, ["早餐组合"], "到店",
                    "订单已完成", 9),   # 早餐
        OrderRecord(TODAY - timedelta(days=2), 22.0, ["猪柳蛋麦满分"], "到店",
                    "订单已完成", 8),   # 早餐
        OrderRecord(TODAY - timedelta(days=3), 25.0, ["巨无霸"], "到店",
                    "订单已完成", 12),  # 午市
        OrderRecord(TODAY - timedelta(days=4), 30.0, ["麦辣鸡腿堡"], "到店",
                    "订单已完成", 13),  # 午市
        OrderRecord(TODAY - timedelta(days=5), 28.0, ["双层吉士汉堡"], "到店",
                    "订单已完成", 12),  # 午市
        OrderRecord(TODAY - timedelta(days=6), 9.0, ["冰美式"], "到店",
                    "订单已完成", 15),  # 下午（只有 1 单）
    ]
    p3 = consumption_profile(by_time, months=1)
    buckets = {b["name"]: b for b in p3["time_buckets"]}

    print("   时段分布：")
    for b in p3["time_buckets"]:
        tail = f"客单价 {b['avg']} 元" if b["reliable"] else "样本不足，未估算"
        print(f"     {b['name']} · {b['orders']} 单 · 支出 {b['total']} 元 · {tail}")

    assert set(buckets) == {"早餐", "午市", "下午"}, "应按真实时段分桶"
    assert [b["name"] for b in p3["time_buckets"]] == ["早餐", "午市", "下午"], \
        "时段应按时间顺序输出（早→晚），而不是按单数排序"

    # 核心契约：不到门槛的时段**拒绝给客单价**。宁可说样本不足，
    # 也不给一个看起来很精确的假数字。
    assert buckets["早餐"]["avg"] is None, "早餐只有 2 笔支出，不得给出客单价"
    assert buckets["早餐"]["reliable"] is False
    assert buckets["下午"]["avg"] is None, "下午只有 1 笔支出，不得给出客单价"
    assert buckets["早餐"]["orders"] == 2, "但单数仍要如实报出来"
    assert MIN_ORDERS_PER_BUCKET == 3, "门槛值变更时请同步本用例的预期"

    # 刚好达到门槛的时段才给数字
    assert buckets["午市"]["reliable"] is True
    mid_avg = buckets["午市"]["avg"]
    assert mid_avg == 27.67, f"午市客单价应为 (25+30+28)/3=27.67，实际 {mid_avg}"

    # 主力时段：3/6 = 50%（≥40%）且单数 ≥ 门槛
    assert p3["main_bucket"] == "午市", f"主力时段应为午市，实际 {p3['main_bucket']}"

    # 存在样本不足的时段时，必须点出「全局客单价是混合口径」
    assert any("口径提醒" in i for i in p3["insights"]), \
        "有样本不足的时段时，必须提示全局客单价是混合口径"
    print("   [PASS] 样本不足的时段拒绝给客单价，并点出混合口径的风险")
    print()

    # 小时未知的订单不参与分桶，也不凭空归到某一类
    p4 = consumption_profile([
        OrderRecord(TODAY, 20.0, ["巨无霸"], "到店", "订单已完成", 12),
        OrderRecord(TODAY - timedelta(days=1), 20.0, ["巨无霸"], "到店"),
    ], months=1)
    assert len(p4["time_buckets"]) == 1, "小时未知的订单不应凭空归类"
    assert p4["time_buckets"][0]["orders"] == 1, "只有带小时的订单参与分桶"
    assert p4["main_bucket"] is None, "单数不足门槛时不得给出主力时段"
    print("   [PASS] 小时未知的订单不参与分桶，也不凭空归类")
    print()

    # 时段定义：含跨零点的夜宵
    assert time_of_day(9) == "早餐" and time_of_day(20) == "晚市"
    assert time_of_day(23) == "夜宵" and time_of_day(2) == "夜宵"
    assert time_of_day(None) is None, "小时未知时时段也未知"
    print("   [PASS] 时段边界正确（含跨零点的夜宵）")
    print()

    print("=" * 60)
    print("7. 空订单处理")
    print("=" * 60)
    empty = consumption_profile([])
    print(f"   {empty['insights'][0]}")
    assert empty["order_count"] == 0 and empty["total_spent"] == 0.0
    print("   [PASS] 空订单不崩溃，友好提示")
    print()

    print("=" * 60)
    print("8. 完整审计报告渲染")
    print("=" * 60)
    report = audit_report(orders, coupons, months=1, today=TODAY)
    print(render_audit(report))
    print()

    assert report["coupons"]["at_risk_count"] == 3
    assert report["profile"]["order_count"] == 4
    print("[PASS] 审计报告生成正确")
    print()
    print("=" * 60)
    print("9. 券的时间限制（星期限制 + 生效日）")
    print("=" * 60)

    # 跨日券 + 工作日限制：周五看来「还剩 2 天」，但那两天是周末，券不生效。
    # 只报天数会让用户按剩余天数安排，最后发现用不了。
    fri = date(2026, 10, 16)
    weekday_only = CouponStock(
        "W1", "工作日券", 10.0, date(2026, 10, 18),
        weekdays=frozenset({0, 1, 2, 3, 4}), weekday_text="周一至周五",
        valid_from=date(2026, 10, 9),
    )
    assert count_unusable_days(weekday_only, fri) == 2, \
        "周六、周日两天券不生效，应数出 2 天"
    assert weekday_only.is_usable_on(fri) is True
    assert weekday_only.is_usable_on(date(2026, 10, 17)) is False, "周六用不了"

    # 单日券今天还没生效 —— 那是「没开始」，不是「不生效」。
    # 混为一谈会报出「剩余期里有 1 天用不了」，而实际只有那 1 天能用。
    single = CouponStock(
        "W2", "单日早餐券", 9.0, date(2026, 10, 15),
        weekdays=frozenset({3}), weekday_text="周四",
        valid_from=date(2026, 10, 15), day_window="05:00-10:29",
    )
    assert count_unusable_days(single, date(2026, 10, 14)) == 0, \
        "今天不在券的有效期内 ≠ 券不生效，不能算作「用不了」"
    assert single.is_usable_on(date(2026, 10, 15)) is True

    # 没有星期限制：返回 None（不限），不能与 False（不能）混淆
    free = CouponStock("W3", "不限星期的券", 5.0, date(2026, 10, 18))
    assert free.is_usable_on(fri) is None, "无星期限制应返回 None，而不是 False"
    assert count_unusable_days(free, fri) == 0
    print("   [PASS] 周末不生效的天数被数出来；单日券未生效不算「用不了」")
    print("   [PASS] 无星期限制返回 None（不限），不与 False（不能）混淆")
    print()

    print("=" * 60)
    print("全部审计模块测试通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
