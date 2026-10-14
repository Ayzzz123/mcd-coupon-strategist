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
from datetime import date, datetime
from typing import Any, NamedTuple, Optional

from auditor import CouponStock, OrderRecord
from points import PointsAccount, RedeemableItem
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
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# 匹配「9.9元中杯冰美式」形态：数字 + 元
_FIXED_PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*元")
# 匹配「免费脆薯饼」形态：券面直接写「免费」
_FREE_RE = re.compile(r"^免费\s*(.+)$")


def _threshold_of(title: str) -> float:
    """从券面文本取出满减门槛（元）；非满减券返回 0。"""
    m = _THRESHOLD_RE.search(title)
    return float(m.group(1)) if m else 0.0


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


# --------------------------------------------------------------------------
# 历史订单与券库存解析（供 auditor.py 使用）
# --------------------------------------------------------------------------


def _parse_datetime(raw: Any) -> Optional[date]:
    """解析 MCP 的时间字符串为 date。

    支持 "2026-10-02 15:57:36" / "2026-10-02" / ISO 格式。
    解析失败返回 None，由调用方决定如何处理。
    """
    if not raw:
        return None
    s = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[: len(fmt) + 2].strip(), fmt).date()
        except ValueError:
            continue
    # 退一步：只取前 10 位的日期部分
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_orders(order_response: dict) -> list[OrderRecord]:
    """解析 order-list 的响应为 auditor.OrderRecord 列表。

    ⚠️ 注意：`realTotalAmount` 是**字符串型的「元」**（如 "15.5"），
    与 calculate-price 返回的「分」完全不同 —— 此处直接按元解析，
    不可再除以 100。这是本适配层中最容易混淆的单位差异。

    同时会过滤掉「整单退款」的订单 —— 退款单不代表真实消费，
    计入总额会虚高。

    退款与 0 元订单的边界（都**不猜**）
    ----------------------------------

    - **「部分退款」保留但标记**：这类订单有部分真实消费，不能整笔丢掉；
      但响应里的 `realTotalAmount` 是退款前还是退款后的口径**无法确认**，
      且响应**不提供退款金额字段**。所以本项目既不猜退款额、也不假装没这回事，
      而是把笔数带进消费画像，由报告如实提示「支出可能略偏高」。
    - **0 元订单保留**：多为券核销或积分兑换。是否算「消费」由消费画像
      按口径决定（见 `auditor.OrderRecord.is_spending`），适配层只负责如实传递。
    """
    data = order_response.get("data") or {}
    raw_list = data.get("list") or []

    records: list[OrderRecord] = []
    for o in raw_list:
        status = str(o.get("orderStatus", ""))
        # 整单退款不计入消费
        if "整单退款" in status:
            continue

        order_date = _parse_datetime(o.get("createTime"))
        if order_date is None:
            continue

        try:
            amount = float(o.get("realTotalAmount") or 0)
        except (TypeError, ValueError):
            amount = 0.0

        items: list[str] = []
        for p in o.get("orderProductList", []):
            name = str(p.get("productName", ""))
            qty = int(p.get("quantity") or 1)
            items.extend([name] * qty)

        # 渠道：beType 1=到店，2=外送，5=得来速
        be = str(o.get("beType", ""))
        channel = {"1": "到店", "2": "外送", "5": "得来速"}.get(be, "其他")

        records.append(
            OrderRecord(
                order_time=order_date,
                amount=amount,
                items=items,
                channel=channel,
                status=status,
            )
        )

    return records


