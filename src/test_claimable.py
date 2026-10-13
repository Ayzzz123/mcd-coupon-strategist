"""「可领取的券」的解析与净价值预判自测。

覆盖两块：

1. `available-coupons` 的响应解析 —— 该工具返回的是 **Markdown 文本而非 JSON**
   （实测 2026-10-13），所以解析器必须按行取字段，并且对没见过的状态保持谨慎。
2. 未领取券的净价值预判与渲染口径 —— 这批券**没有有效期**，
   渲染时不得把「未知」说成「无期限」，更不能输出剩余天数。

运行：
    python src/test_claimable.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcd_adapter import (  # noqa: E402
    coupon_value_and_cost,
    parse_available_coupons,
    parse_claimable_coupons,
)
from planner import (  # noqa: E402
    CATEGORY_DIRECT,
    CATEGORY_UNKNOWN,
    audit_claimable,
    render_claimable,
)

# 真实响应的整理版（2026-10-13 采集）：7 条记录，4 条已领取、3 条可领取
REAL_AVAILABLE_TEXT = """### 麦麦省优惠券列表：
- 优惠券标题：中薯条买一送一 \\
  状态：已领取 \\
  优惠券图片：\\
    <img src="https://img.mcd.cn/x1.png" alt="中薯条买一送一" width="300">
- 优惠券标题：王牌小食拼 \\
  状态：已领取 \\
- 优惠券标题：巧克力味厚松饼猪柳蛋套餐 \\
  状态：已领取 \\
- 优惠券标题：巧克力味厚松饼猪柳蛋套餐 \\
  状态：已领取 \\
- 优惠券标题：免费脆薯饼 \\
  状态：可领取 \\
- 优惠券标题：人气麦旋风买一送一 \\
  状态：可领取 \\
- 优惠券标题：9.9元中杯冰美式 \\
  状态：可领取 \\
