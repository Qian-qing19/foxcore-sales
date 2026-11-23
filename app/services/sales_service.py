"""销售登记服务。

本模块承载 /sales/new 所需的查询、校验与写入逻辑，并在登记销售的同一事务内
匹配规则、生成佣金流水。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re
import sqlite3
from typing import Any
from uuid import uuid4

from app.db import get_db
from app.services.commission_service import create_entries_for_sale


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
QUANTITY_PATTERN = re.compile(r"^[0-9]+$")
PRICE_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]{1,2})?$")
SQLITE_MAX_INTEGER = 2**63 - 1
NO_ACTIVE_SHIFT_MESSAGE = "当前没有进行中的班次，请先在基础数据里添加或联系合伙人"


@dataclass(frozen=True)
class SaleCreationResult:
    """销售登记结果，供路由生成提示与重定向。"""

    sale_id: int
    total_cents: int
    duplicated: bool = False

    @property
    def sale_no(self) -> str:
        """返回面向用户的销售单号。"""
        return f"S{self.sale_id}"

    @property
    def total_yuan(self) -> str:
        """返回两位小数的合计金额字符串。"""
        return _format_cents(self.total_cents)


def _clean(value: Any) -> str:
    """把输入值规范化为去除首尾空白的字符串。"""
    return str(value or "").strip()


def _now() -> datetime:
    """返回 Asia/Shanghai 当前时间（秒级精度）。"""
    return datetime.now(CHINA_TZ).replace(microsecond=0)


def _format_datetime(value: datetime) -> str:
    """按数据库约定格式化时间。"""
    return value.astimezone(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _format_cents(cents: int) -> str:
    """把整数分格式化为两位小数元字符串。"""
    return f"{Decimal(int(cents)) / Decimal('100'):.2f}"


def _form_list(form: Mapping[str, Any], field_name: str) -> list[str]:
    """读取重复表单字段，兼容 MultiDict 与普通映射。"""
    getlist = getattr(form, "getlist", None)
    if callable(getlist):
        return [_clean(value) for value in getlist(field_name)]
    raw_value = form.get(field_name, [])
    if isinstance(raw_value, (list, tuple)):
        return [_clean(value) for value in raw_value]
    return [_clean(raw_value)]


def _submitted_lines(form: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """按行还原销售明细输入，供失败后保留页面内容。"""
    if form is None:
        return []
    product_ids = _form_list(form, "product_id")
    quantities = _form_list(form, "quantity")
    prices = _form_list(form, "final_unit_price")
    row_count = max(len(product_ids), len(quantities), len(prices))
    lines: list[dict[str, str]] = []
    for index in range(row_count):
        lines.append(
            {
                "product_id": product_ids[index] if index < len(product_ids) else "",
                "quantity": quantities[index] if index < len(quantities) else "",
                "final_unit_price": prices[index] if index < len(prices) else "",
            }
        )
    return lines


def _current_shift(
    db: sqlite3.Connection,
    salesperson_id: int,
    now_text: str,
) -> sqlite3.Row | None:
    """查询销售顾问当前进行中的最新班次。"""
    return db.execute(
        "SELECT sh.shift_id, sh.start_time, sh.end_time, "
        "b.booth_code, b.location, e.event_name, sp.name AS salesperson_name "
        "FROM SHIFT AS sh "
        "JOIN BOOTH AS b ON b.booth_id = sh.booth_id "
        "JOIN EVENT AS e ON e.event_id = b.event_id "
        "JOIN SALESPERSON AS sp ON sp.salesperson_id = sh.salesperson_id "
        "WHERE sh.salesperson_id = ? "
        "AND sp.status = '在职' "
        "AND sh.status = '进行中' "
        "AND sh.start_time <= ? "
        "AND sh.end_time >= ? "
        "ORDER BY sh.start_time DESC, sh.shift_id DESC "
        "LIMIT 1",
        (salesperson_id, now_text, now_text),
    ).fetchone()


def _active_products(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """查询全部在售产品，并准备模板展示所需的最低价元字符串。"""
    rows = db.execute(
        "SELECT product_id, product_code, name, min_price_cents "
        "FROM PRODUCT WHERE status = '在售' ORDER BY product_code"
    ).fetchall()
    return [
        {
            **dict(row),
            "min_price_yuan": _format_cents(int(row["min_price_cents"])),
        }
        for row in rows
    ]


def get_sale_form_context(
    salesperson_id: int,
    submitted_form: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造销售登记页的班次、产品、默认值和保留输入。"""
    if salesperson_id <= 0:
        raise ValueError("当前销售顾问无效")

    db = get_db()
    now_text = _format_datetime(_now())
    current_shift = _current_shift(db, salesperson_id, now_text)
    products = _active_products(db)

    submitted_shift_id = (
        _clean(submitted_form.get("shift_id")) if submitted_form is not None else ""
    )
    submitted_client_txn_id = (
        _clean(submitted_form.get("client_txn_id")) if submitted_form is not None else ""
    )
    lines = _submitted_lines(submitted_form)
    if not lines:
        lines = [{"product_id": "", "quantity": "1", "final_unit_price": ""}]

    if not submitted_shift_id and current_shift is not None:
        submitted_shift_id = str(current_shift["shift_id"])

    return {
        "current_shift": current_shift,
        "products": products,
        "can_submit": current_shift is not None and bool(products),
        "no_active_shift_message": NO_ACTIVE_SHIFT_MESSAGE,
        "form_values": {
            "shift_id": submitted_shift_id,
            "client_txn_id": submitted_client_txn_id or str(uuid4()),
            "lines": lines,
        },
    }


