"""数据库层回归测试。"""

from __future__ import annotations

from pathlib import Path
import sqlite3

from init_db import TABLE_NAMES, initialize_database
from scripts.seed import EXPECTED_SEED_COUNTS, seed_database


def _connect(db_path: Path) -> sqlite3.Connection:
    """测试连接同样显式开启外键约束。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    return conn


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """读取全部业务表行数。"""
    return {
        table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in TABLE_NAMES
    }


def test_schema_has_exactly_16_tables_and_foreign_keys(tmp_path: Path) -> None:
    """--no-seed 应只建出 16 张空表，外键约束必须可用。"""
    db_path = tmp_path / "foxcore-schema.db"
    stats = initialize_database(db_path, include_seed=False)

    assert tuple(stats) == TABLE_NAMES
    assert len(stats) == 16
    assert all(row_count == 0 for _, row_count in stats.values())

    conn = _connect(db_path)
    try:
        table_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
        )
        assert table_count == 16
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        with conn:
            try:
                conn.execute(
                    "INSERT INTO EVENT "
                    "(venue_id, event_code, event_name, event_type, start_date, end_date) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (999, "BAD-FK", "非法外键活动", "音乐节", "2026-01-01", "2026-01-02"),
                )
            except sqlite3.IntegrityError:
                pass
            else:
                raise AssertionError("外键约束未拦截不存在的场地")
    finally:
        conn.close()


def test_seed_counts_totals_statuses_and_idempotence(tmp_path: Path) -> None:
    """种子数据完整、金额一致，并且重复执行不新增记录。"""
    db_path = tmp_path / "foxcore-seed.db"
    initialize_database(db_path, include_seed=True)

    conn = _connect(db_path)
    try:
        counts = _table_counts(conn)
        assert counts == EXPECTED_SEED_COUNTS
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        status_counts = {
            row["status"]: int(row["count"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM SALE GROUP BY status"
            )
        }
        assert status_counts == {"作废": 30, "已登记": 255, "待计佣": 15}
        assert "已计佣" not in status_counts

        mismatched_totals = int(
            conn.execute(
                "SELECT COUNT(*) FROM SALE AS s "
                "WHERE s.total_cents <> ("
                "SELECT COALESCE(SUM(i.line_cents), 0) "
                "FROM SALE_ITEM AS i WHERE i.sale_id = s.sale_id)"
            ).fetchone()[0]
        )
        assert mismatched_totals == 0

        assert int(conn.execute("SELECT COUNT(DISTINCT client_txn_id) FROM SALE").fetchone()[0]) == 300
        assert int(conn.execute("SELECT COUNT(*) FROM SALE_ITEM").fetchone()[0]) == 600
        assert int(conn.execute("SELECT COUNT(*) FROM COMMISSION_ENTRY").fetchone()[0]) == 0
        assert int(conn.execute("SELECT COUNT(*) FROM COMMISSION_SETTLEMENT").fetchone()[0]) == 0

        before = _table_counts(conn)
        after = seed_database(conn)
        assert after == before == EXPECTED_SEED_COUNTS
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()