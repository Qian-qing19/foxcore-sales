"""销售多维报表、权限、口径与 CSV 导出测试。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import sqlite3

from flask import Flask
from flask.testing import FlaskClient
import pytest

from app import create_app
from app.db import get_db
from app.services import report_service
from init_db import initialize_database
from scripts.backfill_commissions import backfill_commissions


REPORT_QUERY = {
    "dimension": "product",
    "start_date": "2026-10-01",
    "end_date": "2026-11-30",
}
EXPECTED_NET_SALES_CENTS = 7_246_450
EXPECTED_COMMISSION_CENTS = 541_917


def _connect_path(db_path: Path | str) -> sqlite3.Connection:
    """连接指定数据库并启用行工厂与外键约束。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _insert_void_s301(conn: sqlite3.Connection) -> int:
    """插入 S301 作废销售及待结算佣金，供报表排除口径测试。"""
    shift = conn.execute(
        "SELECT shift_id FROM SHIFT ORDER BY shift_id LIMIT 1"
    ).fetchone()
    products = conn.execute(
        "SELECT product_id, min_price_cents FROM PRODUCT "
        "ORDER BY product_id LIMIT 2"
    ).fetchall()
    assert shift is not None
    assert len(products) == 2

    quantity = 10
    prices = [int(product["min_price_cents"]) + 50_000 for product in products]
    total_cents = sum(price * quantity for price in prices)
    sale_time = "2026-11-15 12:00:00"
    with conn:
        cursor = conn.execute(
            "INSERT INTO SALE "
            "(shift_id, import_batch_id, sale_time, client_txn_id, total_cents, "
            "status, void_reason, void_by, void_at) "
            "VALUES (?, NULL, ?, 'report-s301', ?, '作废', ?, 1, ?)",
            (
                int(shift["shift_id"]),
                sale_time,
                total_cents,
                "报表测试作废",
                sale_time,
            ),
        )
        sale_id = int(cursor.lastrowid)
        conn.executemany(
            "INSERT INTO SALE_ITEM "
            "(sale_id, line_no, product_id, quantity, final_unit_price_cents, line_cents) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    sale_id,
                    line_no,
                    int(product["product_id"]),
                    quantity,
                    price,
                    price * quantity,
                )
                for line_no, (product, price) in enumerate(
                    zip(products, prices),
                    start=1,
                )
            ],
        )
        conn.executemany(
            "INSERT INTO COMMISSION_ENTRY "
            "(sale_id, line_no, rule_id, settlement_id, base_cents, "
            "commission_cents, status) "
            "VALUES (?, ?, 2, NULL, ?, ?, '待结算')",
            [
                (sale_id, line_no, price * quantity, 500_000)
                for line_no, price in enumerate(prices, start=1)
            ],
        )
    return sale_id


@pytest.fixture()
def app(tmp_path: Path) -> Flask:
    """创建带佣金流水和 S301 作废销售的临时报表应用。"""
    db_path = tmp_path / "foxcore-reports.db"
    initialize_database(db_path)
    conn = _connect_path(db_path)
    try:
        processed, created, pending = backfill_commissions(conn)
        assert (processed, created, pending) == (270, 540, 0)
        assert _insert_void_s301(conn) == 301
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


def test_admin_get_reports_returns_200(client: FlaskClient) -> None:
    """合伙人可打开多维报表页并看到数据截至时间。"""
    _login(client, "admin")
    response = client.get("/reports")
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "销售多维报表" in body
    assert "数据截至" in body


def test_product_report_totals_match_required_reporting(app: Flask) -> None:
    """产品维度合计严格匹配净销售额与佣金口径。"""
    with app.app_context():
        context = report_service.get_report_data(REPORT_QUERY)

    assert context["row_count"] == 28
    assert context["totals"]["net_sales_cents"] == EXPECTED_NET_SALES_CENTS
    assert context["totals"]["commission_cents"] == EXPECTED_COMMISSION_CENTS
    assert isinstance(context["totals"]["avg_unit_price_yuan"], Decimal)
    assert context["totals"]["avg_unit_price_yuan"].as_tuple().exponent == -2


@pytest.mark.parametrize(
    ("dimension", "expected_rows"),
    [
        ("product", 28),
        ("event", 3),
        ("booth", 6),
        ("salesperson", 17),
    ],
)
def test_all_dimensions_return_expected_row_counts(
    app: Flask,
    dimension: str,
    expected_rows: int,
) -> None:
    """四个聚合维度的数据行数符合种子数据分布。"""
    query = {**REPORT_QUERY, "dimension": dimension}
    with app.app_context():
        context = report_service.get_report_data(query)
    assert context["row_count"] == expected_rows