def _parse_positive_id(raw: Any, label: str) -> int:
    """解析严格的正整数编号。"""
    value = _clean(raw)
    if not QUANTITY_PATTERN.fullmatch(value):
        raise ValueError(f"{label}无效")
    try:
        parsed = int(value)
    except ValueError:
        raise ValueError(f"{label}无效") from None
    if parsed <= 0:
        raise ValueError(f"{label}无效")
    return parsed


def _parse_quantity(raw: Any, row_number: int) -> int:
    """校验数量是严格正整数字符串，不经过浮点数转换。"""
    value = _clean(raw)
    if not QUANTITY_PATTERN.fullmatch(value):
        raise ValueError(f"第 {row_number} 行数量必须为正整数")
    try:
        quantity = int(value)
    except ValueError:
        raise ValueError(f"第 {row_number} 行数量必须为正整数") from None
    if quantity <= 0:
        raise ValueError(f"第 {row_number} 行数量必须为正整数")
    if quantity > SQLITE_MAX_INTEGER:
        raise ValueError(f"第 {row_number} 行数量超出系统支持范围")
    return quantity


def _parse_price_cents(raw: Any, row_number: int) -> int:
    """把最多两位小数的单价元字符串精确转换为分。"""
    value = _clean(raw)
    if not PRICE_PATTERN.fullmatch(value):
        raise ValueError(
            f"第 {row_number} 行最终单价必须是有效金额，且最多保留两位小数"
        )
    try:
        cents = Decimal(value) * Decimal("100")
    except (ArithmeticError, ValueError):
        raise ValueError(
            f"第 {row_number} 行最终单价必须是有效金额，且最多保留两位小数"
        ) from None
    if not cents.is_finite() or cents != cents.to_integral_value():
        raise ValueError(
            f"第 {row_number} 行最终单价必须是有效金额，且最多保留两位小数"
        )
    parsed = int(cents)
    if parsed < 0:
        raise ValueError(f"第 {row_number} 行最终单价不能为负数")
    if parsed > SQLITE_MAX_INTEGER:
        raise ValueError(f"第 {row_number} 行最终单价超出系统支持范围")
    return parsed


