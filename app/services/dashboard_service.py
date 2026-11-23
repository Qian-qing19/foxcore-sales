"""合伙人工作台数据聚合服务。"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Final

from app.db import get_db


CHINA_TZ: Final[timezone] = timezone(timedelta(hours=8), name="Asia/Shanghai")

_TODAY_SALES_SQL: Final[str] = """
    SELECT COALESCE(SUM(si.line_cents), 0)
    FROM SALE_ITEM AS si
    JOIN SALE AS s ON s.sale_id = si.sale_id
    WHERE s.status <> '作废'
      AND s.sale_time >= ?
      AND s.sale_time < ?
"""

_MONTH_SALES_SQL: Final[str] = """
    SELECT COALESCE(SUM(si.line_cents), 0)
    FROM SALE_ITEM AS si
    JOIN SALE AS s ON s.sale_id = si.sale_id
    WHERE s.status <> '作废'
      AND s.sale_time >= ?
      AND s.sale_time < ?
"""

_MONTH_COMMISSION_SQL: Final[str] = """
    SELECT COALESCE(SUM(ce.commission_cents), 0)
    FROM COMMISSION_ENTRY AS ce
    JOIN SALE AS s ON s.sale_id = ce.sale_id
    WHERE s.status <> '作废'
      AND ce.status IN ('待结算', '已结算')
      AND s.sale_time >= ?
      AND s.sale_time < ?
"""

_PENDING_COMMISSION_SQL: Final[str] = """
    SELECT COALESCE(SUM(ce.commission_cents), 0)
    FROM COMMISSION_ENTRY AS ce
    JOIN SALE AS s ON s.sale_id = ce.sale_id
    WHERE s.status <> '作废'
      AND ce.status = '待结算'
"""

_TREND_SQL: Final[str] = """
    SELECT
        substr(s.sale_time, 1, 10) AS sale_date,
        COALESCE(SUM(si.line_cents), 0) AS sales_cents
    FROM SALE_ITEM AS si
    JOIN SALE AS s ON s.sale_id = si.sale_id
    WHERE s.status <> '作废'
      AND s.sale_time >= ?
      AND s.sale_time < ?
    GROUP BY substr(s.sale_time, 1, 10)
"""

_TOP_PRODUCTS_SQL: Final[str] = """
    SELECT
        p.product_code,
        p.name,
        COALESCE(SUM(si.quantity), 0) AS quantity,
        COALESCE(SUM(si.line_cents), 0) AS sales_cents
    FROM SALE_ITEM AS si
    JOIN SALE AS s ON s.sale_id = si.sale_id
    JOIN PRODUCT AS p ON p.product_id = si.product_id
    WHERE s.status <> '作废'
      AND s.sale_time >= ?
      AND s.sale_time < ?
    GROUP BY p.product_id, p.product_code, p.name
    ORDER BY quantity DESC, sales_cents DESC, p.product_code
    LIMIT 5
"""

_RECENT_SALES_SQL: Final[str] = """
    SELECT
        s.sale_id,
        s.sale_time,
        sp.name AS salesperson_name,
        s.total_cents,
        s.status
    FROM SALE AS s
    JOIN SHIFT AS sh ON sh.shift_id = s.shift_id
    JOIN SALESPERSON AS sp ON sp.salesperson_id = sh.salesperson_id
    ORDER BY s.sale_time DESC, s.sale_id DESC
    LIMIT 5
"""


def _current_time() -> datetime:
    """返回当前上海时区时间，供路由取数和测试固定时钟使用。"""
    return datetime.now(CHINA_TZ)


def _normalize_now(as_of: datetime | None) -> datetime:
    """把可选截止时间规范为上海时区的 aware datetime。"""
    if as_of is None:
        return _current_time()
    if as_of.tzinfo is None:
        return as_of.replace(tzinfo=CHINA_TZ)
    return as_of.astimezone(CHINA_TZ)


def _day_start(day: date) -> str:
    """把日期转换为数据库日期时间下界字符串。"""
    return f"{day.isoformat()} 00:00:00"


def _scalar_cents(sql: str, parameters: tuple[Any, ...] = ()) -> int:
    """执行单值金额 SQL 并把 NULL 或无行结果归一为整数分。"""
    row = get_db().execute(sql, parameters).fetchone()
    return int(row[0] or 0) if row is not None else 0


def _height_percent(cents: int, maximum_cents: int) -> str:
    """按最高日金额计算 CSS 柱高百分比文本。"""
    if maximum_cents <= 0:
        return "0"
    ratio = (
        Decimal(cents) * Decimal("100") / Decimal(maximum_cents)
    ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return f"{ratio:.4f}".rstrip("0").rstrip(".")


def get_dashboard_data(as_of: datetime | None = None) -> dict[str, Any]:
    """汇总合伙人工作台指标、趋势、热销产品与最近销售。"""
    now = _normalize_now(as_of)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    month_start = today.replace(day=1)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    today_start_text = _day_start(today)
    tomorrow_start_text = _day_start(tomorrow)
    month_start_text = _day_start(month_start)
    next_month_start_text = _day_start(next_month)

    metrics = {
        "today_sales_cents": _scalar_cents(
            _TODAY_SALES_SQL,
            (today_start_text, tomorrow_start_text),
        ),
        "month_sales_cents": _scalar_cents(
            _MONTH_SALES_SQL,
            (month_start_text, next_month_start_text),
        ),
        "month_commission_cents": _scalar_cents(
            _MONTH_COMMISSION_SQL,
            (month_start_text, next_month_start_text),
        ),
        "pending_commission_cents": _scalar_cents(_PENDING_COMMISSION_SQL),
    }

    trend_start = today - timedelta(days=6)
    trend_rows = get_db().execute(
        _TREND_SQL,
        (_day_start(trend_start), tomorrow_start_text),
    ).fetchall()
    daily_totals = {
        str(row["sale_date"]): int(row["sales_cents"] or 0)
        for row in trend_rows
    }
    maximum_cents = max(daily_totals.values(), default=0)
    trend = []
    for offset in range(7):
        day = trend_start + timedelta(days=offset)
        sale_cents = daily_totals.get(day.isoformat(), 0)
        trend.append(
            {
                "date": day.isoformat(),
                "label": day.strftime("%m-%d"),
                "sales_cents": sale_cents,
                "height_percent": _height_percent(sale_cents, maximum_cents),
            }
        )

    top_products = [
        {
            "product_code": str(row["product_code"]),
            "name": str(row["name"]),
            "quantity": int(row["quantity"] or 0),
            "sales_cents": int(row["sales_cents"] or 0),
        }
        for row in get_db().execute(
            _TOP_PRODUCTS_SQL,
            (month_start_text, next_month_start_text),
        ).fetchall()
    ]

    recent_sales = [
        {
            "sale_id": int(row["sale_id"]),
            "sale_time": str(row["sale_time"]),
            "salesperson_name": str(row["salesperson_name"]),
            "total_cents": int(row["total_cents"] or 0),
            "status": str(row["status"]),
        }
        for row in get_db().execute(_RECENT_SALES_SQL).fetchall()
    ]

    return {
        "metrics": metrics,
        "trend": trend,
        "trend_has_data": maximum_cents > 0,
        "top_products": top_products,
        "recent_sales": recent_sales,
        "data_as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
    }