"""

# 覆盖到其中一张券对应商品的菜单价格表（均为真实菜单价）
MENU_WITH_TARGETS = {
    "薯条": 14.5,
    "脆薯饼": 8.0,
    "经典麦旋风": 14.5,
    "麦咖啡™美式": 11.9,
}

# 未领取券渲染结果里**不允许**出现的文案 —— 它们的有效期是未知的
FORBIDDEN_EXPIRY_WORDS = ("还剩", "无期限", "今天到期", "已过期")


def check_parsing() -> list:
    """校验文本响应的解析与状态判读。"""
    print("=" * 60)
    print("1. 解析 available-coupons 的 Markdown 文本")
    print("=" * 60)

    coupons = parse_available_coupons(REAL_AVAILABLE_TEXT)
    for c in coupons:
        print(f"   {c.name:<22} claimed={c.claimed}")

    assert len(coupons) == 7, f"应解析出 7 条，实际 {len(coupons)}"
    assert sum(1 for c in coupons if c.claimed is True) == 4, "应有 4 条已领取"
    assert sum(1 for c in coupons if c.claimed is False) == 3, "应有 3 条可领取"
    print("   [PASS] 7 条记录解析正确（4 已领取 / 3 可领取）")
    print()

    print("=" * 60)
    print("2. 解析容错（未知状态、缺状态、空输入）")
    print("=" * 60)

    # 没见过的状态：必须判为「无法识别」，不能想当然当成可领取
    weird = "- 优惠券标题：神秘券 \\\n  状态：已核销 \\\n"
    result = parse_available_coupons(weird)
    assert len(result) == 1 and result[0].claimed is None, \
        "未识别的状态应为 None，而不是 False（否则会误报成可领取）"
    print("   状态「已核销」→ claimed=None（不误报为可领取）")

    # 缺状态：如实收下但不猜
    no_status = "- 优惠券标题：缺状态券 \\\n- 优惠券标题：另一张 \\\n  状态：可领取 \\\n"
    result = parse_available_coupons(no_status)
    assert result[0].claimed is None, "缺状态的券应如实收下且 claimed=None"
    assert result[1].claimed is False, "后续券不应受前一张缺状态影响"
    print("   缺状态 → 如实收下且 claimed=None，不影响后续券")

    assert parse_available_coupons("") == [], "空文本应返回空列表"
    assert parse_available_coupons(None) == [], "None 应返回空列表"
    assert parse_available_coupons({"raw": REAL_AVAILABLE_TEXT})[0].name == "中薯条买一送一", \
        "传入 dict 时应能从 raw 字段取到文本"
    print("   空文本 / None → []；dict 形式 → 取 raw 字段解析")
    print("   [PASS] 容错行为符合预期")
    print()

    print("=" * 60)
    print("3. 只取「可领取」，且同名去重")
    print("=" * 60)

    claimable = parse_claimable_coupons(
        REAL_AVAILABLE_TEXT, menu_prices=MENU_WITH_TARGETS
    )
    for s in claimable:
        print(f"   {s.name:<22} 价值 {s.value:>5} 元  触发 {s.required_spend:>5} 元")

    names = [s.name for s in claimable]
    assert names == ["免费脆薯饼", "人气麦旋风买一送一", "9.9元中杯冰美式"], \
        f"应只含 3 条可领取券，实际 {names}"
    assert all(s.coupon_id == "" for s in claimable), \
        "该工具不返回 couponId，应为空字符串而不是编一个"
    assert all(s.expire_date is None for s in claimable), "有效期未知"
    print("   [PASS] 仅取可领取券、去重、且不臆造 couponId / 有效期")
    print()

    # 同名去重：真实数据里「巧克力味厚松饼猪柳蛋套餐」出现两次
    dup = (
        "- 优惠券标题：某券 \\\n  状态：可领取 \\\n"
        "- 优惠券标题：某券 \\\n  状态：可领取 \\\n"
    )
    assert len(parse_claimable_coupons(dup)) == 1, "同名券应只保留一条"
    print("   同名券只保留一条（领取是不可重复的动作）")
    print()

    return claimable


def check_free_coupon_form() -> None:
    """校验新增的第 4 种券型：免费券。"""
    print("=" * 60)
    print("4. 免费券券型（价值 = 商品单价，券面即免费）")
    print("=" * 60)

    value, required = coupon_value_and_cost("免费脆薯饼", MENU_WITH_TARGETS)
    assert value == 8.0, f"免费券价值应为商品单价 8.0，实际 {value}"
    assert required == 0.0, f"券面写「免费」即无需额外消费，实际 {required}"
    print("   免费脆薯饼 → 价值 8.0 元（脆薯饼单价）· 触发成本 0 元")

    value, required = coupon_value_and_cost("免费小薯条", MENU_WITH_TARGETS)
    assert value == 14.5 and required == 0.0, "免费券的模糊匹配应命中「薯条」"
    print("   免费小薯条 → 价值 14.5 元（模糊匹配到「薯条」）· 触发成本 0 元")

    # 菜单里查不到时仍然保守置 0
    assert coupon_value_and_cost("免费月亮", MENU_WITH_TARGETS) == (0.0, 0.0), \
        "菜单无对应商品时应为 (0, 0)，不估算"
    print("   免费月亮   → (0.0, 0.0)（菜单无对应商品，不估算）")
    print("   [PASS] 第 4 种券型识别正确，且仍守住「不估算」")
    print()


def check_audit_and_render(claimable: list) -> None:
    """校验净价值预判结果与渲染口径。"""
    print("=" * 60)
    print("5. 净价值预判与渲染")
    print("=" * 60)

    natural_spend = 12.94  # 真实客单价
    report = audit_claimable(claimable, natural_spend=natural_spend)

    assert report["worthwhile"] == 1, \
        f"只有「免费脆薯饼」领了就能用，worthwhile 应为 1，实际 {report['worthwhile']}"
    assert report["counts"][CATEGORY_DIRECT] == 1, "免费券应归为「领了就能用」"
    assert report["counts"][CATEGORY_UNKNOWN] == 2, "两张查不到菜单的应为「无法判断」"
    assert report["face_total"] == 8.0, f"可确认价值应为 8.0，实际 {report['face_total']}"
    assert report["realizable"] == 8.0, "免费券净价值 = 券面价值（无额外消费）"
    print("   worthwhile=1 · 可确认价值 8.0 元 · 2 张无法判断")
    print("   [PASS] 预判结果正确")
    print()

    text = render_claimable(report, remaining_slots=2)
    print(text)
    print()

    for key in ("可领取的券", "你还有 3 张券没领", "领了就能用", "无法判断", "我们不猜"):
        assert key in text, f"渲染结果缺少「{key}」"

    # 核心契约：有效期未知 → 券列表区不得出现任何剩余天数文案。
    # 只检查列表区（结论区会提「还能点 N 单」，那是机会不是券的有效期）。
    body = text.rsplit("=" * 46, 1)[0]
    for word in FORBIDDEN_EXPIRY_WORDS:
        assert word not in body, \
            f"有效期未知，券列表不应出现「{word}」（不能把未知说成无期限）"
    print("   [PASS] 券列表不含任何剩余天数文案（有效期未知就不编）")
    print()

    # 机会已被占满时的措辞
    full = render_claimable(report, remaining_slots=0)
    assert "已被你手里的券占满" in full, "机会为 0 时应换一套措辞"
    assert "还能点 0 单" not in full, "不该输出「还能点 0 单」这种别扭说法"
    print("   [PASS] 机会为 0 时措辞已切换（不出现「还能点 0 单」）")
    print()

    # 不传机会数时不该出现机会相关文案
    plain = render_claimable(report)
    assert "机会" not in plain, "未传 remaining_slots 时不该提机会"
    print("   [PASS] 未传机会数时不输出机会相关文案")
    print()

    # 空券包
    empty = render_claimable(audit_claimable([], natural_spend=natural_spend))
    assert "没有可领取的券" in empty, "空券包应有明确说明"
    print(f"   空券包 → 「{empty}」")
    print("   [PASS] 边界处理正确")
    print()


def main() -> None:
    """依次跑完全部用例。"""
    claimable = check_parsing()
    check_free_coupon_form()
    check_audit_and_render(claimable)

    print("=" * 60)
    print("全部「可领取的券」测试通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