def _parse_sale_items(
    db: sqlite3.Connection,
    form: Mapping[str, Any],
) -> list[dict[str, int]]:
    """校验产品、数量与单价，忽略完全未填写的明细行。"""
    product_ids = _form_list(form, "product_id")
    quantities = _form_list(form, "quantity")
    prices = _form_list(form, "final_unit_price")
    row_count = max(len(product_ids), len(quantities), len(prices))
    parsed_items: list[dict[str, int]] = []

    for index in range(row_count):
        row_number = index + 1
        product_raw = product_ids[index] if index < len(product_ids) else ""
        quantity_raw = quantities[index] if index < len(quantities) else ""
        price_raw = prices[index] if index < len(prices) else ""

        if not product_raw and not price_raw:
            continue
        if not product_raw:
            raise ValueError(f"第 {row_number} 行请选择产品")

        product_id = _parse_positive_id(product_raw, f"第 {row_number} 行产品")
        quantity = _parse_quantity(quantity_raw, row_number)
        price_cents = _parse_price_cents(price_raw, row_number)
        product = db.execute(
            "SELECT product_code, name, min_price_cents, status "
            "FROM PRODUCT WHERE product_id = ?",
            (product_id,),
        ).fetchone()
        if product is None:
            raise ValueError(f"第 {row_number} 行所选产品不存在")
        if product["status"] != "在售":
            raise ValueError(
                f"产品 {product['product_code']} {product['name']} 已停用，不能登记销售"
            )
        if price_cents < int(product["min_price_cents"]):
            raise ValueError(
                f"第 {row_number} 行单价低于最低售价，需合伙人批准"
            )

        line_cents = quantity * price_cents
        if line_cents > SQLITE_MAX_INTEGER:
            raise ValueError(f"第 {row_number} 行金额超出系统支持范围")
        parsed_items.append(
            {
                "line_no": len(parsed_items) + 1,
                "product_id": product_id,
                "quantity": quantity,
                "final_unit_price_cents": price_cents,
                "line_cents": line_cents,
            }
        )

    if not parsed_items:
        raise ValueError("请至少填写一行有效销售明细")
    return parsed_items


def _sale_by_client_txn_id(
    db: sqlite3.Connection,
    client_txn_id: str,
) -> sqlite3.Row | None:
    """按客户端幂等键查询已保存销售。"""
    return db.execute(
        "SELECT sale_id, total_cents FROM SALE WHERE client_txn_id = ?",
        (client_txn_id,),
    ).fetchone()


def _duplicate_result(sale: sqlite3.Row) -> SaleCreationResult:
    """把已有销售行转换为幂等返回结果。"""
    return SaleCreationResult(
        sale_id=int(sale["sale_id"]),
        total_cents=int(sale["total_cents"]),
        duplicated=True,
    )


def create_sale(
    form: Mapping[str, Any],
    salesperson_id: int,
) -> SaleCreationResult:
    """校验并登记一笔销售；重复 client_txn_id 返回原单且不重复写入。"""
    if salesperson_id <= 0:
        raise ValueError("当前销售顾问无效")

    client_txn_id = _clean(form.get("client_txn_id"))
    if not client_txn_id:
        raise ValueError("客户端交易编号不能为空")

    db = get_db()
    existing = _sale_by_client_txn_id(db, client_txn_id)
    if existing is not None:
        return _duplicate_result(existing)

    now = _now()
    now_text = _format_datetime(now)
    current_shift = _current_shift(db, salesperson_id, now_text)
    if current_shift is None:
        raise ValueError(NO_ACTIVE_SHIFT_MESSAGE)

    submitted_shift_raw = _clean(form.get("shift_id"))
    if submitted_shift_raw:
        submitted_shift_id = _parse_positive_id(submitted_shift_raw, "班次")
        if submitted_shift_id != int(current_shift["shift_id"]):
            raise ValueError("所选班次不是当前进行中的班次，请刷新页面后重试")

    items = _parse_sale_items(db, form)
    total_cents = sum(item["line_cents"] for item in items)
    if total_cents > SQLITE_MAX_INTEGER:
        raise ValueError("销售合计金额超出系统支持范围")

    try:
        with db:
            cursor = db.execute(
                "INSERT INTO SALE "
                "(shift_id, import_batch_id, sale_time, client_txn_id, total_cents, status) "
                "VALUES (?, NULL, ?, ?, ?, '待计佣')",
                (
                    int(current_shift["shift_id"]),
                    now_text,
                    client_txn_id,
                    total_cents,
                ),
            )
            sale_id = int(cursor.lastrowid)
            db.executemany(
                "INSERT INTO SALE_ITEM "
                "(sale_id, line_no, product_id, quantity, "
                "final_unit_price_cents, line_cents) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        sale_id,
                        item["line_no"],
                        item["product_id"],
                        item["quantity"],
                        item["final_unit_price_cents"],
                        item["line_cents"],
                    )
                    for item in items
                ],
            )
            create_entries_for_sale(db, sale_id)
    except sqlite3.IntegrityError:
        existing = _sale_by_client_txn_id(db, client_txn_id)
        if existing is not None:
            return _duplicate_result(existing)
        raise ValueError("销售保存失败，数据不符合约束") from None

    return SaleCreationResult(sale_id=sale_id, total_cents=total_cents)