def coupon_value_and_cost(
    coupon_title: str,
    menu_prices: dict,
) -> tuple[float, float]:
    """从券面文本推算「券面价值」与「触发所需消费额」，单位均为元。

    ========== ==================== =============== ==================
    券型        券面示例               券面价值         触发所需消费额
    ========== ==================== =============== ==================
    满减        满50减12               12（减免额）      50（门槛）
    买一送一     中薯条买一送一           商品单价          商品单价（买一件）
    免费        免费脆薯饼              商品单价          0（券面即免费）
    固定价      9.9元中杯冰美式          原价 − 9.9      原价
    ========== ==================== =============== ==================

    「触发所需消费额」是净价值判定的基础 —— **用任何一张券都至少要花这么多钱**。
    没有它就无法回答「为了省这 12 元，我要多花多少」。

    无法与菜单对应上时返回 ``(0.0, 0.0)``，**绝不估算**：
    宁可少报价值，也不编造一个看似合理的数字。

    已知局限（免费券）：券面写「免费」即按字面视为无需额外消费，与
    「满50减12」按字面读门槛是同一个标准。若该券实际要求随单购买，
    券面并无体现 —— 这种情况下本函数会高估其净价值。
    """
    title = coupon_title.strip()

    # 形态 1：满减券 —— 门槛与减免额都写在券面上
    m_threshold = _THRESHOLD_RE.search(title)
    if m_threshold:
        m_discount = _DISCOUNT_RE.search(title)
        threshold = float(m_threshold.group(1))
        discount = float(m_discount.group(1)) if m_discount else 0.0
        return discount, threshold

    # 形态 2：买一送一 —— 价值等于商品单价，代价是买一件
    if "买一送一" in title or "买1送1" in title:
        target = title.replace("买一送一", "").replace("买1送1", "").strip()
        price = _lookup_price(target, menu_prices)
        if price is not None:
            return round(price, 2), round(price, 2)
        return 0.0, 0.0

    # 形态 3：免费券 —— 券面即「免费」，价值等于商品单价、无需额外消费
    m_free = _FREE_RE.match(title)
    if m_free:
        price = _lookup_price(m_free.group(1).strip(), menu_prices)
        if price is not None:
            return round(price, 2), 0.0
        return 0.0, 0.0

    # 形态 4：固定价券「9.9元xxx」—— 价值是原价与固定价之差
    m_fixed = _FIXED_PRICE_RE.search(title)
    if m_fixed:
        fixed = float(m_fixed.group(1))
        target = _FIXED_PRICE_RE.sub("", title).strip()
        price = _lookup_price(target, menu_prices)
        if price is not None:
            return max(round(price - fixed, 2), 0.0), round(price, 2)
        return 0.0, 0.0

    return 0.0, 0.0


def coupon_real_value(
    coupon_title: str,
    menu_prices: dict,
    fallback: float = 0.0,
) -> float:
    """从券面文本推算券的**真实价值**（用这张券能省下多少元）。

    算法细节见 `coupon_value_and_cost()`；本函数只取其中的价值部分，
    供不需要关心触发成本的调用方使用。

    设计原则：宁可少报价值，也不编造一个看似合理的数字。
    一旦券面无法与菜单对应上，就返回 `fallback`（默认 0），
    并在报告中如实标注「价值未知」。
    """
    value, _required = coupon_value_and_cost(coupon_title, menu_prices)
    return value if value > 0 else fallback


def _lookup_price(name: str, menu_prices: dict) -> Optional[float]:
    """在菜单价格表中模糊查找商品单价。

    匹配策略：先精确匹配，再找「菜单项名被券面文本包含」或反向包含的项。
    例如券面「中薯条买一送一」→ target「中薯条」→ 命中菜单「薯条/中薯条」。
    """
    if not name or not menu_prices:
        return None

    # 精确匹配
    if name in menu_prices:
        return menu_prices[name]

    # 双向包含匹配，取名称最长的（最具体）那个
    candidates = [
        (k, v) for k, v in menu_prices.items()
        if k and (k in name or name in k)
    ]
    if candidates:
        candidates.sort(key=lambda kv: -len(kv[0]))
        return candidates[0][1]

    return None


