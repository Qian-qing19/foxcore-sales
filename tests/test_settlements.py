"""佣金结算生成、列表、详情与确认测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from flask import Flask
from flask.testing import FlaskClient
import pytest

from app import create_app
from app.db import get_db
from app.services import settlement_service
from init_db import initialize_database
from scripts.backfill_commissions import backfill_commissions


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _connect_path(db_path: Path | str) -> sqlite3.Connection:
    """连接指定数据库并启用行工厂与外键约束。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _connect(app: Flask) -> sqlite3.Connection:
    """连接测试应用配置的数据库。"""
    return _connect_path(app.config["DATABASE"])


def _insert_reversed_s301(conn: sqlite3.Connection) -> int:
    """在种子销售之后创建 S301，并录入两条已冲减佣金。"""
    shift = conn.execute(
        "SELECT shift_id FROM SHIFT ORDER BY shift_id LIMIT 1"
    ).fetchone()
    products = conn.execute(
        "SELECT product_id, min_price_cents FROM PRODUCT "
        "ORDER BY product_id LIMIT 2"
    ).fetchall()
    assert shift is not None
    assert len(products) == 2

    sale_time = datetime.now(CHINA_TZ).replace(microsecond=0).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    prices = [int(product["min_price_cents"]) + 100 for product in products]
    total_cents = sum(prices)
    with conn:
        cursor = conn.execute(
            "INSERT INTO SALE "
            "(shift_id, import_batch_id, sale_time, client_txn_id, total_cents, "
            "status, void_reason, void_by, void_at) "
            "VALUES (?, NULL, ?, ?, ?, '作废', ?, 1, ?)",
            (
                int(shift["shift_id"]),
                sale_time,
                f"s301-reversed-{uuid4()}",
                total_cents,
                "D6-B 测试作废",
                sale_time,
            ),
        )
        sale_id = int(cursor.lastrowid)
        assert sale_id == 301
        conn.executemany(
            "INSERT INTO SALE_ITEM "
            "(sale_id, line_no, product_id, quantity, final_unit_price_cents, line_cents) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            [
                (sale_id, line_no, int(product["product_id"]), price, price)
                for line_no, (product, price) in enumerate(zip(products, prices), 1)
            ],
        )
        conn.executemany(
            "INSERT INTO COMMISSION_ENTRY "
            "(sale_id, line_no, rule_id, settlement_id, base_cents, "
            "commission_cents, status) "
            "VALUES (?, ?, 1, NULL, ?, ?, '已冲减')",
            [
                (sale_id, 1, prices[0], 777),
                (sale_id, 2, prices[1], 888),
            ],
        )
    return sale_id


def _insert_pending_sale(app: Flask) -> int:
    """新增一笔带一条待结算佣金的销售，用于确认后继续生成测试。"""
    sale_time = datetime.now(CHINA_TZ).replace(microsecond=0).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn = _connect(app)
    try:
        shift = conn.execute(
            "SELECT shift_id FROM SHIFT ORDER BY shift_id LIMIT 1"
        ).fetchone()
        product = conn.execute(
            "SELECT product_id, min_price_cents FROM PRODUCT "
            "ORDER BY product_id LIMIT 1"
        ).fetchone()
        assert shift is not None
        assert product is not None
        unit_price = int(product["min_price_cents"]) + 100
        with conn:
            cursor = conn.execute(
                "INSERT INTO SALE "
                "(shift_id, import_batch_id, sale_time, client_txn_id, total_cents, "
                "status, void_reason, void_by, void_at) "
                "VALUES (?, NULL, ?, ?, ?, '已计佣', NULL, NULL, NULL)",
                (
                    int(shift["shift_id"]),
                    sale_time,
                    f"pending-{uuid4()}",
                    unit_price,
                ),
            )
            sale_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO SALE_ITEM "
                "(sale_id, line_no, product_id, quantity, final_unit_price_cents, line_cents) "
                "VALUES (?, 1, ?, 1, ?, ?)",
                (sale_id, int(product["product_id"]), unit_price, unit_price),
            )
            conn.execute(
                "INSERT INTO COMMISSION_ENTRY "
                "(sale_id, line_no, rule_id, settlement_id, base_cents, "
                "commission_cents, status) "
                "VALUES (?, 1, 1, NULL, ?, 123, '待结算')",
                (sale_id, unit_price),
            )
        return sale_id
    finally:
        conn.close()


@pytest.fixture()
def app(tmp_path: Path) -> Flask:
    """创建含 540 条待结算佣金及 S301 冲减佣金的临时应用。"""
    db_path = tmp_path / "foxcore-settlements.db"
    initialize_database(db_path)
    conn = _connect_path(db_path)
    try:
        processed, created, pending = backfill_commissions(conn)
        assert (processed, created, pending) == (270, 540, 0)
        assert _insert_reversed_s301(conn) == 301
    finally:
        conn.close()
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret-key",
            "DATABASE": str(db_path),
        }
    )


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    """提供 Flask 测试客户端。"""
    return app.test_client()


