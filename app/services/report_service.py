"""销售报表聚合、指标计算与 CSV 导出服务。"""

from __future__ import annotations

from collections.abc import Mapping
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from io import StringIO
from typing import Any, Final

from app.db import get_db


CHINA_TZ: Final[timezone] = timezone(timedelta(hours=8), name="Asia/Shanghai")
ZERO_CENTS: Final[Decimal] = Decimal("0")
CENT_DIVISOR: Final[Decimal] = Decimal("100")
MONEY_QUANTUM: Final[Decimal] = Decimal("0.01")
DIMENSION_LABELS: Final[dict[str, str]] = {
    "product": "产品",
    "event": "活动",
    "booth": "摊位",
    "salesperson": "销售顾问",
}
DIMENSION_OPTIONS: Final[tuple[tuple[str, str], ...]] = tuple(
    DIMENSION_LABELS.items()
)


_REPORT_QUERIES: Final[dict[str, str]] = {
    "product": """
        SELECT
            p.product_id AS group_id,
            p.product_code AS code,
            p.name AS name,
            p.category AS detail,
            COALESCE(SUM(si.quantity), 0) AS quantity,
            COALESCE(SUM(si.line_cents), 0) AS net_sales_cents,
            COALESCE(SUM(
                CASE
                    WHEN ce.status IN ('待结算', '已结算')
                    THEN ce.commission_cents
                    ELSE 0
                END
            ), 0) AS commission_cents
        FROM SALE_ITEM AS si
        JOIN SALE AS s ON s.sale_id = si.sale_id
        JOIN PRODUCT AS p ON p.product_id = si.product_id
        LEFT JOIN COMMISSION_ENTRY AS ce
            ON ce.sale_id = si.sale_id AND ce.line_no = si.line_no
        WHERE s.status <> '作废'
          AND s.sale_time >= ?
          AND s.sale_time < ?
        GROUP BY p.product_id, p.product_code, p.name, p.category
        ORDER BY net_sales_cents DESC, p.product_code
    """,
    "event": """
        SELECT
            e.event_id AS group_id,
            e.event_code AS code,
            e.event_name AS name,
            e.event_type AS detail,
            COALESCE(SUM(si.quantity), 0) AS quantity,
            COALESCE(SUM(si.line_cents), 0) AS net_sales_cents,
            COALESCE(SUM(
                CASE
                    WHEN ce.status IN ('待结算', '已结算')
                    THEN ce.commission_cents
                    ELSE 0
                END
            ), 0) AS commission_cents
        FROM SALE_ITEM AS si
        JOIN SALE AS s ON s.sale_id = si.sale_id
        JOIN SHIFT AS sh ON sh.shift_id = s.shift_id
        JOIN BOOTH AS b ON b.booth_id = sh.booth_id
        JOIN EVENT AS e ON e.event_id = b.event_id
        LEFT JOIN COMMISSION_ENTRY AS ce
            ON ce.sale_id = si.sale_id AND ce.line_no = si.line_no
        WHERE s.status <> '作废'
          AND s.sale_time >= ?
          AND s.sale_time < ?
        GROUP BY e.event_id, e.event_code, e.event_name, e.event_type
        ORDER BY net_sales_cents DESC, e.event_code
    """,
    "booth": """
        SELECT
            b.booth_id AS group_id,
            b.booth_code AS code,
            b.booth_code AS name,
            e.event_name AS detail,
            COALESCE(SUM(si.quantity), 0) AS quantity,
            COALESCE(SUM(si.line_cents), 0) AS net_sales_cents,
            COALESCE(SUM(
                CASE
                    WHEN ce.status IN ('待结算', '已结算')
                    THEN ce.commission_cents
                    ELSE 0
                END
            ), 0) AS commission_cents
        FROM SALE_ITEM AS si
        JOIN SALE AS s ON s.sale_id = si.sale_id
        JOIN SHIFT AS sh ON sh.shift_id = s.shift_id
        JOIN BOOTH AS b ON b.booth_id = sh.booth_id
        JOIN EVENT AS e ON e.event_id = b.event_id
        LEFT JOIN COMMISSION_ENTRY AS ce
            ON ce.sale_id = si.sale_id AND ce.line_no = si.line_no
        WHERE s.status <> '作废'
          AND s.sale_time >= ?
          AND s.sale_time < ?
        GROUP BY b.booth_id, b.booth_code, e.event_name
        ORDER BY net_sales_cents DESC, b.booth_code
    """,
    "salesperson": """
        SELECT
            sp.salesperson_id AS group_id,
            sp.emp_code AS code,
            sp.name AS name,
            sp.status AS detail,
            COALESCE(SUM(si.quantity), 0) AS quantity,
            COALESCE(SUM(si.line_cents), 0) AS net_sales_cents,
            COALESCE(SUM(
                CASE
                    WHEN ce.status IN ('待结算', '已结算')
                    THEN ce.commission_cents
                    ELSE 0
                END
            ), 0) AS commission_cents
        FROM SALE_ITEM AS si
        JOIN SALE AS s ON s.sale_id = si.sale_id
        JOIN SHIFT AS sh ON sh.shift_id = s.shift_id
        JOIN BOOTH AS b ON b.booth_id = sh.booth_id
        JOIN EVENT AS e ON e.event_id = b.event_id
        JOIN SALESPERSON AS sp ON sp.salesperson_id = sh.salesperson_id
        LEFT JOIN COMMISSION_ENTRY AS ce
            ON ce.sale_id = si.sale_id AND ce.line_no = si.line_no
        WHERE s.status <> '作废'
          AND s.sale_time >= ?
          AND s.sale_time < ?
        GROUP BY sp.salesperson_id, sp.emp_code, sp.name, sp.status
        ORDER BY net_sales_cents DESC, sp.emp_code
    """,
}


