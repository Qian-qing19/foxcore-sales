"""微信小程序 JSON API 测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from uuid import uuid4

from flask import Flask
from flask.testing import FlaskClient
import pytest

from app import create_app
from init_db import initialize_database


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


@pytest.fixture()
def app(tmp_path: Path) -> Flask:
    """为每个 API 测试创建独立临时数据库和应用实例。"""
    db_path = tmp_path / "foxcore-api.db"
    initialize_database(db_path)
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-api-secret-key",
            "DATABASE": str(db_path),
        }
    )


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    """提供 Flask 测试客户端。"""
    return app.test_client()


def _login(
    client: FlaskClient,
    username: str = "sales01",
    password: str = "123456",
):
    """提交 API 登录请求。"""
    return client.post(
        "/api/login",
        json={"username": username, "password": password},
    )


def _token(client: FlaskClient, username: str = "sales01") -> str:
    """登录并返回测试 token。"""
    response = _login(client, username)
    assert response.status_code == 200
    token = response.get_json()["token"]
    assert isinstance(token, str) and token
    return token


def _headers(token: str) -> dict[str, str]:
    """构造 Bearer 认证请求头。"""
    return {"Authorization": f"Bearer {token}"}


def _connect(app: Flask) -> sqlite3.Connection:
    """连接测试数据库并开启外键约束。"""
    conn = sqlite3.connect(app.config["DATABASE"])
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _current_shift_id(app: Flask, salesperson_id: int = 1) -> int:
    """返回指定顾问当前进行中的最新班次编号。"""
    now_text = datetime.now(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect(app)
    try:
        row = conn.execute(
            "SELECT shift_id FROM SHIFT "
            "WHERE salesperson_id = ? AND status = '进行中' "
            "AND start_time <= ? AND end_time >= ? "
            "ORDER BY start_time DESC, shift_id DESC LIMIT 1",
            (salesperson_id, now_text, now_text),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row["shift_id"])


def _active_product(app: Flask) -> sqlite3.Row:
    """返回一个在售产品。"""
    conn = _connect(app)
    try:
        row = conn.execute(
            "SELECT product_id, product_code, name, min_price_cents "
            "FROM PRODUCT WHERE status = '在售' ORDER BY product_id LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row


def _sale_payload(
    shift_id: int,
    product_id: int,
    price_cents: int,
    *,
    client_txn_id: str | None = None,
) -> dict[str, object]:
    """构造整数分销售请求体。"""
    return {
        "client_txn_id": client_txn_id or str(uuid4()),
        "shift_id": shift_id,
        "items": [
            {
                "product_id": product_id,
                "quantity": 2,
                "final_unit_price_cents": price_cents,
            }
        ],
    }


def _sale_count(app: Flask, client_txn_id: str) -> int:
    """按客户端交易编号统计销售主表行数。"""
    conn = _connect(app)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM SALE WHERE client_txn_id = ?",
                (client_txn_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def test_api_login_success_and_failure(client: FlaskClient) -> None:
    """正确凭据返回 token 和用户信息，错误密码返回 401。"""
    success = _login(client)
    assert success.status_code == 200
    body = success.get_json()
    assert body["user"] == {
        "user_id": 2,
        "username": "sales01",
        "role": "销售顾问",
        "salesperson_id": 1,
    }
    assert isinstance(body["token"], str) and body["token"]

    me = client.get("/api/me", headers=_headers(body["token"]))
    assert me.status_code == 200
    assert me.get_json()["username"] == "sales01"

    failure = _login(client, password="wrong-password")
    assert failure.status_code == 401
    assert failure.get_json() == {"error": "用户名或密码错误"}


def test_api_requires_bearer_token(client: FlaskClient) -> None:
    """未携带 token 的请求返回 401 JSON。"""
    response = client.get("/api/me")
    assert response.status_code == 401
    assert response.get_json() == {"error": "请先登录"}


def test_current_shift_and_partner_scope_forbidden(
    app: Flask,
    client: FlaskClient,
) -> None:
    """销售顾问可取当前班次，合伙人访问本人数据接口返回 403。"""
    salesperson_token = _token(client)
    response = client.get(
        "/api/shifts/current",
        headers=_headers(salesperson_token),
    )
    assert response.status_code == 200
    shift = response.get_json()
    assert shift["shift_id"] == _current_shift_id(app)
    assert shift["salesperson_id"] == 1


    partner_token = _token(client, "admin")
    for path in ("/api/shifts/current", "/api/sales", "/api/summary"):
        forbidden = client.get(path, headers=_headers(partner_token))
        assert forbidden.status_code == 403
        assert set(forbidden.get_json()) == {"error"}
        assert isinstance(forbidden.get_json()["error"], str)


def test_active_products_include_integer_min_price_cents(
    client: FlaskClient,
) -> None:
    """产品列表只含在售产品，并返回整数分最低售价。"""
    token = _token(client)
    response = client.get("/api/products", headers=_headers(token))
    assert response.status_code == 200
    products = response.get_json()
    assert isinstance(products, list) and products
    for product in products:
        assert isinstance(product["product_id"], int)
        assert isinstance(product["product_code"], str)
        assert isinstance(product["name"], str)
        assert isinstance(product["min_price_cents"], int)
        assert not isinstance(product["min_price_cents"], bool)
        assert product["min_price_cents"] >= 0


def test_create_sale_writes_sale_items_and_commission(
    app: Flask,
    client: FlaskClient,
) -> None:
    """整数分下单成功并写入销售、明细、佣金及今日汇总。"""
    token = _token(client)
    shift_id = _current_shift_id(app)
    product = _active_product(app)
    price_cents = int(product["min_price_cents"])
    client_txn_id = str(uuid4())
    payload = _sale_payload(
        shift_id,
        int(product["product_id"]),
        price_cents,
        client_txn_id=client_txn_id,
    )

    response = client.post(
        "/api/sales",
        json=payload,
        headers=_headers(token),
    )
    assert response.status_code == 200
    body = response.get_json()
    assert set(body) == {"sale_id", "total_cents", "status"}
    assert body["total_cents"] == price_cents * 2
    assert body["status"] == "已计佣"

    conn = _connect(app)
    try:
        sale = conn.execute(
            "SELECT total_cents, status FROM SALE WHERE sale_id = ?",
            (body["sale_id"],),
        ).fetchone()
        item_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM SALE_ITEM WHERE sale_id = ?",
                (body["sale_id"],),
            ).fetchone()[0]
        )
        commission_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM COMMISSION_ENTRY WHERE sale_id = ?",
                (body["sale_id"],),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert sale is not None
    assert int(sale["total_cents"]) == body["total_cents"]
    assert item_count == 1
    assert commission_count == 1

    summary = client.get("/api/summary", headers=_headers(token))
    assert summary.status_code == 200
    summary_body = summary.get_json()
    assert summary_body["today_sales_cents"] >= body["total_cents"]
    assert summary_body["month_sales_cents"] >= body["total_cents"]
    assert isinstance(summary_body["month_commission_cents"], int)


def test_create_sale_rejects_price_below_minimum(
    app: Flask,
    client: FlaskClient,
) -> None:
    """低于最低售价时返回 400 且整单不落库。"""
    token = _token(client)
    shift_id = _current_shift_id(app)
    product = _active_product(app)
    client_txn_id = str(uuid4())
    payload = _sale_payload(
        shift_id,
        int(product["product_id"]),
        int(product["min_price_cents"]) - 1,
        client_txn_id=client_txn_id,
    )

    response = client.post(
        "/api/sales",
        json=payload,
        headers=_headers(token),
    )
    assert response.status_code == 400
    body = response.get_json()
    assert set(body) == {"error"}
    assert "最低售价" in body["error"]
    assert "需合伙人批准" in body["error"]
    assert _sale_count(app, client_txn_id) == 0


def test_duplicate_client_txn_id_is_idempotent(
    app: Flask,
    client: FlaskClient,
) -> None:
    """相同 client_txn_id 重复提交只入库一次并返回原单。"""
    token = _token(client)
    shift_id = _current_shift_id(app)
    product = _active_product(app)
    client_txn_id = str(uuid4())
    payload = _sale_payload(
        shift_id,
        int(product["product_id"]),
        int(product["min_price_cents"]),
        client_txn_id=client_txn_id,
    )

    first = client.post("/api/sales", json=payload, headers=_headers(token))
    second = client.post("/api/sales", json=payload, headers=_headers(token))
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json() == second.get_json()

    conn = _connect(app)
    try:
        sale_row = conn.execute(
            "SELECT sale_id FROM SALE WHERE client_txn_id = ?",
            (client_txn_id,),
        ).fetchone()
        assert sale_row is not None
        sale_id = int(sale_row["sale_id"])
        item_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM SALE_ITEM WHERE sale_id = ?",
                (sale_id,),
            ).fetchone()[0]
        )
        commission_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM COMMISSION_ENTRY WHERE sale_id = ?",
                (sale_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert item_count == 1
    assert commission_count == 1


def test_sales_list_only_returns_current_salesperson_sales(
    app: Flask,
    client: FlaskClient,
) -> None:
    """销售列表只返回本人记录，不包含其他顾问的最新销售。"""
    token = _token(client)
    now_text = datetime.now(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect(app)
    try:
        with conn:
            cursor = conn.execute(
                "INSERT INTO SALE "
                "(shift_id, import_batch_id, sale_time, client_txn_id, "
                "total_cents, status) "
                "VALUES (?, NULL, ?, ?, ?, '已登记')",
                (2, now_text, f"other-{uuid4()}", 100),
            )
            other_sale_id = int(cursor.lastrowid)
        expected_ids = [
            int(row["sale_id"])
            for row in conn.execute(
                "SELECT s.sale_id FROM SALE AS s "
                "JOIN SHIFT AS sh ON sh.shift_id = s.shift_id "
                "WHERE sh.salesperson_id = 1 "
                "ORDER BY s.sale_time DESC, s.sale_id DESC LIMIT 20"
            ).fetchall()
        ]
    finally:
        conn.close()

    response = client.get("/api/sales?page=1", headers=_headers(token))
    assert response.status_code == 200
    body = response.get_json()
    returned_ids = [int(sale["sale_id"]) for sale in body["sales"]]
    assert body["page_size"] == 20
    assert returned_ids == expected_ids
    assert other_sale_id not in returned_ids
