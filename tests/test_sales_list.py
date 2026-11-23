"""销售流水列表、筛选、详情与作废测试。"""

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
from init_db import initialize_database


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _connect_path(db_path: Path | str) -> sqlite3.Connection:
    """连接指定 SQLite 数据库并启用外键。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _connect(app: Flask) -> sqlite3.Connection:
    """连接测试应用数据库。"""
    return _connect_path(app.config["DATABASE"])


def _create_test_sale(app: Flask) -> int:
    """在当前临时库创建一笔带待结算、已结算佣金流水的销售。"""
    sale_time = datetime.now(CHINA_TZ).replace(microsecond=0).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    sale_date = sale_time[:10]
    conn = _connect(app)
    try:
        products = conn.execute(
            "SELECT product_id, min_price_cents FROM PRODUCT "
            "WHERE product_id IN (1, 2) ORDER BY product_id"
        ).fetchall()
        assert len(products) == 2
        first_price = int(products[0]["min_price_cents"]) + 100
        second_price = int(products[1]["min_price_cents"]) + 50
        first_line = 2 * first_price
        second_line = second_price
        total_cents = first_line + second_line

        with conn:
            cursor = conn.execute(
                "INSERT INTO SALE "
                "(shift_id, import_batch_id, sale_time, client_txn_id, "
                "total_cents, status) "
                "VALUES (1, NULL, ?, ?, ?, '已计佣')",
                (sale_time, f"sales-list-test-{uuid4()}", total_cents),
            )
            sale_id = int(cursor.lastrowid)
            conn.executemany(
                "INSERT INTO SALE_ITEM "
                "(sale_id, line_no, product_id, quantity, "
                "final_unit_price_cents, line_cents) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (sale_id, 1, int(products[0]["product_id"]), 2, first_price, first_line),
                    (sale_id, 2, int(products[1]["product_id"]), 1, second_price, second_line),
                ],
            )
            settlement_cursor = conn.execute(
                "INSERT INTO COMMISSION_SETTLEMENT "
                "(settlement_no, period_start, period_end, scope_desc, status) "
                "VALUES (?, ?, ?, ?, '草稿')",
                (
                    f"TEST-SALES-LIST-{uuid4()}",
                    sale_date,
                    sale_date,
                    "销售流水测试",
                ),
            )
            settlement_id = int(settlement_cursor.lastrowid)
            conn.executemany(
                "INSERT INTO COMMISSION_ENTRY "
                "(sale_id, line_no, rule_id, settlement_id, base_cents, "
                "commission_cents, status) "
                "VALUES (?, ?, 1, ?, ?, ?, ?)",
                [
                    (sale_id, 1, None, first_line, 111, "待结算"),
                    (sale_id, 2, settlement_id, second_line, 222, "已结算"),
                ],
            )
        return sale_id
    finally:
        conn.close()


@pytest.fixture()
def app(tmp_path: Path) -> Flask:
    """创建只含 300 条种子销售的临时应用。"""
    db_path = tmp_path / "foxcore-sales-list.db"
    initialize_database(db_path)
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


def _total_count(body: str) -> int:
    """从列表页读取服务端统计总数。"""
    match = re.search(r'data-total-count="(\d+)"', body)
    assert match is not None
    return int(match.group(1))


def _sale_links(body: str) -> set[int]:
    """从列表页提取详情链接中的销售编号。"""
    return {int(value) for value in re.findall(r'href="/sales/(\d+)"', body)}


def _other_salesperson_sale_id(app: Flask) -> int:
    """返回一笔不属于 sales01 的种子销售编号。"""
    conn = _connect(app)
    try:
        row = conn.execute(
            "SELECT s.sale_id FROM SALE AS s "
            "JOIN SHIFT AS sh ON sh.shift_id = s.shift_id "
            "WHERE sh.salesperson_id <> 1 "
            "ORDER BY s.sale_id LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row["sale_id"])


def test_admin_sales_list_returns_200_and_seed_total_300(
    client: FlaskClient,
) -> None:
    """合伙人列表应返回 200，临时库总数为种子数据的 300 条。"""
    _login(client, "admin")
    response = client.get("/sales")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "销售流水" in body
    assert _total_count(body) == 300


def test_salesperson_list_only_contains_own_sales(
    app: Flask,
    client: FlaskClient,
) -> None:
    """销售顾问列表由服务端强制只返回自己的销售，并隐藏顾问筛选。"""
    conn = _connect(app)
    try:
        own_ids = {
            int(row["sale_id"])
            for row in conn.execute(
                "SELECT s.sale_id FROM SALE AS s "
                "JOIN SHIFT AS sh ON sh.shift_id = s.shift_id "
                "WHERE sh.salesperson_id = 1"
            ).fetchall()
        }
    finally:
        conn.close()
    assert own_ids

    _login(client, "sales01")
    response = client.get("/sales")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert _sale_links(body)
    assert _sale_links(body).issubset(own_ids)
    assert _total_count(body) == len(own_ids)
    assert _other_salesperson_sale_id(app) not in _sale_links(body)
    assert 'id="salesperson_id"' not in body


def test_status_void_filter_returns_30_rows(client: FlaskClient) -> None:
    """状态筛选作废应返回种子数据中的 30 条作废销售。"""
    _login(client, "admin")
    response = client.get("/sales", query_string={"status": "作废"})
    assert response.status_code == 200
    assert _total_count(response.get_data(as_text=True)) == 30


def test_date_range_filter_returns_database_count(
    app: Flask,
    client: FlaskClient,
) -> None:
    """起止日期筛选结果应与临时库同口径统计完全一致。"""
    conn = _connect(app)
    try:
        sale_date = conn.execute(
            "SELECT substr(sale_time, 1, 10) FROM SALE ORDER BY sale_id LIMIT 1"
        ).fetchone()[0]
        expected = int(
            conn.execute(
                "SELECT COUNT(*) FROM SALE WHERE substr(sale_time, 1, 10) = ?",
                (sale_date,),
            ).fetchone()[0]
        )
    finally:
        conn.close()

    _login(client, "admin")
    response = client.get(
        "/sales",
        query_string={"date_from": sale_date, "date_to": sale_date},
    )
    assert response.status_code == 200
    assert _total_count(response.get_data(as_text=True)) == expected


def test_partner_event_salesperson_and_sale_no_filters(
    app: Flask,
    client: FlaskClient,
) -> None:
    """合伙人活动、销售顾问和精确单号筛选均应生效。"""
    sale_id = _create_test_sale(app)
    conn = _connect(app)
    try:
        event_id = conn.execute(
            "SELECT e.event_id FROM SALE AS s "
            "JOIN SHIFT AS sh ON sh.shift_id = s.shift_id "
            "JOIN BOOTH AS b ON b.booth_id = sh.booth_id "
            "JOIN EVENT AS e ON e.event_id = b.event_id "
            "WHERE s.sale_id = ?",
            (sale_id,),
        ).fetchone()[0]
        event_expected = int(
            conn.execute(
                "SELECT COUNT(*) FROM SALE AS s "
                "JOIN SHIFT AS sh ON sh.shift_id = s.shift_id "
                "JOIN BOOTH AS b ON b.booth_id = sh.booth_id "
                "JOIN EVENT AS e ON e.event_id = b.event_id "
                "WHERE e.event_id = ?",
                (event_id,),
            ).fetchone()[0]
        )
        salesperson_expected = int(
            conn.execute(
                "SELECT COUNT(*) FROM SALE AS s "
                "JOIN SHIFT AS sh ON sh.shift_id = s.shift_id "
                "WHERE sh.salesperson_id = 1"
            ).fetchone()[0]
        )
    finally:
        conn.close()

    _login(client, "admin")
    event_response = client.get("/sales", query_string={"event_id": event_id})
    assert _total_count(event_response.get_data(as_text=True)) == event_expected

    salesperson_response = client.get(
        "/sales",
        query_string={"salesperson_id": 1},
    )
    assert _total_count(
        salesperson_response.get_data(as_text=True)
    ) == salesperson_expected

    sale_response = client.get(
        "/sales",
        query_string={"sale_no": f"S{sale_id}"},
    )
    sale_body = sale_response.get_data(as_text=True)
    assert _total_count(sale_body) == 1
    assert _sale_links(sale_body) == {sale_id}


def test_detail_displays_items_and_commission(
    app: Flask,
    client: FlaskClient,
) -> None:
    """详情页应显示销售明细、佣金规则、金额和状态。"""
    sale_id = _create_test_sale(app)
    conn = _connect(app)
    try:
        products = conn.execute(
            "SELECT p.product_code, p.name FROM SALE_ITEM AS si "
            "JOIN PRODUCT AS p ON p.product_id = si.product_id "
            "WHERE si.sale_id = ? ORDER BY si.line_no",
            (sale_id,),
        ).fetchall()
    finally:
        conn.close()

    _login(client, "admin")
    response = client.get(f"/sales/{sale_id}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert f"S{sale_id}" in body
    for product in products:
        assert product["product_code"] in body
        assert product["name"] in body
    assert "R-GLOBAL-RATE" in body
    assert "1.11" in body
    assert "2.22" in body
    assert "待结算" in body
    assert "已结算" in body


def test_salesperson_cannot_view_other_sale_detail(
    app: Flask,
    client: FlaskClient,
) -> None:
    """销售顾问访问他人销售详情时由服务层拒绝并返回 403。"""
    _login(client, "sales01")
    response = client.get(f"/sales/{_other_salesperson_sale_id(app)}")
    assert response.status_code == 403


def test_void_success_updates_sale_and_only_reverses_pending_entries(
    app: Flask,
    client: FlaskClient,
) -> None:
    """作废成功应写全审计字段，仅把待结算佣金改为已冲减。"""
    sale_id = _create_test_sale(app)
    reason = "顾客取消订单"
    _login(client, "admin")
    response = client.post(
        f"/sales/{sale_id}/void",
        data={"void_reason": reason},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "已作废" in response.get_data(as_text=True)

    conn = _connect(app)
    try:
        sale = conn.execute(
            "SELECT status, void_reason, void_by, void_at "
            "FROM SALE WHERE sale_id = ?",
            (sale_id,),
        ).fetchone()
        entries = conn.execute(
            "SELECT line_no, status FROM COMMISSION_ENTRY "
            "WHERE sale_id = ? ORDER BY line_no",
            (sale_id,),
        ).fetchall()
    finally:
        conn.close()

    assert sale is not None
    assert sale["status"] == "作废"
    assert sale["void_reason"] == reason
    assert int(sale["void_by"]) == 1
    assert sale["void_at"]
    assert [(int(row["line_no"]), row["status"]) for row in entries] == [
        (1, "已冲减"),
        (2, "已结算"),
    ]


def test_empty_void_reason_is_rejected_without_changes(
    app: Flask,
    client: FlaskClient,
) -> None:
    """空作废原因应拒绝，且销售、审计字段和佣金状态均不变化。"""
    sale_id = _create_test_sale(app)
    _login(client, "admin")
    response = client.post(
        f"/sales/{sale_id}/void",
        data={"void_reason": "   "},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "作废原因不能为空" in response.get_data(as_text=True)

    conn = _connect(app)
    try:
        sale = conn.execute(
            "SELECT status, void_reason, void_by, void_at "
            "FROM SALE WHERE sale_id = ?",
            (sale_id,),
        ).fetchone()
        entries = conn.execute(
            "SELECT status FROM COMMISSION_ENTRY "
            "WHERE sale_id = ? ORDER BY line_no",
            (sale_id,),
        ).fetchall()
    finally:
        conn.close()

    assert sale is not None
    assert sale["status"] == "已计佣"
    assert sale["void_reason"] is None
    assert sale["void_by"] is None
    assert sale["void_at"] is None
    assert [row["status"] for row in entries] == ["待结算", "已结算"]


def test_pagination_pages_one_and_two_do_not_overlap(
    client: FlaskClient,
) -> None:
    """第 1、2 页各 20 条且销售编号集合不重复。"""
    _login(client, "admin")
    first = client.get("/sales", query_string={"page": 1})
    second = client.get("/sales", query_string={"page": 2})
    assert first.status_code == second.status_code == 200

    first_ids = _sale_links(first.get_data(as_text=True))
    second_ids = _sale_links(second.get_data(as_text=True))
    assert len(first_ids) == 20
    assert len(second_ids) == 20
    assert first_ids.isdisjoint(second_ids)


def test_commission_total_excludes_reversed_and_defaults_to_zero(
    app: Flask,
    client: FlaskClient,
) -> None:
    """佣金合计排除已冲减；没有 entry 的销售显示 0.00。"""
    sale_id = _create_test_sale(app)
    conn = _connect(app)
    try:
        with conn:
            conn.execute(
                "UPDATE COMMISSION_ENTRY SET status = '已冲减' "
                "WHERE sale_id = ? AND line_no = 1",
                (sale_id,),
            )
    finally:
        conn.close()

    _login(client, "admin")
    reversed_response = client.get(
        "/sales",
        query_string={"sale_no": f"S{sale_id}"},
    )
    reversed_body = reversed_response.get_data(as_text=True)
    assert f'data-sale-id="{sale_id}" data-commission-cents="222"' in reversed_body
    assert "¥ 2.22" in reversed_body

    no_entry_response = client.get("/sales", query_string={"sale_no": "S1"})
    no_entry_body = no_entry_response.get_data(as_text=True)
    assert 'data-sale-id="1" data-commission-cents="0"' in no_entry_body
    assert "¥ 0.00" in no_entry_body