class CouponProbe(NamedTuple):
    """一张券的**实测**结果（来自带券计价）。"""

    value: float  # 券真实价值（元）—— 计价响应里的 discount
    price: float  # 用券实付（元）—— 计价响应里的 price，即真实触发成本


def parse_coupon_probes(probe_responses: dict) -> dict:
    """把「带券计价」响应批量解析为 ``{couponId: CouponProbe}``。

    券的真实价值有两种获取方式，本项目优先用第二种
    -------------------------------------------------

    1. **菜单反查**（`coupon_value_and_cost`）—— 从券名剥离商品名、去菜单查价。
       不需要额外调用，但只认四种券型；对「王牌小食拼」这类标题里没有数字的券
       只能返回 0（价值未知）。

    2. **带券计价**（本函数）—— 把券自带的 `productCode` 与 `couponId` /
       `couponCode` 一起传给 `calculate-price`，返回的 `discount` 就是**精确价值**，
       `price` 就是用券实付（真实触发成本）。

    实测对比（2026-10-13，门店 1990622）：

    ================== ============ ==============
    券                  菜单反查      带券计价
    ================== ============ ==============
    中薯条买一送一        14.5 元       14.5 元
    王牌小食拼            0（未知）      **6.6 元**
    ================== ============ ==============

    反查不但可能低估，还会把真实存在的价值报成 0。因此策略是
    **能实测就实测，测不到再退回反查** —— 两者都不估算。
    """
    probes: dict = {}
    for coupon_id, response in (probe_responses or {}).items():
        data = (response or {}).get("data") or {}
        if not data:
            continue
        probes[str(coupon_id)] = CouponProbe(
            value=cents_to_yuan(data.get("discount")),
            price=cents_to_yuan(data.get("price")),
        )
    return probes


def parse_coupon_stock(
    coupon_response: dict,
    menu_prices: Optional[dict] = None,
    probes: Optional[dict] = None,
) -> list[CouponStock]:
    """解析 query-my-coupons 的响应为 auditor.CouponStock 列表。

    券的真实价值与触发成本，按下面的优先级确定：

    1. **实测**（`probes`，由 `parse_coupon_probes` 产出）—— 精确
    2. **菜单反查**（`menu_prices`）—— 买一送一 / 固定价券可算，其余为 0

    若两者都拿不到，value 取 0，报告中标注「价值未知」。
    **绝不估算** —— 宁可少报，也不编造一个看似合理的数字。
    """
    menu_prices = menu_prices or {}
    probes = probes or {}
    data = coupon_response.get("data") or []
    if isinstance(data, dict):
        data = data.get("coupons") or data.get("list") or []

    stock: list[CouponStock] = []
    for raw in data:
        title = str(raw.get("title") or raw.get("name") or "")
        coupon_id = str(raw.get("couponId") or raw.get("id") or "")

        probe = probes.get(coupon_id)
        if probe is not None:
            value, required = probe.value, probe.price
        else:
            value, required = coupon_value_and_cost(title, menu_prices)

        # 门槛只对满减券有意义
        threshold = _threshold_of(title)

        # 有效期：tradeDateTime 形如 "2026-10-12 10:30-2026-10-16 23:59"
        expire = None
        trade = str(raw.get("tradeDateTime") or "")
        dates = _DATE_RE.findall(trade)
        if dates:
            expire = _parse_datetime(dates[-1])

        stock.append(
            CouponStock(
                coupon_id=coupon_id,
                name=title,
                value=value,
                expire_date=expire,
                threshold=threshold,
                required_spend=required,
            )
        )

    return stock