def _clean(value: Any) -> str:
    """把查询参数转换为去除首尾空白的字符串。"""
    if value is None:
        return ""
    return str(value).strip()


def _parse_iso_date(value: Any, label: str) -> date:
    """校验 YYYY-MM-DD 日期并返回日期对象。"""
    text = _clean(value)
    if not text:
        raise ValueError(f"{label}不能为空。")
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{label}格式应为 YYYY-MM-DD。") from None
    if parsed.isoformat() != text:
        raise ValueError(f"{label}格式应为 YYYY-MM-DD。")
    return parsed


def parse_report_filters(
    query: Mapping[str, Any] | None = None,
    *,
    today: date | None = None,
) -> dict[str, str]:
    """校验报表查询条件；缺省时使用本月 1 日至今天。"""
    values = query or {}
    reference_date = today or datetime.now(CHINA_TZ).date()

    if "dimension" in values:
        dimension = _clean(values.get("dimension")).lower()
    else:
        dimension = "product"
    if dimension not in DIMENSION_LABELS:
        raise ValueError("报表维度无效，请选择产品、活动、摊位或销售顾问。")

    start_default = reference_date.replace(day=1).isoformat()
    end_default = reference_date.isoformat()
    if "start_date" in values:
        start_raw = values.get("start_date")
    elif "date_from" in values:
        start_raw = values.get("date_from")
    else:
        start_raw = start_default

    if "end_date" in values:
        end_raw = values.get("end_date")
    elif "date_to" in values:
        end_raw = values.get("date_to")
    else:
        end_raw = end_default

    start_date = _parse_iso_date(start_raw, "开始日期")
    end_date = _parse_iso_date(end_raw, "结束日期")
    if start_date > end_date:
        raise ValueError("结束日期不能早于开始日期。")

    return {
        "dimension": dimension,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }


