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
    CouponStock,
    OrderRecord,
    audit_report,
    consumption_profile,
    coupon_silent_cost,
    render_audit,
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
    print("5. 空订单处理")
    print("=" * 60)
    empty = consumption_profile([])
    print(f"   {empty['insights'][0]}")
    assert empty["order_count"] == 0 and empty["total_spent"] == 0.0
    print("   [PASS] 空订单不崩溃，友好提示")
    print()

    print("=" * 60)
    print("6. 完整审计报告渲染")
    print("=" * 60)
    report = audit_report(orders, coupons, months=1, today=TODAY)
    print(render_audit(report))
    print()

    assert report["coupons"]["at_risk_count"] == 3
    assert report["profile"]["order_count"] == 4
    print("[PASS] 审计报告生成正确")
    print()
    print("=" * 60)
    print("全部审计模块测试通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
