"""佣金结算生成、详情查询与确认服务。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final
import sqlite3

from app.db import get_db


CHINA_TZ: Final[timezone] = timezone(timedelta(hours=8), name="Asia/Shanghai")
SETTLEMENT_NO_RETRY_LIMIT: Final[int] = 100


def _clean(value: Any) -> str:
    """把可选表单值转换为去除首尾空白的字符串。"""
    if value is None:
        return ""
    return str(value).strip()


def _now() -> datetime:
    """返回上海时区当前时间，并去掉微秒。"""
    return datetime.now(CHINA_TZ).replace(microsecond=0)


def _format_datetime(value: datetime) -> str:
    """把时区时间转换为数据库约定的时间字符串。"""
    return value.astimezone(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _parse_period_date(value: Any, label: str) -> date:
    """校验必填的 YYYY-MM-DD 日期并返回日期对象。"""
    text = _clean(value)
    if not text:
        raise ValueError(f"{label}不能为空")
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{label}格式应为 YYYY-MM-DD") from None
    if parsed.isoformat() != text:
        raise ValueError(f"{label}格式应为 YYYY-MM-DD")
    return parsed


def _period_bounds(period_start: Any, period_end: Any) -> tuple[str, str, str, str]:
    """校验结算期间并返回规范日期和含首尾两端的销售时间范围。"""
    start = _parse_period_date(period_start, "期间开始日期")
    end = _parse_period_date(period_end, "期间结束日期")
    if start > end:
        raise ValueError("期间开始日期不能晚于期间结束日期")
    return (
        start.isoformat(),
        end.isoformat(),
        f"{start.isoformat()} 00:00:00",
        f"{end.isoformat()} 23:59:59",
    )


def get_settlements_page(
    form_values: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """返回结算单列表和生成表单需要保留的输入值。"""
    db = get_db()
    rows = db.execute(
        "SELECT cs.settlement_id, cs.settlement_no, cs.period_start, "
        "cs.period_end, cs.scope_desc, cs.status, cs.confirmed_at, "
        "cs.created_at, u.username AS confirmed_by_username, "
        "(SELECT COUNT(*) FROM COMMISSION_SETTLEMENT_LINE AS csl "
        " WHERE csl.settlement_id = cs.settlement_id) AS line_count, "
        "(SELECT COALESCE(SUM(csl.commission_total_cents), 0) "
        " FROM COMMISSION_SETTLEMENT_LINE AS csl "
        " WHERE csl.settlement_id = cs.settlement_id) AS commission_total_cents "
        "FROM COMMISSION_SETTLEMENT AS cs "
        "LEFT JOIN APP_USER AS u ON u.user_id = cs.confirmed_by "
        "ORDER BY cs.created_at DESC, cs.settlement_id DESC"
    ).fetchall()

    settlements = [
        {
            **dict(row),
            "settlement_id": int(row["settlement_id"]),
            "line_count": int(row["line_count"]),
            "commission_total_cents": int(row["commission_total_cents"]),
        }
        for row in rows
    ]
    submitted = form_values or {}
    return {
        "settlements": settlements,
        "form_values": {
            "period_start": _clean(submitted.get("period_start")),
            "period_end": _clean(submitted.get("period_end")),
        },
    }

def _next_settlement_sequence(
    db: sqlite3.Connection,
    prefix: str,
) -> int:
    """查询指定日期前缀下已使用的最大结算单序号。"""
    rows = db.execute(
        "SELECT settlement_no FROM COMMISSION_SETTLEMENT "
        "WHERE settlement_no LIKE ?",
        (f"{prefix}-%",),
    ).fetchall()
    maximum = 0
    marker = f"{prefix}-"
    for row in rows:
        settlement_no = str(row["settlement_no"])
        if not settlement_no.startswith(marker):
            continue
        try:
            maximum = max(maximum, int(settlement_no[len(marker):]))
        except ValueError:
            continue
    return maximum + 1


def _insert_settlement_header(
    db: sqlite3.Connection,
    period_start: str,
    period_end: str,
) -> tuple[int, str]:
    """插入结算单头；单号唯一键冲突时自动顺延序号重试。"""
    date_label = _now().strftime("%Y%m%d")
    prefix = f"ST-{date_label}"
    sequence = _next_settlement_sequence(db, prefix)

    for _attempt in range(SETTLEMENT_NO_RETRY_LIMIT):
        settlement_no = f"{prefix}-{sequence:02d}"
        try:
            cursor = db.execute(
                "INSERT INTO COMMISSION_SETTLEMENT "
                "(settlement_no, period_start, period_end, scope_desc, status) "
                "VALUES (?, ?, ?, ?, '草稿')",
                (settlement_no, period_start, period_end, "全部销售顾问"),
            )
            return int(cursor.lastrowid), settlement_no
        except sqlite3.IntegrityError as exc:
            if "UNIQUE constraint failed: COMMISSION_SETTLEMENT.settlement_no" not in str(exc):
                raise
            sequence += 1

    raise ValueError("结算单号连续冲突，暂时无法生成，请稍后重试")


def generate_settlement(period_start: Any, period_end: Any) -> dict[str, Any]:
    """在单个事务中生成佣金结算草稿并归属待结算佣金流水。"""
    start_text, end_text, start_datetime, end_datetime = _period_bounds(
        period_start,
        period_end,
    )
    db = get_db()

    with db:
        draft = db.execute(
            "SELECT settlement_no FROM COMMISSION_SETTLEMENT "
            "WHERE status = '草稿' ORDER BY settlement_id LIMIT 1"
        ).fetchone()
        if draft is not None:
            raise ValueError(
                f"已存在草稿结算单 {draft['settlement_no']}，请先确认现有草稿"
            )

        entries = db.execute(
            "SELECT ce.entry_id, ce.base_cents, ce.commission_cents, "
            "sh.salesperson_id "
            "FROM COMMISSION_ENTRY AS ce "
            "JOIN SALE AS s ON s.sale_id = ce.sale_id "
            "JOIN SHIFT AS sh ON sh.shift_id = s.shift_id "
            "WHERE ce.status = '待结算' "
            "AND ce.settlement_id IS NULL "
            "AND s.status <> '作废' "
            "AND s.sale_time >= ? AND s.sale_time <= ? "
            "ORDER BY sh.salesperson_id, ce.entry_id",
            (start_datetime, end_datetime),
        ).fetchall()
        if not entries:
            raise ValueError("该期间内没有可结算的佣金流水，未生成结算单")

        aggregates: dict[int, dict[str, int]] = defaultdict(
            lambda: {"net_sales_cents": 0, "commission_total_cents": 0}
        )
        for entry in entries:
            salesperson_id = int(entry["salesperson_id"])
            aggregates[salesperson_id]["net_sales_cents"] += int(entry["base_cents"])
            aggregates[salesperson_id]["commission_total_cents"] += int(
                entry["commission_cents"]
            )

        settlement_id, settlement_no = _insert_settlement_header(
            db,
            start_text,
            end_text,
        )
        line_rows = [
            (
                settlement_id,
                salesperson_id,
                totals["net_sales_cents"],
                totals["commission_total_cents"],
                0,
                0,
            )
            for salesperson_id, totals in sorted(aggregates.items())
        ]
        db.executemany(
            "INSERT INTO COMMISSION_SETTLEMENT_LINE "
            "(settlement_id, salesperson_id, net_sales_cents, "
            "commission_total_cents, pending_cents, adjustment_cents) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            line_rows,
        )

        entry_ids = [int(entry["entry_id"]) for entry in entries]
        affected = 0
        for offset in range(0, len(entry_ids), 900):
            chunk = entry_ids[offset:offset + 900]
            placeholders = ", ".join("?" for _ in chunk)
            cursor = db.execute(
                "UPDATE COMMISSION_ENTRY SET settlement_id = ? "
                f"WHERE entry_id IN ({placeholders}) "
                "AND settlement_id IS NULL AND status = '待结算'",
                (settlement_id, *chunk),
            )
            affected += int(cursor.rowcount)
        if affected != len(entry_ids):
            raise ValueError("佣金流水归属已发生变化，请重新生成结算单")

    return {
        "settlement_id": settlement_id,
        "settlement_no": settlement_no,
        "line_count": len(line_rows),
        "entry_count": len(entry_ids),
        "commission_total_cents": sum(
            totals["commission_total_cents"] for totals in aggregates.values()
        ),
    }

def get_settlement_detail(settlement_id: int) -> dict[str, Any]:
    """查询结算单头、按顾问汇总、异常清单及页面汇总金额。"""
    if settlement_id <= 0:
        raise ValueError("结算单编号无效")

    db = get_db()
    settlement_row = db.execute(
        "SELECT cs.settlement_id, cs.settlement_no, cs.period_start, "
        "cs.period_end, cs.scope_desc, cs.status, cs.confirmed_by, "
        "cs.confirmed_at, cs.created_at, u.username AS confirmed_by_username "
        "FROM COMMISSION_SETTLEMENT AS cs "
        "LEFT JOIN APP_USER AS u ON u.user_id = cs.confirmed_by "
        "WHERE cs.settlement_id = ?",
        (settlement_id,),
    ).fetchone()
    if settlement_row is None:
        raise LookupError("结算单不存在")

    entry_count_rows = db.execute(
        "SELECT sh.salesperson_id, COUNT(*) AS entry_count "
        "FROM COMMISSION_ENTRY AS ce "
        "JOIN SALE AS s ON s.sale_id = ce.sale_id "
        "JOIN SHIFT AS sh ON sh.shift_id = s.shift_id "
        "WHERE ce.settlement_id = ? "
        "GROUP BY sh.salesperson_id",
        (settlement_id,),
    ).fetchall()
    entry_counts = {
        int(row["salesperson_id"]): int(row["entry_count"])
        for row in entry_count_rows
    }

    line_rows = db.execute(
        "SELECT csl.salesperson_id, sp.emp_code, sp.name AS salesperson_name, "
        "csl.net_sales_cents, csl.commission_total_cents, csl.pending_cents, "
        "csl.adjustment_cents "
        "FROM COMMISSION_SETTLEMENT_LINE AS csl "
        "JOIN SALESPERSON AS sp ON sp.salesperson_id = csl.salesperson_id "
        "WHERE csl.settlement_id = ? "
        "ORDER BY sp.emp_code, sp.salesperson_id",
        (settlement_id,),
    ).fetchall()
    lines = [
        {
            **dict(row),
            "salesperson_id": int(row["salesperson_id"]),
            "net_sales_cents": int(row["net_sales_cents"]),
            "commission_total_cents": int(row["commission_total_cents"]),
            "pending_cents": int(row["pending_cents"]),
            "adjustment_cents": int(row["adjustment_cents"]),
            "entry_count": entry_counts.get(int(row["salesperson_id"]), 0),
        }
        for row in line_rows
    ]

    start_datetime = f"{settlement_row['period_start']} 00:00:00"
    end_datetime = f"{settlement_row['period_end']} 23:59:59"
    exception_rows = db.execute(
        "SELECT s.sale_id, s.sale_time, s.client_txn_id, s.total_cents, "
        "s.status, sp.emp_code, sp.name AS salesperson_name, "
        "e.event_name, b.booth_code, "
        "(SELECT COUNT(*) FROM SALE_ITEM AS si "
        " WHERE si.sale_id = s.sale_id) AS item_count "
        "FROM SALE AS s "
        "JOIN SHIFT AS sh ON sh.shift_id = s.shift_id "
        "JOIN SALESPERSON AS sp ON sp.salesperson_id = sh.salesperson_id "
        "JOIN BOOTH AS b ON b.booth_id = sh.booth_id "
        "JOIN EVENT AS e ON e.event_id = b.event_id "
        "WHERE s.status = '待计佣' "
        "AND s.sale_time >= ? AND s.sale_time <= ? "
        "ORDER BY s.sale_time, s.sale_id",
        (start_datetime, end_datetime),
    ).fetchall()
    exceptions = [
        {
            **dict(row),
            "sale_id": int(row["sale_id"]),
            "total_cents": int(row["total_cents"]),
            "item_count": int(row["item_count"]),
        }
        for row in exception_rows
    ]

    settlement = dict(settlement_row)
    settlement["settlement_id"] = int(settlement_row["settlement_id"])
    return {
        "settlement": settlement,
        "lines": lines,
        "exceptions": exceptions,
        "totals": {
            "line_count": len(lines),
            "entry_count": sum(line["entry_count"] for line in lines),
            "net_sales_cents": sum(line["net_sales_cents"] for line in lines),
            "commission_total_cents": sum(
                line["commission_total_cents"] for line in lines
            ),
            "pending_cents": sum(line["pending_cents"] for line in lines),
            "adjustment_cents": sum(line["adjustment_cents"] for line in lines),
        },
    }

def _mark_entries_settled(db: sqlite3.Connection, settlement_id: int) -> int:
    """把指定结算单关联的全部佣金流水标记为已结算。"""
    cursor = db.execute(
        "UPDATE COMMISSION_ENTRY SET status = '已结算' "
        "WHERE settlement_id = ?",
        (settlement_id,),
    )
    return int(cursor.rowcount)


def confirm_settlement(settlement_id: int, confirmed_by: int) -> dict[str, Any]:
    """在单个事务中确认草稿结算单并固化全部佣金流水。"""
    if settlement_id <= 0:
        raise ValueError("结算单编号无效")
    if confirmed_by <= 0:
        raise ValueError("确认人无效")

    confirmed_at = _format_datetime(_now())
    db = get_db()
    with db:
        settlement = db.execute(
            "SELECT settlement_no, status FROM COMMISSION_SETTLEMENT "
            "WHERE settlement_id = ?",
            (settlement_id,),
        ).fetchone()
        if settlement is None:
            raise LookupError("结算单不存在")
        if str(settlement["status"]) != "草稿":
            raise ValueError("结算单已确认，不能重复确认")

        cursor = db.execute(
            "UPDATE COMMISSION_SETTLEMENT "
            "SET status = '已确认', confirmed_by = ?, confirmed_at = ? "
            "WHERE settlement_id = ? AND status = '草稿'",
            (confirmed_by, confirmed_at, settlement_id),
        )
        if int(cursor.rowcount) != 1:
            raise ValueError("结算单状态已变化，请刷新后重试")
        entry_count = _mark_entries_settled(db, settlement_id)

    return {
        "settlement_id": settlement_id,
        "settlement_no": str(settlement["settlement_no"]),
        "status": "已确认",
        "confirmed_by": confirmed_by,
        "confirmed_at": confirmed_at,
        "entry_count": entry_count,
    }