SALE_PAGE_SIZE = 20
SALE_STATUSES = ("已登记", "待计佣", "已计佣", "作废")
VOIDABLE_SALE_STATUSES = ("已登记", "待计佣", "已计佣")


def _parse_filter_date(value: Any, label: str) -> str:
    """校验 YYYY-MM-DD 筛选日期并返回规范字符串。"""
    text = _clean(value)
    if not text:
        return ""
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"{label}格式应为 YYYY-MM-DD") from None
    return parsed.isoformat()


def _parse_filter_id(value: Any, label: str) -> int | None:
    """解析可选正整数筛选条件。"""
    text = _clean(value)
    if not text:
        return None
    try:
        parsed = int(text)
    except ValueError:
        raise ValueError(f"{label}筛选值无效") from None
    if parsed <= 0:
        raise ValueError(f"{label}筛选值无效")
    return parsed


def _parse_page(value: Any) -> int:
    """解析页码；空值默认第一页。"""
    text = _clean(value)
    if not text:
        return 1
    try:
        page = int(text)
    except ValueError:
        raise ValueError("页码无效") from None
    if page <= 0:
        raise ValueError("页码无效")
    return page


def _parse_sale_number_filter(value: Any) -> int | None:
    """把 S123 或 123 形式的单号筛选转换为销售编号。"""
    text = _clean(value)
    if not text:
        return None
    normalized = text[1:] if text[:1].upper() == "S" else text
    if not normalized.isdigit() or int(normalized) <= 0:
        raise ValueError("单号格式应为 S123 或 123")
    return int(normalized)


def _user_sale_scope(
    user_role: str,
    user_salesperson_id: int | None,
) -> tuple[bool, int | None]:
    """返回当前用户是否为合伙人，以及强制生效的销售顾问编号。"""
    if user_role == "合伙人":
        return True, None
    if user_role == "销售顾问":
        if user_salesperson_id is None or int(user_salesperson_id) <= 0:
            raise PermissionError("当前账号未关联销售顾问")
        return False, int(user_salesperson_id)
    raise PermissionError("当前账号无权查看销售流水")