def _login(client: FlaskClient, username: str, password: str = "123456"):
    """登录指定测试账号。"""
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def _period(app: Flask) -> tuple[str, str]:
    """返回覆盖临时库全部销售的最短日期区间。"""
    conn = _connect(app)
    try:
        row = conn.execute(
            "SELECT MIN(substr(sale_time, 1, 10)) AS start_date, "
            "MAX(substr(sale_time, 1, 10)) AS end_date FROM SALE"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["start_date"] is not None
    return str(row["start_date"]), str(row["end_date"])


def _generate(client: FlaskClient, app: Flask):
    """按临时库日期区间提交生成表单。"""
    start_date, end_date = _period(app)
    return client.post(
        "/settlements/generate",
        data={"period_start": start_date, "period_end": end_date},
        follow_redirects=False,
    )


def _settlement_id(client: FlaskClient, app: Flask) -> int:
    """提交生成请求并返回新草稿编号。"""
    response = _generate(client, app)
    assert response.status_code == 302
    conn = _connect(app)
    try:
        row = conn.execute(
            "SELECT settlement_id FROM COMMISSION_SETTLEMENT "
            "ORDER BY settlement_id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row["settlement_id"])

def test_admin_get_settlements_returns_200(client: FlaskClient) -> None:
    """合伙人可打开结算列表和生成表单。"""
    _login(client, "admin")
    response = client.get("/settlements")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "佣金核算" in body
    assert 'action="/settlements/generate"' in body


def test_generate_draft_has_fixed_counts_and_amount(
    app: Flask,
    client: FlaskClient,
) -> None:
    """生成草稿必须固定为 17 行、541917 分、归属 540 条佣金。"""
    _login(client, "admin")
    response = _generate(client, app)
    assert response.status_code == 302

    conn = _connect(app)
    try:
        settlement = conn.execute(
            "SELECT settlement_id, settlement_no, status "
            "FROM COMMISSION_SETTLEMENT"
        ).fetchone()
        assert settlement is not None
        settlement_id = int(settlement["settlement_id"])
        assert settlement["status"] == "草稿"
        assert re.fullmatch(r"ST-\d{8}-\d{2}", settlement["settlement_no"])
        assert conn.execute(
            "SELECT COUNT(*) FROM COMMISSION_SETTLEMENT"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM COMMISSION_SETTLEMENT_LINE "
            "WHERE settlement_id = ?",
            (settlement_id,),
        ).fetchone()[0] == 17
        assert conn.execute(
            "SELECT SUM(commission_total_cents) "
            "FROM COMMISSION_SETTLEMENT_LINE WHERE settlement_id = ?",
            (settlement_id,),
        ).fetchone()[0] == 541_917
        assert conn.execute(
            "SELECT COUNT(*) FROM COMMISSION_ENTRY "
            "WHERE settlement_id = ? AND status = '待结算'",
            (settlement_id,),
        ).fetchone()[0] == 540
        s301 = conn.execute(
            "SELECT status, settlement_id FROM COMMISSION_ENTRY "
            "WHERE sale_id = 301 ORDER BY line_no"
        ).fetchall()
    finally:
        conn.close()

    assert [row["status"] for row in s301] == ["已冲减", "已冲减"]
    assert [row["settlement_id"] for row in s301] == [None, None]
    list_response = client.get("/settlements")
    assert list_response.status_code == 200
    assert "5419.17" in list_response.get_data(as_text=True)


def test_empty_period_is_rejected_without_settlement(
    client: FlaskClient,
) -> None:
    """空期间应显示中文提示且不生成结算单。"""
    _login(client, "admin")
    response = client.post(
        "/settlements/generate",
        data={"period_start": "1900-01-01", "period_end": "1900-01-02"},
    )
    assert response.status_code == 200
    assert "没有可结算的佣金流水" in response.get_data(as_text=True)


def test_invalid_period_order_is_rejected(
    app: Flask,
    client: FlaskClient,
) -> None:
    """开始日期晚于结束日期时应拒绝生成。"""
    _login(client, "admin")
    response = client.post(
        "/settlements/generate",
        data={"period_start": "2026-12-31", "period_end": "2026-01-01"},
    )
    assert response.status_code == 200
    assert "不能晚于" in response.get_data(as_text=True)
    conn = _connect(app)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM COMMISSION_SETTLEMENT"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_confirm_marks_all_entries_settled(
    app: Flask,
    client: FlaskClient,
) -> None:
    """确认后单头写入确认人时间，关联 540 条佣金全部已结算。"""
    _login(client, "admin")
    settlement_id = _settlement_id(client, app)
    response = client.post(
        f"/settlements/{settlement_id}/confirm",
        follow_redirects=False,
    )
    assert response.status_code == 302

    conn = _connect(app)
    try:
        settlement = conn.execute(
            "SELECT status, confirmed_by, confirmed_at "
            "FROM COMMISSION_SETTLEMENT WHERE settlement_id = ?",
            (settlement_id,),
        ).fetchone()
        assert settlement is not None
        assert settlement["status"] == "已确认"
        assert settlement["confirmed_by"] == 1
        assert settlement["confirmed_at"]
        assert conn.execute(
            "SELECT COUNT(*) FROM COMMISSION_ENTRY "
            "WHERE settlement_id = ? AND status = '已结算'",
            (settlement_id,),
        ).fetchone()[0] == 540
    finally:
        conn.close()


def test_confirmed_settlement_cannot_be_confirmed_again(
    app: Flask,
    client: FlaskClient,
) -> None:
    """已确认结算单再次确认应返回中文提示且不改状态。"""
    _login(client, "admin")
    settlement_id = _settlement_id(client, app)
    first = client.post(f"/settlements/{settlement_id}/confirm")
    assert first.status_code == 302

    conn = _connect(app)
    try:
        before = conn.execute(
            "SELECT confirmed_at FROM COMMISSION_SETTLEMENT "
            "WHERE settlement_id = ?",
            (settlement_id,),
        ).fetchone()
    finally:
        conn.close()
    assert before is not None

    second = client.post(f"/settlements/{settlement_id}/confirm")
    assert second.status_code == 200
    assert "不能重复确认" in second.get_data(as_text=True)
    conn = _connect(app)
    try:
        after = conn.execute(
            "SELECT status, confirmed_at FROM COMMISSION_SETTLEMENT "
            "WHERE settlement_id = ?",
            (settlement_id,),
        ).fetchone()
    finally:
        conn.close()
    assert after is not None
    assert after["status"] == "已确认"
    assert after["confirmed_at"] == before["confirmed_at"]


def test_existing_draft_blocks_new_generation(
    app: Flask,
    client: FlaskClient,
) -> None:
    """全局已有草稿时再次生成应被拒绝。"""
    _login(client, "admin")
    settlement_id = _settlement_id(client, app)
    response = _generate(client, app)
    assert response.status_code == 200
    assert "请先确认现有草稿" in response.get_data(as_text=True)

    conn = _connect(app)
    try:
        rows = conn.execute(
            "SELECT settlement_id, status FROM COMMISSION_SETTLEMENT"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert int(rows[0]["settlement_id"]) == settlement_id
    assert rows[0]["status"] == "草稿"

def test_reversed_s301_entries_are_excluded(
    app: Flask,
    client: FlaskClient,
) -> None:
    """S301 的两条已冲减佣金不得归属新结算单。"""
    _login(client, "admin")
    settlement_id = _settlement_id(client, app)
    conn = _connect(app)
    try:
        rows = conn.execute(
            "SELECT status, settlement_id, commission_cents "
            "FROM COMMISSION_ENTRY WHERE sale_id = 301 ORDER BY line_no"
        ).fetchall()
        linked_count = conn.execute(
            "SELECT COUNT(*) FROM COMMISSION_ENTRY WHERE sale_id = 301 "
            "AND settlement_id = ?",
            (settlement_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert len(rows) == 2
    assert [row["status"] for row in rows] == ["已冲减", "已冲减"]
    assert [row["settlement_id"] for row in rows] == [None, None]
    assert linked_count == 0
    assert sum(int(row["commission_cents"]) for row in rows) == 1_665


def test_salesperson_cannot_access_settlements(client: FlaskClient) -> None:
    """销售顾问访问结算页面应返回 403。"""
    _login(client, "sales01")
    response = client.get("/settlements")
    assert response.status_code == 403


def test_confirmed_amount_stays_frozen_after_new_sale(
    app: Flask,
    client: FlaskClient,
) -> None:
    """确认后新增销售并再次生成，不得改变原结算单金额。"""
    _login(client, "admin")
    settlement_id = _settlement_id(client, app)
    first_confirm = client.post(f"/settlements/{settlement_id}/confirm")
    assert first_confirm.status_code == 302

    conn = _connect(app)
    try:
        original_total = int(
            conn.execute(
                "SELECT SUM(commission_total_cents) "
                "FROM COMMISSION_SETTLEMENT_LINE WHERE settlement_id = ?",
                (settlement_id,),
            ).fetchone()[0]
        )
        original_line_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM COMMISSION_SETTLEMENT_LINE "
                "WHERE settlement_id = ?",
                (settlement_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert original_total == 541_917

    new_sale_id = _insert_pending_sale(app)
    second_generate = _generate(client, app)
    assert second_generate.status_code == 302

    conn = _connect(app)
    try:
        settlements = conn.execute(
            "SELECT settlement_id, status FROM COMMISSION_SETTLEMENT "
            "ORDER BY settlement_id"
        ).fetchall()
        unchanged_total = int(
            conn.execute(
                "SELECT SUM(commission_total_cents) "
                "FROM COMMISSION_SETTLEMENT_LINE WHERE settlement_id = ?",
                (settlement_id,),
            ).fetchone()[0]
        )
        unchanged_line_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM COMMISSION_SETTLEMENT_LINE "
                "WHERE settlement_id = ?",
                (settlement_id,),
            ).fetchone()[0]
        )
        new_entry_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM COMMISSION_ENTRY "
                "WHERE sale_id = ? AND settlement_id IS NOT NULL",
                (new_sale_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert len(settlements) == 2
    assert settlements[0]["status"] == "已确认"
    assert settlements[1]["status"] == "草稿"
    assert unchanged_total == original_total
    assert unchanged_line_count == original_line_count
    assert new_entry_count == 1


def test_settlement_number_conflict_retries_next_sequence(
    app: Flask,
    client: FlaskClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同日单号唯一键冲突时应自动顺延到下一个序号。"""
    _login(client, "admin")
    today_label = datetime.now(CHINA_TZ).strftime("%Y%m%d")
    existing_no = f"ST-{today_label}-01"
    confirmed_at = datetime.now(CHINA_TZ).replace(microsecond=0).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn = _connect(app)
    try:
        with conn:
            conn.execute(
                "INSERT INTO COMMISSION_SETTLEMENT "
                "(settlement_no, period_start, period_end, scope_desc, "
                "status, confirmed_by, confirmed_at) "
                "VALUES (?, '1900-01-01', '1900-01-02', '冲突测试', "
                "'已确认', 1, ?)",
                (existing_no, confirmed_at),
            )
    finally:
        conn.close()

    def conflict_sequence(_db: sqlite3.Connection, _prefix: str) -> int:
        """强制第一次尝试使用冲突序号 01。"""
        return 1

    monkeypatch.setattr(
        settlement_service,
        "_next_settlement_sequence",
        conflict_sequence,
    )
    response = _generate(client, app)
    assert response.status_code == 302

    conn = _connect(app)
    try:
        draft = conn.execute(
            "SELECT settlement_no FROM COMMISSION_SETTLEMENT "
            "WHERE status = '草稿'"
        ).fetchone()
    finally:
        conn.close()
    assert draft is not None
    assert draft["settlement_no"] == f"ST-{today_label}-02"


def test_confirmed_detail_hides_confirm_button(
    app: Flask,
    client: FlaskClient,
) -> None:
    """已确认详情页只显示确认信息，不渲染确认按钮。"""
    _login(client, "admin")
    settlement_id = _settlement_id(client, app)
    confirm_response = client.post(f"/settlements/{settlement_id}/confirm")
    assert confirm_response.status_code == 302

    response = client.get(f"/settlements/{settlement_id}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "已确认" in body
    assert "admin" in body
    assert "金额已冻结" in body
    assert f'action="/settlements/{settlement_id}/confirm"' not in body


def test_confirm_rolls_back_header_and_entries_on_failure(
    app: Flask,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认中任一步失败时，单头和全部佣金流水的更新一起回滚。"""
    start_date, end_date = _period(app)
    with app.app_context():
        result = settlement_service.generate_settlement(start_date, end_date)
        settlement_id = int(result["settlement_id"])

        def fail_mark_entries(
            _db: sqlite3.Connection,
            _settlement_id: int,
        ) -> int:
            """模拟确认事务中的中途失败。"""
            raise RuntimeError("forced entry update failure")

        monkeypatch.setattr(
            settlement_service,
            "_mark_entries_settled",
            fail_mark_entries,
        )
        with pytest.raises(RuntimeError, match="forced entry update failure"):
            settlement_service.confirm_settlement(settlement_id, 1)

        db = get_db()
        settlement = db.execute(
            "SELECT status, confirmed_by, confirmed_at "
            "FROM COMMISSION_SETTLEMENT WHERE settlement_id = ?",
            (settlement_id,),
        ).fetchone()
        assert settlement is not None
        assert settlement["status"] == "草稿"
        assert settlement["confirmed_by"] is None
        assert settlement["confirmed_at"] is None
        assert db.execute(
            "SELECT COUNT(*) FROM COMMISSION_ENTRY "
            "WHERE settlement_id = ? AND status = '待结算'",
            (settlement_id,),
        ).fetchone()[0] == 540