def build_menu_price_table(menu_response: dict) -> dict:
    """从 query-meals 响应构建「商品名 → 单价(元)」表，供券价值推算使用。

    注意：菜单的 currentPrice 是**元**的字符串，直接解析即可。
    """
    data = menu_response.get("data") or {}
    meals_map: dict = data.get("meals") or {}

    table: dict[str, float] = {}
    for code, meal in meals_map.items():
        name = str(meal.get("name") or "").strip()
        if not name:
            continue
        try:
            price = float(meal.get("currentPrice") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0:
            table[name] = price
    return table


# --------------------------------------------------------------------------
# 麦麦省券列表（含「还没领的」）
# --------------------------------------------------------------------------

# 该工具输出的是 Markdown 文本，按「优惠券标题 / 状态」两个字段逐行解析
_AVAILABLE_TITLE_RE = re.compile(r"优惠券标题[：:]\s*(.+)")
_AVAILABLE_STATUS_RE = re.compile(r"状态[：:]\s*(.+)")


class AvailableCoupon(NamedTuple):
    """麦麦省里的一张券及其领取状态（来自 `available-coupons`）。"""

    name: str
    claimed: Optional[bool]  # True=已领取；False=可领取；None=状态无法识别


def _available_text(response: Any) -> str:
    """从 `available-coupons` 的响应中取出可解析的文本。"""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        for key in ("raw", "text", "content", "message"):
            value = response.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return ""


def _claim_status(status: str) -> Optional[bool]:
    """把券状态文案归一为「是否已领取」；无法识别时返回 None。

    **只认「可领取」为未领取** —— 出现没见过的状态（如「已使用」）时返回
    None，让调用方把它排除在「可领取」之外，而不是想当然地当成没领。
    """
    if "已领取" in status:
        return True
    if "可领取" in status:
        return False
    return None


def parse_available_coupons(response: Any) -> list[AvailableCoupon]:
    """解析 `available-coupons` 的响应为券列表（含领取状态）。

    ⚠️ **该工具返回的不是 JSON，而是 Markdown 文本**（实测 2026-10-13）：

        ### 麦麦省优惠券列表：
        - 优惠券标题：免费脆薯饼 \\
          状态：可领取 \\
          ...

    因此这里按「优惠券标题 / 状态」两个字段逐行解析，并对行尾反斜杠与
    多余空白做容错。传入 dict 时会先尝试取其中的文本（如 `raw` 字段）。

    该工具**不返回** couponId、面值、有效期或商品码 —— 这些一律不臆测。
    缺少状态的券仍会返回，但 `claimed` 为 None（不静默丢弃）。
    """
    text = _available_text(response)
    if not text:
        return []

    coupons: list[AvailableCoupon] = []
    pending: Optional[str] = None

    for raw_line in text.splitlines():
        line = raw_line.strip().rstrip("\\").strip()
        if not line:
            continue

        m_title = _AVAILABLE_TITLE_RE.search(line)
        if m_title:
            if pending is not None:  # 上一张缺状态，如实收下而不是丢掉
                coupons.append(AvailableCoupon(pending, None))
            pending = m_title.group(1).strip()
            continue

        m_status = _AVAILABLE_STATUS_RE.search(line)
        if m_status and pending is not None:
            coupons.append(AvailableCoupon(pending, _claim_status(m_status.group(1))))
            pending = None

    if pending is not None:
        coupons.append(AvailableCoupon(pending, None))

    return coupons


def parse_claimable_coupons(
    coupon_response: Any,
    menu_prices: Optional[dict] = None,
) -> list[CouponStock]:
    """解析 `available-coupons`，取出**尚未领取**的券（作为 CouponStock）。

    价值遵循与已领取券相同的优先级，但这一批券**没有 couponId**
    （该工具不返回），因此「带券计价实测」这条途径天然不可用，
    只能靠菜单反查（`coupon_value_and_cost`）。查不到时 value 取 0，
    报告中标注「价值未知」—— 两条路都不估算。

    两个字段需要特别说明：

    - `coupon_id` 一律为空字符串 —— 该工具不返回 ID
    - `expire_date` 一律为 None —— **这不代表无期限，而是未知**。
      券包体检里 None 表示无期限，这里必须按「未知」解读，
      所以渲染时不要套用 `CouponVerdict.expiry_text()`
      （见 `planner.render_claimable`）。

    同名券只保留一张 —— 领取是不可重复的动作，重复提示没有意义。
    状态无法识别的券一律不进入本列表（宁可不提示，也不误报）。
    """
    menu_prices = menu_prices or {}

    seen: set[str] = set()
    stock: list[CouponStock] = []
    for coupon in parse_available_coupons(coupon_response):
        if coupon.claimed is not False:
            continue
        if coupon.name in seen:
            continue
        seen.add(coupon.name)

        value, required = coupon_value_and_cost(coupon.name, menu_prices)
        stock.append(
            CouponStock(
                coupon_id="",
                name=coupon.name,
                value=value,
                expire_date=None,  # 未知，不是无期限
                threshold=_threshold_of(coupon.name),
                required_spend=required,
            )
        )

    return stock


# --------------------------------------------------------------------------
# 积分：账户与可兑换商品
# --------------------------------------------------------------------------


def _to_float(raw: Any) -> float:
    """把 MCP 返回的字符串数值转为 float；无法解析时返回 0.0。

    积分字段全部是字符串（如 `"376.3"`），且可以是小数 —— 不能当整数处理。
    """
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _to_int(raw: Any) -> int:
    """把 MCP 返回的 ID 类字段转为 int；无法解析时返回 0。"""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def parse_points_account(account_response: dict) -> PointsAccount:
    """解析 query-my-account 的响应为 `PointsAccount`。

    注意两点：

    - 所有积分字段都是**字符串**且**可以是小数**（`"376.3"`），按整数解析会丢精度。
    - 接口里拼写为 `currentMouthExpirePoint` / `nextMouthExpirePoint`
      （Mouth 而非 Month），这是接口本身的拼写，不是笔误 —— 按原样读取。
    """
    data = account_response.get("data") or {}
    return PointsAccount(
        available=_to_float(data.get("availablePoint")),
        accumulative=_to_float(data.get("accumulativePoint")),
        used=_to_float(data.get("usedPoint")),
        expired=_to_float(data.get("expiredPoint")),
        expiring_this_month=_to_float(data.get("currentMouthExpirePoint")),
        expiring_next_month=_to_float(data.get("nextMouthExpirePoint")),
        frozen=_to_float(data.get("frozenPoint")),
    )


def parse_points_products(product_response: dict) -> list[RedeemableItem]:
    """解析 mall-points-products 的响应为可兑换商品列表。

    只读结构化字段（`spuName` / `spuId` / `point` / `downTime` / `status`）。

    ⚠️ **不要从 `note`（购买须知）里提取兑换内容或金额。** `mall-product-detail`
    的响应说明明确要求：「纯展示文案，必须原文展示给用户，严禁从中提取任何数值
    用于下单校验、价格计算、库存判断等业务逻辑」。见 fixtures/mall_product_detail.json。
    """
    items: list[RedeemableItem] = []
    for raw in product_response.get("data") or []:
        name = str(raw.get("spuName") or "").strip()
        if not name:
            continue
        items.append(
            RedeemableItem(
                spu_id=_to_int(raw.get("spuId")),
                name=name,
                points=_to_float(raw.get("point")),
                deadline=_parse_datetime(raw.get("downTime")),
                status=str(raw.get("status") or ""),
            )
        )
    return items


__all__ = [
    "CENTS_PER_YUAN",
    "AvailableCoupon",
    "CouponProbe",
    "cents_to_yuan",
    "parse_meal_to_item",
    "parse_menu",
    "parse_combo_from_detail",
    "parse_coupon",
    "parse_coupons",
    "parse_price_response",
    "extract_threshold_gap",
    "parse_orders",
    "parse_available_coupons",
    "parse_claimable_coupons",
    "parse_coupon_probes",
    "parse_coupon_stock",
    "parse_points_account",
    "parse_points_products",
    "build_menu_price_table",
    "coupon_value_and_cost",
    "coupon_real_value",
]