def get_sales_list(
    filters: Mapping[str, Any],
    *,
    user_role: str,
    user_salesperson_id: int | None,
) -> dict[str, Any]:
    """按权限范围查询销售流水，返回筛选条件、分页信息和页面选项。"""
    is_partner, forced_salesperson_id = _user_sale_scope(
        user_role,
        user_salesperson_id,
    )

    date_from = _parse_filter_date(filters.get("date_from"), "起始日期")
    date_to = _parse_filter_date(filters.get("date_to"), "结束日期")
    if date_from and date_to and date_from > date_to:
        raise ValueError("起始日期不能晚于结束日期")

    event_id = _parse_filter_id(filters.get("event_id"), "活动")
    salesperson_id = _parse_filter_id(filters.get("salesperson_id"), "销售顾问")
    status = _clean(filters.get("status"))
    if status and status not in SALE_STATUSES:
        raise ValueError("销售状态筛选无效")
    sale_no = _clean(filters.get("sale_no"))
    sale_id = _parse_sale_number_filter(sale_no)
    page = _parse_page(filters.get("page"))

    where_clauses: list[str] = []
    where_params: list[Any] = []

    if forced_salesperson_id is not None:
        where_clauses.append("sh.salesperson_id = ?")
        where_params.append(forced_salesperson_id)
    elif is_partner and salesperson_id is not None:
        where_clauses.append("sh.salesperson_id = ?")
        where_params.append(salesperson_id)

    if event_id is not None:
        where_clauses.append("e.event_id = ?")
        where_params.append(event_id)
    if date_from:
        where_clauses.append("s.sale_time >= ?")
        where_params.append(f"{date_from} 00:00:00")
    if date_to:
        next_date = (
            datetime.strptime(date_to, "%Y-%m-%d").date() + timedelta(days=1)
        ).isoformat()
        where_clauses.append("s.sale_time < ?")
        where_params.append(f"{next_date} 00:00:00")
    if status:
        where_clauses.append("s.status = ?")
        where_params.append(status)
    if sale_id is not None:
        where_clauses.append("s.sale_id = ?")
        where_params.append(sale_id)

    where_sql = " AND ".join(where_clauses) if where_clauses else "1 = 1"
    db = get_db()
    total = int(
        db.execute(
            "SELECT COUNT(*) "
            "FROM SALE AS s "
            "JOIN SHIFT AS sh ON sh.shift_id = s.shift_id "
            "JOIN BOOTH AS b ON b.booth_id = sh.booth_id "
            "JOIN EVENT AS e ON e.event_id = b.event_id "
            f"WHERE {where_sql}",
            tuple(where_params),
        ).fetchone()[0]
    )
    total_pages = max(1, (total + SALE_PAGE_SIZE - 1) // SALE_PAGE_SIZE)
    page = min(page, total_pages)
    offset = (page - 1) * SALE_PAGE_SIZE

    rows = db.execute(
        "SELECT s.sale_id, s.sale_time, s.total_cents, s.status, "
        "sp.name AS salesperson_name, e.event_name, b.booth_code, "
        "COALESCE(item_totals.item_count, 0) AS item_count, "
        "COALESCE(commission_totals.commission_cents, 0) AS commission_cents "
        "FROM SALE AS s "
        "JOIN SHIFT AS sh ON sh.shift_id = s.shift_id "
        "JOIN SALESPERSON AS sp ON sp.salesperson_id = sh.salesperson_id "
        "JOIN BOOTH AS b ON b.booth_id = sh.booth_id "
        "JOIN EVENT AS e ON e.event_id = b.event_id "
        "LEFT JOIN ("
        "    SELECT sale_id, SUM(quantity) AS item_count "
        "    FROM SALE_ITEM GROUP BY sale_id"
        ") AS item_totals ON item_totals.sale_id = s.sale_id "
        "LEFT JOIN ("
        "    SELECT sale_id, SUM(commission_cents) AS commission_cents "
        "    FROM COMMISSION_ENTRY "
        "    WHERE status <> '已冲减' "
        "    GROUP BY sale_id"
        ") AS commission_totals ON commission_totals.sale_id = s.sale_id "
        f"WHERE {where_sql} "
        "ORDER BY s.sale_time DESC, s.sale_id DESC "
        "LIMIT ? OFFSET ?",
        tuple(where_params) + (SALE_PAGE_SIZE, offset),
    ).fetchall()

    sales_rows = [
        {
            **dict(row),
            "item_count": int(row["item_count"]),
            "total_cents": int(row["total_cents"]),
            "commission_cents": int(row["commission_cents"]),
        }
        for row in rows
    ]
    events = [
        dict(row)
        for row in db.execute(
            "SELECT event_id, event_name FROM EVENT "
            "ORDER BY start_date DESC, event_id DESC"
        ).fetchall()
    ]
    salespersons = (
        [
            dict(row)
            for row in db.execute(
                "SELECT salesperson_id, emp_code, name FROM SALESPERSON "
                "ORDER BY emp_code"
            ).fetchall()
        ]
        if is_partner
        else []
    )

    return {
        "sales": sales_rows,
        "events": events,
        "salespersons": salespersons,
        "status_options": SALE_STATUSES,
        "is_partner": is_partner,
        "filters": {
            "date_from": date_from,
            "date_to": date_to,
            "event_id": str(event_id) if event_id is not None else "",
            "salesperson_id": (
                str(salesperson_id) if salesperson_id is not None else ""
            ),
            "status": status,
            "sale_no": sale_no,
        },
        "page": page,
        "page_size": SALE_PAGE_SIZE,
        "total": total,
        "total_pages": total_pages,
    }


def get_sale_detail(
    sale_id: int,
    *,
    user_role: str,
    user_salesperson_id: int | None,
) -> dict[str, Any]:
    """查询销售单、明细及逐行佣金，并在服务层执行数据范围校验。"""
    if sale_id <= 0:
        raise ValueError("销售编号无效")

    db = get_db()
    sale_row = db.execute(
        "SELECT s.sale_id, s.sale_time, s.client_txn_id, s.total_cents, s.status, "
        "s.void_reason, s.void_at, vu.username AS void_by_username, "
        "sh.shift_id, sh.start_time AS shift_start, sh.end_time AS shift_end, "
        "sp.salesperson_id, sp.emp_code AS salesperson_code, "
        "sp.name AS salesperson_name, b.booth_code, b.location, "
        "e.event_name, e.event_type "
        "FROM SALE AS s "
        "JOIN SHIFT AS sh ON sh.shift_id = s.shift_id "
        "JOIN SALESPERSON AS sp ON sp.salesperson_id = sh.salesperson_id "
        "JOIN BOOTH AS b ON b.booth_id = sh.booth_id "
        "JOIN EVENT AS e ON e.event_id = b.event_id "
        "LEFT JOIN APP_USER AS vu ON vu.user_id = s.void_by "
        "WHERE s.sale_id = ?",
        (sale_id,),
    ).fetchone()
    if sale_row is None:
        raise LookupError("销售单不存在")

    _, forced_salesperson_id = _user_sale_scope(
        user_role,
        user_salesperson_id,
    )
    if (
        forced_salesperson_id is not None
        and int(sale_row["salesperson_id"]) != forced_salesperson_id
    ):
        raise PermissionError("无权查看该销售单")

    item_rows = db.execute(
        "SELECT si.line_no, p.product_code, p.name AS product_name, "
        "si.quantity, si.final_unit_price_cents, si.line_cents, "
        "ce.commission_cents, ce.status AS commission_status, "
        "cr.rule_code "
        "FROM SALE_ITEM AS si "
        "JOIN PRODUCT AS p ON p.product_id = si.product_id "
        "LEFT JOIN COMMISSION_ENTRY AS ce "
        "    ON ce.sale_id = si.sale_id AND ce.line_no = si.line_no "
        "LEFT JOIN COMMISSION_RULE AS cr ON cr.rule_id = ce.rule_id "
        "WHERE si.sale_id = ? "
        "ORDER BY si.line_no",
        (sale_id,),
    ).fetchall()
    items = [
        {
            **dict(row),
            "line_no": int(row["line_no"]),
            "quantity": int(row["quantity"]),
            "final_unit_price_cents": int(row["final_unit_price_cents"]),
            "line_cents": int(row["line_cents"]),
            "commission_cents": (
                int(row["commission_cents"])
                if row["commission_cents"] is not None
                else None
            ),
        }
        for row in item_rows
    ]
    commission_total_cents = sum(
        int(item["commission_cents"])
        for item in items
        if item["commission_cents"] is not None
        and item["commission_status"] != "已冲减"
    )

    return {
        "sale": dict(sale_row),
        "items": items,
        "commission_total_cents": commission_total_cents,
        "can_void": (
            user_role == "合伙人"
            and str(sale_row["status"]) in VOIDABLE_SALE_STATUSES
        ),
    }


def void_sale(sale_id: int, reason: Any, void_by: int) -> dict[str, Any]:
    """作废销售并冲减待结算佣金；已结算佣金保持原状。"""
    if sale_id <= 0:
        raise ValueError("销售编号无效")
    if void_by <= 0:
        raise ValueError("作废操作人无效")
    void_reason = _clean(reason)
    if not void_reason:
        raise ValueError("作废原因不能为空")
    if len(void_reason) > 200:
        raise ValueError("作废原因不能超过 200 字")

    db = get_db()
    void_at = _format_datetime(_now())
    with db:
        sale = db.execute(
            "SELECT status FROM SALE WHERE sale_id = ?",
            (sale_id,),
        ).fetchone()
        if sale is None:
            raise LookupError("销售单不存在")
        current_status = str(sale["status"])
        if current_status == "作废":
            raise ValueError("该销售单已作废，请勿重复操作")
        if current_status not in VOIDABLE_SALE_STATUSES:
            raise ValueError("当前销售状态不允许作废")

        db.execute(
            "UPDATE SALE "
            "SET status = '作废', void_reason = ?, void_by = ?, void_at = ? "
            "WHERE sale_id = ?",
            (void_reason, void_by, void_at, sale_id),
        )
        reversed_cursor = db.execute(
            "UPDATE COMMISSION_ENTRY "
            "SET status = '已冲减' "
            "WHERE sale_id = ? AND status = '待结算'",
            (sale_id,),
        )

    return {
        "sale_id": sale_id,
        "status": "作废",
        "void_reason": void_reason,
        "void_by": void_by,
        "void_at": void_at,
        "reversed_entries": int(reversed_cursor.rowcount),
    }
