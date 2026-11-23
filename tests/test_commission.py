"""佣金匹配、计提与历史补算测试。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from app.services.commission_service import (
    calc_commission,
    create_entries_for_sale,
    match_rule,
)
from init_db import initialize_database
from scripts.backfill_commissions import backfill_commissions


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
SALE_TIME = datetime.now(CHINA_TZ).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
SALE_DATE = SALE_TIME[:10]


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """创建带种子数据的临时数据库连接。"""
    db_path = tmp_path / "foxcore-commission.db"
    initialize_database(db_path)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    finally:
        connection.close()


def _create_sale(
    conn: sqlite3.Connection,
    *,
    salesperson_id: int = 1,
    lines: list[tuple[int, int, int]] | None = None,
    status: str = "待计佣",
) -> int:
    """直接创建一笔用于佣金测试的销售及明细。"""
    sale_lines = lines or [(1, 1, 10_000)]
    shift = conn.execute(
        "SELECT sh.shift_id FROM SHIFT AS sh "
        "WHERE sh.salesperson_id = ? ORDER BY sh.shift_id LIMIT 1",
        (salesperson_id,),
    ).fetchone()
    assert shift is not None

    total_cents = sum(quantity * unit_price for _, quantity, unit_price in sale_lines)
    void_reason = "测试作废" if status == "作废" else None
    void_by = 1 if status == "作废" else None
    void_at = SALE_TIME if status == "作废" else None
    cursor = conn.execute(
        "INSERT INTO SALE "
        "(shift_id, import_batch_id, sale_time, client_txn_id, total_cents, status, "
        "void_reason, void_by, void_at) "
        "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?)",
        (
            int(shift["shift_id"]),
            SALE_TIME,
            f"commission-test-{uuid4()}",
            total_cents,
            status,
            void_reason,
            void_by,
            void_at,
        ),
    )
    sale_id = int(cursor.lastrowid)
    conn.executemany(
        "INSERT INTO SALE_ITEM "
        "(sale_id, line_no, product_id, quantity, final_unit_price_cents, line_cents) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (sale_id, line_no, product_id, quantity, unit_price, quantity * unit_price)
            for line_no, (product_id, quantity, unit_price) in enumerate(sale_lines, 1)
        ],
    )
    return sale_id


def _entry_count(conn: sqlite3.Connection, sale_id: int) -> int:
    """返回指定销售的佣金流水数。"""
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM COMMISSION_ENTRY WHERE sale_id = ?",
            (sale_id,),
        ).fetchone()[0]
    )


def test_product_rule_precedes_global_rule(conn: sqlite3.Connection) -> None:
    """产品规则 priority 40 应优先于 GLOBAL 规则 priority 20。"""
    rule = match_rule(conn, product_id=1, salesperson_id=2, event_type="其他", sale_date=SALE_DATE)
    assert rule is not None
    assert rule["rule_code"] == "R-PRODUCT-001"
    assert rule["priority"] == 40


def test_salesperson_rule_precedes_product_rule(conn: sqlite3.Connection) -> None:
    """顾问规则 priority 50 应优先于产品规则 priority 40。"""
    rule = match_rule(conn, product_id=1, salesperson_id=1, event_type="其他", sale_date=SALE_DATE)
    assert rule is not None
    assert rule["rule_code"] == "R-SALESPERSON-TOP"
    assert rule["priority"] == 50


def test_expired_rule_is_not_matched(conn: sqlite3.Connection) -> None:
    """高优先级但已过期的规则不得参与匹配。"""
    with conn:
        conn.execute(
            "INSERT INTO COMMISSION_RULE "
            "(rule_code, version_no, dimension, event_type_value, calc_method, rate, "
            "fixed_per_unit_cents, priority, effective_from, effective_to, status, created_by) "
            "VALUES (?, 1, 'GLOBAL', NULL, '按比例', ?, NULL, 999, ?, ?, '有效', 1)",
            ("TEST-EXPIRED", "0.5", "2020-01-01", "2020-12-31"),
        )

    rule = match_rule(conn, product_id=1, salesperson_id=2, event_type="其他", sale_date=SALE_DATE)
    assert rule is not None
    assert rule["rule_code"] == "R-PRODUCT-001"


def test_rate_commission_rounds_half_up() -> None:
    """22100 分按 0.06 计提应得到 1326 分。"""
    rule = {"calc_method": "按比例", "rate": Decimal("0.06")}
    assert calc_commission(rule, base_cents=22_100, quantity=1) == 1_326


def test_fixed_commission_uses_quantity() -> None:
    """300 分/件、2 件应计提 600 分。"""
    rule = {"calc_method": "按件固定额", "fixed_per_unit_cents": 300}
    assert calc_commission(rule, base_cents=22_100, quantity=2) == 600


def test_no_rule_keeps_sale_pending_without_entry(conn: sqlite3.Connection) -> None:
    """无匹配规则时返回 None，销售保持待计佣且不生成 entry。"""
    with conn:
        conn.execute("UPDATE COMMISSION_RULE SET status = '停用'")
        sale_id = _create_sale(conn, salesperson_id=1, status="待计佣")

    assert match_rule(
        conn,
        product_id=1,
        salesperson_id=1,
        event_type="其他",
        sale_date=SALE_DATE,
    ) is None
    with conn:
        created = create_entries_for_sale(conn, sale_id)

    sale = conn.execute(
        "SELECT status FROM SALE WHERE sale_id = ?",
        (sale_id,),
    ).fetchone()
    assert created == 0
    assert sale is not None
    assert sale["status"] == "待计佣"
    assert _entry_count(conn, sale_id) == 0


def test_void_sale_is_skipped_by_backfill(conn: sqlite3.Connection) -> None:
    """作废销售不参与补算，也不会生成佣金 entry。"""
    with conn:
        conn.execute(
            "UPDATE SALE SET status = '已计佣' "
            "WHERE status IN ('已登记', '待计佣')"
        )
        sale_id = _create_sale(conn, salesperson_id=1, status="作废")

    processed, created, pending = backfill_commissions(conn)

    sale = conn.execute(
        "SELECT status FROM SALE WHERE sale_id = ?",
        (sale_id,),
    ).fetchone()
    assert (processed, created, pending) == (0, 0, 0)
    assert sale is not None
    assert sale["status"] == "作废"
    assert _entry_count(conn, sale_id) == 0


def test_backfill_is_repeatable_without_duplicate_entries(conn: sqlite3.Connection) -> None:
    """重复执行补算不应增加佣金 entry。"""
    with conn:
        conn.execute(
            "UPDATE SALE SET status = '已计佣' "
            "WHERE status IN ('已登记', '待计佣')"
        )
        sale_id = _create_sale(conn, salesperson_id=2, status="待计佣")

    first_result = backfill_commissions(conn)
    first_count = _entry_count(conn, sale_id)
    second_result = backfill_commissions(conn)
    second_count = _entry_count(conn, sale_id)

    assert first_result == (1, 1, 0)
    assert second_result == (0, 0, 0)
    assert first_count == second_count == 1


def test_existing_settlement_entry_is_never_modified(conn: sqlite3.Connection) -> None:
    """已有 settlement_id 的 entry 在补算其他明细时必须保持原值。"""
    with conn:
        sale_id = _create_sale(
            conn,
            salesperson_id=1,
            lines=[(1, 1, 10_000), (2, 1, 10_000)],
            status="待计佣",
        )
        settlement_cursor = conn.execute(
            "INSERT INTO COMMISSION_SETTLEMENT "
            "(settlement_no, period_start, period_end, scope_desc, status) "
            "VALUES (?, ?, ?, ?, '草稿')",
            ("TEST-SETTLEMENT", "2026-11-01", "2026-11-30", "佣金测试"),
        )
        settlement_id = int(settlement_cursor.lastrowid)
        conn.execute(
            "INSERT INTO COMMISSION_ENTRY "
            "(sale_id, line_no, rule_id, settlement_id, base_cents, "
            "commission_cents, status) "
            "VALUES (?, 1, 7, ?, 10000, 900, '待结算')",
            (sale_id, settlement_id),
        )

    with conn:
        created = create_entries_for_sale(conn, sale_id)

    existing = conn.execute(
        "SELECT rule_id, settlement_id, base_cents, commission_cents, status "
        "FROM COMMISSION_ENTRY WHERE sale_id = ? AND line_no = 1",
        (sale_id,),
    ).fetchone()
    sale = conn.execute(
        "SELECT status FROM SALE WHERE sale_id = ?",
        (sale_id,),
    ).fetchone()
    assert created == 1
    assert existing is not None
    assert existing["rule_id"] == 7
    assert existing["settlement_id"] == settlement_id
    assert existing["base_cents"] == 10_000
    assert existing["commission_cents"] == 900
    assert existing["status"] == "待结算"
    assert sale is not None
    assert sale["status"] == "已计佣"
    assert _entry_count(conn, sale_id) == 2