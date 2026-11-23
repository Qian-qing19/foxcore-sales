"""销售登记页面与服务测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import sqlite3
from uuid import uuid4

from flask import Flask
from flask.testing import FlaskClient
import pytest
from werkzeug.datastructures import MultiDict

from app import create_app
from init_db import initialize_database


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


@pytest.fixture()
def app(tmp_path: Path) -> Flask:
    """为每个销售测试创建独立临时数据库和应用实例。"""
    db_path = tmp_path / "foxcore-sales.db"
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


def _connect(app: Flask) -> sqlite3.Connection:
    """连接测试数据库并开启外键约束。"""
    conn = sqlite3.connect(app.config["DATABASE"])
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _current_shift_id(app: Flask, salesperson_id: int = 1) -> int:
    """返回临时库中指定顾问当前最新班次编号。"""
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


def _product(app: Flask, product_code: str) -> sqlite3.Row:
    """按编码读取测试产品。"""
    conn = _connect(app)
    try:
        row = conn.execute(
            "SELECT product_id, product_code, name, min_price_cents, status "
            "FROM PRODUCT WHERE product_code = ?",
            (product_code,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row


def _price_for_cents(cents: int) -> str:
    """把整数分转换为两位小数的元字符串。"""
    return f"{Decimal(cents) / Decimal('100'):.2f}"


def _payload(
    shift_id: int,
    lines: list[tuple[int, str, str]],
    client_txn_id: str | None = None,
) -> MultiDict[str]:
    """构造包含重复明细字段的销售提交表单。"""
    form: MultiDict[str] = MultiDict()
    form.add("client_txn_id", client_txn_id or str(uuid4()))
    form.add("shift_id", str(shift_id))
    for product_id, quantity, price in lines:
        form.add("product_id", str(product_id))
        form.add("quantity", quantity)
        form.add("final_unit_price", price)
    return form


def _sale_by_client_txn_id(app: Flask, client_txn_id: str) -> sqlite3.Row | None:
    """按幂等键查询销售主表。"""
    conn = _connect(app)
    try:
        return conn.execute(
            "SELECT sale_id, shift_id, sale_time, client_txn_id, total_cents, status "
            "FROM SALE WHERE client_txn_id = ?",
            (client_txn_id,),
        ).fetchone()
    finally:
        conn.close()


def test_salesperson_get_new_sale_returns_200_and_current_shift(
    app: Flask,
    client: FlaskClient,
) -> None:
    """销售顾问应看到当前班次的活动、摊位和起止时间。"""
    _login(client, "sales01")
    shift_id = _current_shift_id(app)

    conn = _connect(app)
    try:
        shift = conn.execute(
            "SELECT e.event_name, b.booth_code, sh.start_time, sh.end_time "
            "FROM SHIFT AS sh "
            "JOIN BOOTH AS b ON b.booth_id = sh.booth_id "
            "JOIN EVENT AS e ON e.event_id = b.event_id "
            "WHERE sh.shift_id = ?",
            (shift_id,),
        ).fetchone()
    finally:
        conn.close()
    assert shift is not None

    response = client.get("/sales/new")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "登记销售" in body
    assert shift["event_name"] in body
    assert shift["booth_code"] in body
    assert shift["start_time"] in body
    assert shift["end_time"] in body
    assert f'name="shift_id" value="{shift_id}"' in body


def test_get_new_sale_uses_latest_active_shift(
    app: Flask,
    client: FlaskClient,
) -> None:
    """同一顾问有多条进行中班次时，页面应展示开始时间最新的一条。"""
    now = datetime.now(CHINA_TZ).replace(microsecond=0)
    conn = _connect(app)
    try:
        cursor = conn.execute(
            "INSERT INTO SHIFT "
            "(salesperson_id, booth_id, start_time, end_time, status) "
            "VALUES (?, ?, ?, ?, '进行中')",
            (
                1,
                2,
                (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
                (now + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        latest_shift_id = int(cursor.lastrowid)
        conn.commit()
    finally:
        conn.close()

    _login(client, "sales01")
    response = client.get("/sales/new")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert f'name="shift_id" value="{latest_shift_id}"' in body
    assert "A-02" in body


def test_create_sale_with_two_lines_persists_sale_and_items(
    app: Flask,
    client: FlaskClient,
) -> None:
    """正常提交两行应写入明细，并按顾问优先规则生成两条佣金流水。"""
    product_a = _product(app, "PRODUCT-001")
    product_b = _product(app, "PRODUCT-002")
    price_a_cents = int(product_a["min_price_cents"]) + 100
    price_b_cents = int(product_b["min_price_cents"]) + 50
    shift_id = _current_shift_id(app)
    client_txn_id = str(uuid4())
    form = _payload(
        shift_id,
        [
            (int(product_a["product_id"]), "2", _price_for_cents(price_a_cents)),
            (int(product_b["product_id"]), "3", _price_for_cents(price_b_cents)),
        ],
        client_txn_id,
    )

    _login(client, "sales01")
    response = client.post("/sales/new", data=form, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"] == "/sales/new"

    sale = _sale_by_client_txn_id(app, client_txn_id)
    assert sale is not None
    assert sale["shift_id"] == shift_id
    assert sale["status"] == "已计佣"
    assert sale["total_cents"] == 2 * price_a_cents + 3 * price_b_cents

    conn = _connect(app)
    try:
        items = conn.execute(
            "SELECT line_no, product_id, quantity, final_unit_price_cents, line_cents "
            "FROM SALE_ITEM WHERE sale_id = ? ORDER BY line_no",
            (sale["sale_id"],),
        ).fetchall()
        entries = conn.execute(
            "SELECT line_no, rule_id, settlement_id, base_cents, commission_cents, status "
            "FROM COMMISSION_ENTRY WHERE sale_id = ? ORDER BY line_no",
            (sale["sale_id"],),
        ).fetchall()
    finally:
        conn.close()

    assert len(items) == 2
    assert items[0]["line_no"] == 1
    assert items[0]["quantity"] == 2
    assert items[0]["final_unit_price_cents"] == price_a_cents
    assert items[0]["line_cents"] == 2 * price_a_cents
    assert items[1]["line_no"] == 2
    assert items[1]["quantity"] == 3
    assert items[1]["final_unit_price_cents"] == price_b_cents
    assert items[1]["line_cents"] == 3 * price_b_cents
    assert len(entries) == 2
    for entry, item in zip(entries, items):
        expected_commission = int(
            (Decimal(item["line_cents"]) * Decimal("0.09")).quantize(
                Decimal("1"),
                rounding=ROUND_HALF_UP,
            )
        )
        assert entry["line_no"] == item["line_no"]
        assert entry["rule_id"] == 7
        assert entry["settlement_id"] is None
        assert entry["base_cents"] == item["line_cents"]
        assert entry["commission_cents"] == expected_commission
        assert entry["status"] == "待结算"

    page = client.get("/sales/new")
    assert page.status_code == 200
    assert f"S{sale['sale_id']}" in page.get_data(as_text=True)


def test_sale_without_matching_rules_stays_pending(app: Flask, client: FlaskClient) -> None:
    """全部规则停用时销售仍应落库，但保持待计佣且不生成佣金流水。"""
    product = _product(app, "PRODUCT-001")
    shift_id = _current_shift_id(app)
    client_txn_id = str(uuid4())
    form = _payload(
        shift_id,
        [
            (
                int(product["product_id"]),
                "1",
                _price_for_cents(int(product["min_price_cents"])),
            )
        ],
        client_txn_id,
    )

    conn = _connect(app)
    try:
        with conn:
            conn.execute("UPDATE COMMISSION_RULE SET status = '停用'")
    finally:
        conn.close()

    _login(client, "sales01")
    response = client.post("/sales/new", data=form, follow_redirects=False)
    assert response.status_code == 302

    sale = _sale_by_client_txn_id(app, client_txn_id)
    assert sale is not None
    assert sale["status"] == "待计佣"

    conn = _connect(app)
    try:
        entry_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM COMMISSION_ENTRY WHERE sale_id = ?",
                (sale["sale_id"],),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert entry_count == 0


def test_price_below_minimum_is_rejected_without_sale(
    app: Flask,
    client: FlaskClient,
) -> None:
    """低于最低售价应拒绝整单并保留输入。"""
    product = _product(app, "PRODUCT-001")
    below_min_cents = int(product["min_price_cents"]) - 1
    shift_id = _current_shift_id(app)
    client_txn_id = str(uuid4())
    form = _payload(
        shift_id,
        [(int(product["product_id"]), "1", _price_for_cents(below_min_cents))],
        client_txn_id,
    )

    _login(client, "sales01")
    response = client.post("/sales/new", data=form, follow_redirects=False)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "单价低于最低售价" in body
    assert "需合伙人批准" in body
    assert f'value="{_price_for_cents(below_min_cents)}"' in body
    assert _sale_by_client_txn_id(app, client_txn_id) is None


def test_price_with_more_than_two_decimals_is_rejected(
    app: Flask,
    client: FlaskClient,
) -> None:
    """三位小数不得被四舍五入，应直接拒绝。"""
    product = _product(app, "PRODUCT-001")
    price = f"{Decimal(int(product['min_price_cents'])) / Decimal('100'):.3f}"
    shift_id = _current_shift_id(app)
    client_txn_id = str(uuid4())
    form = _payload(
        shift_id,
        [(int(product["product_id"]), "1", price)],
        client_txn_id,
    )

    _login(client, "sales01")
    response = client.post("/sales/new", data=form, follow_redirects=False)
    assert response.status_code == 200
    assert "最多保留两位小数" in response.get_data(as_text=True)
    assert _sale_by_client_txn_id(app, client_txn_id) is None


@pytest.mark.parametrize("quantity", ["0", "-1", "1.5", ""])
def test_non_positive_or_non_integer_quantity_is_rejected(
    app: Flask,
    client: FlaskClient,
    quantity: str,
) -> None:
    """数量必须是严格正整数字符串，不能截断小数或接受空值。"""
    product = _product(app, "PRODUCT-001")
    shift_id = _current_shift_id(app)
    client_txn_id = str(uuid4())
    form = _payload(
        shift_id,
        [
            (
                int(product["product_id"]),
                quantity,
                _price_for_cents(int(product["min_price_cents"])),
            )
        ],
        client_txn_id,
    )

    _login(client, "sales01")
    response = client.post("/sales/new", data=form, follow_redirects=False)
    assert response.status_code == 200
    assert "数量必须为正整数" in response.get_data(as_text=True)
    assert _sale_by_client_txn_id(app, client_txn_id) is None


def test_disabled_product_is_rejected_and_not_listed(
    app: Flask,
    client: FlaskClient,
) -> None:
    """停用产品不应出现在下拉中，也不能用于登记销售。"""
    product = _product(app, "PRODUCT-001")
    conn = _connect(app)
    try:
        with conn:
            conn.execute(
                "UPDATE PRODUCT SET status = '停用' WHERE product_id = ?",
                (product["product_id"],),
            )
    finally:
        conn.close()

    _login(client, "sales01")
    page = client.get("/sales/new")
    assert page.status_code == 200
    assert product["product_code"] not in page.get_data(as_text=True)

    shift_id = _current_shift_id(app)
    client_txn_id = str(uuid4())
    form = _payload(
        shift_id,
        [
            (
                int(product["product_id"]),
                "1",
                _price_for_cents(int(product["min_price_cents"])),
            )
        ],
        client_txn_id,
    )
    response = client.post("/sales/new", data=form, follow_redirects=False)
    assert response.status_code == 200
    assert "已停用，不能登记销售" in response.get_data(as_text=True)
    assert _sale_by_client_txn_id(app, client_txn_id) is None


def test_duplicate_client_txn_id_returns_original_sale_once(
    app: Flask,
    client: FlaskClient,
) -> None:
    """同一客户端交易编号重复提交应返回原单且不重复写明细。"""
    product = _product(app, "PRODUCT-001")
    shift_id = _current_shift_id(app)
    client_txn_id = str(uuid4())
    form = _payload(
        shift_id,
        [
            (
                int(product["product_id"]),
                "1",
                _price_for_cents(int(product["min_price_cents"])),
            )
        ],
        client_txn_id,
    )

    _login(client, "sales01")
    first = client.post("/sales/new", data=form, follow_redirects=False)
    assert first.status_code == 302
    sale = _sale_by_client_txn_id(app, client_txn_id)
    assert sale is not None

    second = client.post("/sales/new", data=form, follow_redirects=True)
    assert second.status_code == 200
    assert f"该笔已登记，单号 S{sale['sale_id']}" in second.get_data(as_text=True)

    conn = _connect(app)
    try:
        sale_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM SALE WHERE client_txn_id = ?",
                (client_txn_id,),
            ).fetchone()[0]
        )
        item_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM SALE_ITEM WHERE sale_id = ?",
                (sale["sale_id"],),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert sale_count == 1
    assert item_count == 1


def test_admin_cannot_access_new_sale(client: FlaskClient) -> None:
    """合伙人访问销售登记页应返回 403。"""
    _login(client, "admin")
    response = client.get("/sales/new")
    assert response.status_code == 403


def test_no_active_shift_disables_page_and_rejects_post(
    app: Flask,
    client: FlaskClient,
) -> None:
    """无进行中班次时页面提示并禁用提交，直接 POST 也不落库。"""
    old_shift_id = _current_shift_id(app)
    product = _product(app, "PRODUCT-001")
    conn = _connect(app)
    try:
        with conn:
            conn.execute(
                "UPDATE SHIFT SET status = '已结束' "
                "WHERE salesperson_id = 1 AND status = '进行中'"
            )
    finally:
        conn.close()

    _login(client, "sales01")
    page = client.get("/sales/new")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "当前没有进行中的班次，请先在基础数据里添加或联系合伙人" in body
    assert '<button class="button" id="submit-sale" type="submit" disabled>' in body

    client_txn_id = str(uuid4())
    form = _payload(
        old_shift_id,
        [
            (
                int(product["product_id"]),
                "1",
                _price_for_cents(int(product["min_price_cents"])),
            )
        ],
        client_txn_id,
    )
    response = client.post("/sales/new", data=form, follow_redirects=False)
    assert response.status_code == 200
    assert "当前没有进行中的班次，请先在基础数据里添加或联系合伙人" in response.get_data(as_text=True)
    assert _sale_by_client_txn_id(app, client_txn_id) is None