def calculate_average_unit_price(
    net_sales_cents: int,
    quantity: int,
) -> Decimal:
    """按元计算平均成交价，使用 Decimal 四舍五入保留两位。"""
    if quantity <= 0:
        return Decimal("0.00")
    net_sales_yuan = Decimal(int(net_sales_cents)) / CENT_DIVISOR
    average = net_sales_yuan / Decimal(int(quantity))
    return average.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _normalize_report_row(row: Any) -> dict[str, Any]:
    """把数据库聚合行转换为模板和 CSV 可用的报表行。"""
    quantity = int(row["quantity"] or 0)
    net_sales_cents = int(row["net_sales_cents"] or 0)
    commission_cents = int(row["commission_cents"] or 0)
    return {
        "group_id": int(row["group_id"]),
        "code": str(row["code"]),
        "name": str(row["name"]),
        "detail": str(row["detail"]) if row["detail"] is not None else "",
        "quantity": quantity,
        "net_sales_cents": net_sales_cents,
        "avg_unit_price_yuan": calculate_average_unit_price(
            net_sales_cents,
            quantity,
        ),
        "commission_cents": commission_cents,
    }


def get_report_data(
    query: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """按维度与日期范围返回报表行、合计及数据截至时间。"""
    filters = parse_report_filters(query)
    end_exclusive = (
        date.fromisoformat(filters["end_date"]) + timedelta(days=1)
    ).isoformat()
    rows = get_db().execute(
        _REPORT_QUERIES[filters["dimension"]],
        (
            f"{filters['start_date']} 00:00:00",
            f"{end_exclusive} 00:00:00",
        ),
    ).fetchall()
    report_rows = [_normalize_report_row(row) for row in rows]

    net_sales_total = sum(
        (Decimal(row["net_sales_cents"]) for row in report_rows),
        ZERO_CENTS,
    )
    commission_total = sum(
        (Decimal(row["commission_cents"]) for row in report_rows),
        ZERO_CENTS,
    )
    quantity_total = sum(int(row["quantity"]) for row in report_rows)
    net_sales_cents_total = int(net_sales_total)
    commission_cents_total = int(commission_total)

    return {
        "filters": filters,
        "dimension_label": DIMENSION_LABELS[filters["dimension"]],
        "dimension_options": DIMENSION_OPTIONS,
        "rows": report_rows,
        "row_count": len(report_rows),
        "totals": {
            "quantity": quantity_total,
            "net_sales_cents": net_sales_cents_total,
            "avg_unit_price_yuan": calculate_average_unit_price(
                net_sales_cents_total,
                quantity_total,
            ),
            "commission_cents": commission_cents_total,
        },
        "data_as_of": datetime.now(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }


def _format_cents(cents: int) -> str:
    """把整数分格式化为两位小数元文本。"""
    amount = (Decimal(int(cents)) / CENT_DIVISOR).quantize(
        MONEY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )
    return f"{amount:.2f}"


def build_report_csv(context: Mapping[str, Any]) -> str:
    """把报表上下文转换为以 UTF-8 BOM 开头的 CSV 文本。"""
    output = StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(
        [
            "维度",
            "编号",
            "名称",
            "补充信息",
            "销售数量",
            "净销售额(元)",
            "平均成交价(元)",
            "佣金合计(元)",
        ]
    )

    dimension_label = str(context["dimension_label"])
    rows = context["rows"]
    for row in rows:
        writer.writerow(
            [
                dimension_label,
                row["code"],
                row["name"],
                row["detail"],
                row["quantity"],
                _format_cents(row["net_sales_cents"]),
                f"{row['avg_unit_price_yuan']:.2f}",
                _format_cents(row["commission_cents"]),
            ]
        )

    if rows:
        totals = context["totals"]
        writer.writerow(
            [
                "合计",
                "",
                "",
                "",
                totals["quantity"],
                _format_cents(totals["net_sales_cents"]),
                f"{totals['avg_unit_price_yuan']:.2f}",
                _format_cents(totals["commission_cents"]),
            ]
        )

    return "\ufeff" + output.getvalue()


def export_filename(filters: Mapping[str, Any]) -> str:
    """按维度与起止日期生成安全的 CSV 下载文件名。"""
    dimension = str(filters.get("dimension", ""))
    if dimension not in DIMENSION_LABELS:
        raise ValueError("报表维度无效，无法生成导出文件名。")
    start_label = str(filters.get("start_date", "")).replace("-", "")
    end_label = str(filters.get("end_date", "")).replace("-", "")
    return f"reports_{dimension}_{start_label}_{end_label}.csv"