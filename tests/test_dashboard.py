"""合伙人工作台指标、权限与空数据测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import re
import sqlite3

from flask import Flask
from flask.testing import FlaskClient
from werkzeug.security import generate_password_hash
import pytest

from app import create_app
from app.db import get_db
from app.services import dashboard_service
from init_db import initialize_database
from scripts.backfill_commissions import backfill_commissions


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _connect_path(db_path: Path) -> sqlite3.Connection:
    """连接测试数据库并启用行工厂与外键约束。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@pytest.fixture()
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Flask:
    """创建带种子销售与佣金流水的临时工作台应用。"""
    db_path = tmp_path / "foxcore-dashboard.db"
    initialize_database(db_path)
    conn = _connect_path(db_path)
    try:
        processed, created, pending = backfill_commissions(conn)
        assert processed > 0
        assert created > 0
        assert pending >= 0
        conn.execute(
            "UPDATE COMMISSION_ENTRY SET status = '已结算' "
            "WHERE entry_id = (SELECT MIN(entry_id) FROM COMMISSION_ENTRY)"
        )
        conn.commit()
    finally:
        conn.close()

    fixed_now = datetime.now(CHINA_TZ).replace(microsecond=0)
    monkeypatch.setattr(dashboard_service, "_current_time", lambda: fixed_now)
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret-key",
            "DATABASE": str(db_path),
        }
    )
    application.config["DASHBOARD_NOW"] = fixed_now
    return application


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


def _metric_value(body: str, metric: str) -> str:
    """从工作台 HTML 中读取指定指标卡的元金额文本。"""
    match = re.search(
        rf'data-metric="{re.escape(metric)}">\s*([^<]+)',
        body,
    )
    assert match is not None, f"未找到指标 {metric}"
    return match.group(1)


def _format_cents(cents: int) -> str:
    """把整数分格式化为页面使用的两位小数元文本。"""
    amount = Decimal(int(cents)) / Decimal("100")
    return f"{amount:.2f}"


def _expected_metrics(app: Flask) -> dict[str, int]:
    """使用与页面一致的统计口径直接查询四项指标。"""
    now = app.config["DASHBOARD_NOW"]
    today = now.date()
    tomorrow = today + timedelta(days=1)
    month_start = today.replace(day=1)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    today_start = f"{today.isoformat()} 00:00:00"
    tomorrow_start = f"{tomorrow.isoformat()} 00:00:00"
    month_start_text = f"{month_start.isoformat()} 00:00:00"
    next_month_text = f"{next_month.isoformat()} 00:00:00"

    with app.app_context():
        db = get_db()
        today_sales = int(
            db.execute(
                "SELECT COALESCE(SUM(si.line_cents), 0) "
                "FROM SALE_ITEM AS si "
                "JOIN SALE AS s ON s.sale_id = si.sale_id "
                "WHERE s.status <> '作废' "
                "AND s.sale_time >= ? AND s.sale_time < ?",
                (today_start, tomorrow_start),
            ).fetchone()[0]
        )
        month_sales = int(
            db.execute(
                "SELECT COALESCE(SUM(si.line_cents), 0) "
                "FROM SALE_ITEM AS si "
                "JOIN SALE AS s ON s.sale_id = si.sale_id "
                "WHERE s.status <> '作废' "
                "AND s.sale_time >= ? AND s.sale_time < ?",
                (month_start_text, next_month_text),
            ).fetchone()[0]
        )
        month_commission = int(
            db.execute(
                "SELECT COALESCE(SUM(ce.commission_cents), 0) "
                "FROM COMMISSION_ENTRY AS ce "
                "JOIN SALE AS s ON s.sale_id = ce.sale_id "
                "WHERE s.status <> '作废' "
                "AND ce.status IN ('待结算', '已结算') "
                "AND s.sale_time >= ? AND s.sale_time < ?",
                (month_start_text, next_month_text),
            ).fetchone()[0]
        )
        pending_commission = int(
            db.execute(
                "SELECT COALESCE(SUM(ce.commission_cents), 0) "
                "FROM COMMISSION_ENTRY AS ce "
                "JOIN SALE AS s ON s.sale_id = ce.sale_id "
                "WHERE s.status <> '作废' AND ce.status = '待结算'"
            ).fetchone()[0]
        )

    return {
        "today-sales": today_sales,
        "month-sales": month_sales,
        "month-commission": month_commission,
        "pending-commission": pending_commission,
    }


def test_dashboard_metrics_match_direct_sql(app: Flask, client: FlaskClient) -> None:
    """页面四张指标卡金额与独立 SQL 聚合结果完全一致。"""
    _login(client, "admin")
    response = client.get("/")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    expected = _expected_metrics(app)
    for metric, cents in expected.items():
        assert _metric_value(body, metric) == _format_cents(cents)
    assert "近 7 日销售额趋势" in body
    assert "本月 TOP5 产品" in body
    assert "最近 5 笔销售" in body
    assert f"数据截至 {app.config['DASHBOARD_NOW']:%Y-%m-%d %H:%M:%S}" in body


def test_salesperson_is_redirected_to_new_sale(client: FlaskClient) -> None:
    """销售顾问访问根路径仍跳转销售登记页。"""
    login_response = _login(client, "sales01")
    assert login_response.status_code == 302

    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"] == "/sales/new"


def test_empty_business_data_returns_zeroes_and_empty_states(tmp_path: Path) -> None:
    """无销售业务数据时工作台返回 200、0.00 与暂无数据。"""
    db_path = tmp_path / "foxcore-dashboard-empty.db"
    initialize_database(db_path, include_seed=False)
    conn = _connect_path(db_path)
    try:
        conn.execute(
            "INSERT INTO APP_USER "
            "(username, password_hash, role, salesperson_id, status) "
            "VALUES (?, ?, '合伙人', NULL, '启用')",
            ("admin", generate_password_hash("123456")),
        )
        conn.commit()
    finally:
        conn.close()

    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret-key",
            "DATABASE": str(db_path),
        }
    )
    client = app.test_client()
    _login(client, "admin")
    response = client.get("/")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    for metric in (
        "today-sales",
        "month-sales",
        "month-commission",
        "pending-commission",
    ):
        assert _metric_value(body, metric) == "0.00"
    assert body.count("暂无数据") >= 3

