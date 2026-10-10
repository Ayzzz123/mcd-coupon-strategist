"""麦当劳 MCP 适配层 —— 把 MCP 返回的真实响应转换为 saver.py 需要的结构。

本模块的核心职责：
1. **单位换算**：MCP 返回的金额是「分」，本模块统一转换为「元」。
   这是最容易出错的地方 —— 忘记换算会导致价格放大 100 倍。
2. 字段映射：MCP 用 productCode / productName，saver.py 用 code / name。
3. 券解析：把 MCP 券面信息转换为 Coupon 对象。

调用契约（实测确认，官方文档未明确）：
- query-nearby-stores  需要 beType + searchType
- query-meals          需要 storeCode + orderType + beType
- calculate-price      商品字段是 items[{productCode, quantity}]，不是 meals
- 所有金额字段单位为「分」

本模块只做数据转换，**不做任何网络请求** —— 网络请求由 Skill 编排层通过 MCP
工具发起，结果传入本模块。这样可以保持纯函数、易测试。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from saver import Coupon, Item, Plan, PricedPlan


# --------------------------------------------------------------------------
# 单位换算
# --------------------------------------------------------------------------

CENTS_PER_YUAN = 100


def cents_to_yuan(cents: Any) -> float:
    """把 MCP 返回的「分」转换为「元」。

    MCP 的金额字段均为整数分。忘记换算是本项目最容易踩的坑，
    因此所有金额转换都必须经过此函数，不允散落在各处手写 /100。

    >>> cents_to_yuan(4650)
    46.5
    >>> cents_to_yuan("2250")
    22.5
    >>> cents_to_yuan(None)
    0.0
    """
    if cents is None:
        return 0.0
    try:
        return round(int(cents) / CENTS_PER_YUAN, 2)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------
# 菜单解析
# --------------------------------------------------------------------------


def parse_meal_to_item(meal: dict, is_side: bool = False) -> Item:
    """把 query-meals 返回的单个餐品转换为 Item。

    query-meals 返回的 meals 是以 code 为 key 的映射，值为：
        {"name": str, "currentPrice": str, "originalPrice": str, ...}
    注意 currentPrice 是「元」的字符串（与 calculate-price 的「分」不同）。
    """
    return Item(
        code=str(meal.get("code", "")),
        name=str(meal.get("name", "")),
        price=float(meal.get("currentPrice") or 0),
        category=str(meal.get("category", "")),
        is_side=is_side,
    )


def parse_menu(menu_response: dict) -> tuple[list[Item], list[Item]]:
    """解析 query-meals 的响应，返回 (主食候选, 配餐候选)。

    判定规则：
    - 若餐品名称含「套餐」「三件套」「四件套」「双人餐」等关键词，视为套餐主体（主食）
    - 若餐品所属分类包含「小食」「甜品」「饮品」「蘸酱」，视为配餐 / 凑单品候选
    """
    data = menu_response.get("data") or {}
    meals_map: dict = data.get("meals") or {}
    categories: list = data.get("categories") or []

    # 建立 code -> 分类名 的反查表
    code_to_category: dict[str, str] = {}
    for cat in categories:
        cat_name = cat.get("name", "")
        for m in cat.get("meals", []):
            code_to_category.setdefault(str(m.get("code", "")), cat_name)

    SIDE_KEYWORDS = ("小食", "甜品", "饮品", "蘸酱", "饮料")
    mains: list[Item] = []
    sides: list[Item] = []

    for code, meal in meals_map.items():
        meal = dict(meal)
        meal["code"] = code
        cat = code_to_category.get(str(code), "")
        meal["category"] = cat

        is_side = any(k in cat for k in SIDE_KEYWORDS)
        item = parse_meal_to_item(meal, is_side=is_side)
        (sides if is_side else mains).append(item)

    return mains, sides


def parse_combo_from_detail(detail_response: dict) -> list[Item]:
    """解析 query-meal-detail，提取套餐内默认选中的子项。

    用于「套餐 vs 单点」的对比：把套餐拆成等价单品组合。
    注意此处的子项价格来自 choices[].diffPrice，是「差价」而非绝对价格，
    因此仅用于展示套餐构成，不用于独立计价。
    """
    data = detail_response.get("data") or {}
    items: list[Item] = []
    for rnd in data.get("rounds", []):
        for choice in rnd.get("choices", []):
            if choice.get("isDefault") == 1:
                items.append(
                    Item(
                        code=str(choice.get("code", "")),
                        name=str(choice.get("name", "")),
                        price=0.0,  # 子项价格由套餐总价决定，不单独计价
                        category=rnd.get("name", ""),
                    )
                )
    return items


# --------------------------------------------------------------------------
# 券解析
# --------------------------------------------------------------------------


# 匹配「满50减12」「满 20 减 3」「满50元减12元」等表述
_THRESHOLD_RE = re.compile(r"满\s*(\d+(?:\.\d+)?)")
_DISCOUNT_RE = re.compile(r"减\s*(\d+(?:\.\d+)?)")


def parse_coupon(raw: dict) -> Coupon:
    """解析券面信息为 Coupon。

    麦当劳券有多种形态：
    - 立减型：「满50减12」→ threshold=50, discount=12
    - 特价型：「9.9元中杯冰美式」→ 无门槛，用券价 9.9
    - 买一送一：「中薯条买一送一」→ 视作折扣型

    对于无法解析出「满减」结构的券，threshold 置 0（无门槛），
    discount 置 0，表示其优惠力度需由 calculate-price 实际计算得出。
    """
    title = str(raw.get("title") or raw.get("name") or "")

    threshold = 0.0
    discount = 0.0

    m_th = _THRESHOLD_RE.search(title)
    if m_th:
        threshold = float(m_th.group(1))
    m_dc = _DISCOUNT_RE.search(title)
    if m_dc:
        discount = float(m_dc.group(1))

    return Coupon(
        id=str(raw.get("couponId") or raw.get("id") or ""),
        name=title,
        threshold=threshold,
        discount=discount,
        scope=",".join(str(t) for t in raw.get("tags", [])) if raw.get("tags") else "",
    )


def parse_coupons(coupon_response: dict) -> list[Coupon]:
    """解析 query-store-coupons / query-my-coupons 的响应。

    同一 couponId 可能对应多张券码，按 ID 去重只保留一张。
    """
    data = coupon_response.get("data") or []
    if isinstance(data, dict):
        data = data.get("coupons") or data.get("list") or []

    seen: set[str] = set()
    coupons: list[Coupon] = []
    for raw in data:
        c = parse_coupon(raw)
        if c.id and c.id in seen:
            continue
        if c.id:
            seen.add(c.id)
        coupons.append(c)
    return coupons


# --------------------------------------------------------------------------
# 计价结果解析
# --------------------------------------------------------------------------


def parse_price_response(plan: Plan, price_response: dict) -> PricedPlan:
    """把 calculate-price 的响应转换为 PricedPlan。

    **关键**：MCP 返回的 price / discount / deliveryPrice 均为「分」，
    必须经 cents_to_yuan 转换。
    """
    data = price_response.get("data") or {}

    return PricedPlan(
        plan=plan,
        payable=cents_to_yuan(data.get("price")),
        discount=cents_to_yuan(data.get("discount")),
        delivery_fee=cents_to_yuan(data.get("deliveryPrice")),
        source="calculate-price",
    )


def extract_threshold_gap(price_response: dict) -> Optional[dict]:
    """提取「距可享受优惠还差多少」的信息。

    calculate-price 的响应中：
    - `enjoyable`：可享受但尚未满足的优惠，其 balance 为门槛差额（分）
    - `enjoyed`  ：已享受的优惠

    返回示例：{"balance": 3.5, "amountType": "3", "realDiscount": 12.0}
    balance 的单位已转换为元。若无可享受优惠则返回 None。
    """
    data = price_response.get("data") or {}
    enjoyable = data.get("enjoyable")
    if not enjoyable:
        return None

    return {
        "balance": cents_to_yuan(enjoyable.get("balance")),
        "amountType": str(enjoyable.get("amountType", "")),
        "realDiscount": float(enjoyable.get("realDiscount") or 0),
        "note": (
            f"再消费 {cents_to_yuan(enjoyable.get('balance'))} 元即可享受优惠 "
            f"{enjoyable.get('realDiscount')}"
        ),
    }


__all__ = [
    "CENTS_PER_YUAN",
    "cents_to_yuan",
    "parse_meal_to_item",
    "parse_menu",
    "parse_combo_from_detail",
    "parse_coupon",
    "parse_coupons",
    "parse_price_response",
    "extract_threshold_gap",
]