def test_void_sales_including_s301_are_excluded(app: Flask) -> None:
    """种子作废销售和 S301 均不进入净销售额与佣金合计。"""
    with app.app_context():
        s301_net = get_db().execute(
            "SELECT COALESCE(SUM(line_cents), 0) FROM SALE_ITEM "
            "WHERE sale_id = 301"
        ).fetchone()[0]
        context = report_service.get_report_data(REPORT_QUERY)

    assert int(s301_net) > 0
    assert context["totals"]["net_sales_cents"] == EXPECTED_NET_SALES_CENTS
    assert context["totals"]["commission_cents"] == EXPECTED_COMMISSION_CENTS


def test_reversed_commission_is_excluded(app: Flask) -> None:
    """已冲减佣金不计入佣金合计，且不影响净销售额。"""
    with app.app_context():
        before = report_service.get_report_data(REPORT_QUERY)
        db = get_db()
        entry = db.execute(
            "SELECT ce.entry_id, ce.commission_cents "
            "FROM COMMISSION_ENTRY AS ce "
            "JOIN SALE AS s ON s.sale_id = ce.sale_id "
            "WHERE s.status <> '作废' AND ce.status = '待结算' "
            "ORDER BY ce.entry_id LIMIT 1"
        ).fetchone()
        assert entry is not None
        with db:
            db.execute(
                "UPDATE COMMISSION_ENTRY SET status = '已冲减' "
                "WHERE entry_id = ?",
                (int(entry["entry_id"]),),
            )
        after = report_service.get_report_data(REPORT_QUERY)

    excluded_commission = int(entry["commission_cents"])
    assert after["totals"]["net_sales_cents"] == before["totals"]["net_sales_cents"]
    assert (
        after["totals"]["commission_cents"]
        == before["totals"]["commission_cents"] - excluded_commission
    )


def test_empty_period_returns_empty_table_and_200(client: FlaskClient) -> None:
    """空期间返回 200、显示空状态且不渲染零值数据行。"""
    _login(client, "admin")
    response = client.get(
        "/reports",
        query_string={
            "dimension": "product",
            "start_date": "2020-01-01",
            "end_date": "2020-01-02",
        },
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "该条件下没有数据" in body
    assert "data-report-row " not in body
    assert "data-report-row>" not in body


def test_export_csv_has_bom_filename_and_content_type(
    client: FlaskClient,
) -> None:
    """CSV 响应以 BOM 开头，并携带维度与日期文件名。"""
    _login(client, "admin")
    response = client.get("/reports/export", query_string=REPORT_QUERY)
    assert response.status_code == 200
    assert "text/csv" in response.content_type
    assert response.get_data(as_text=True).startswith("\ufeff")
    assert response.data.startswith(b"\xef\xbb\xbf")
    assert (
        "reports_product_20261001_20261130.csv"
        in response.headers["Content-Disposition"]
    )
    assert "佣金合计(元)" in response.get_data(as_text=True)


def test_salesperson_get_reports_returns_403(client: FlaskClient) -> None:
    """销售顾问无权访问合伙人报表。"""
    _login(client, "sales01")
    response = client.get("/reports")
    assert response.status_code == 403


@pytest.mark.parametrize(
    ("query_string", "message"),
    [
        (
            {
                "dimension": "invalid",
                "start_date": "2026-10-01",
                "end_date": "2026-11-30",
            },
            "报表维度无效",
        ),
        (
            {
                "dimension": "product",
                "start_date": "2026-13-01",
                "end_date": "2026-11-30",
            },
            "开始日期格式应为 YYYY-MM-DD",
        ),
    ],
)
def test_invalid_filters_flash_and_fall_back_to_defaults(
    client: FlaskClient,
    query_string: dict[str, str],
    message: str,
) -> None:
    """非法维度或日期返回 200，显示中文提示并回退默认查询条件。"""
    _login(client, "admin")
    response = client.get("/reports", query_string=query_string)
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert message in body
    assert 'data-dimension="product"' in body


def test_parse_report_filters_defaults_to_current_month() -> None:
    """未传条件时默认本月 1 日至参考日期。"""
    filters = report_service.parse_report_filters(
        {},
        today=date(2026, 11, 18),
    )
    assert filters == {
        "dimension": "product",
        "start_date": "2026-11-01",
        "end_date": "2026-11-18",
    }


def test_average_unit_price_uses_decimal_and_handles_zero_quantity() -> None:
    """平均成交价按元保留两位，数量为零时返回 0.00。"""
    expected = report_service.calculate_average_unit_price(1_001, 3)
    assert expected == Decimal("3.34")
    assert report_service.calculate_average_unit_price(0, 0) == Decimal("0.00")