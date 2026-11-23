"""基础数据维护页面与服务测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

from flask import Flask
from flask.testing import FlaskClient
import pytest

from app import create_app
from init_db import initialize_database


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


@pytest.fixture()
def app(tmp_path: Path) -> Flask:
    """为每个测试创建独立临时数据库和应用实例。"""
    db_path = tmp_path / "foxcore-master.db"
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


def _product_by_code(app: Flask, product_code: str) -> sqlite3.Row | None:
    """按编码读取产品。"""
    conn = _connect(app)
    try:
        return conn.execute(
            "SELECT product_id, product_code, name, category, min_price_cents, status "
            "FROM PRODUCT WHERE product_code = ?",
            (product_code,),
        ).fetchone()
    finally:
        conn.close()


def test_admin_get_master_returns_200_and_contains_product(client: FlaskClient) -> None:
    """合伙人可打开基础数据页并看到产品 Tab。"""
    _login(client, "admin")
    response = client.get("/master")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "基础数据" in body
    assert "产品" in body
    assert "销售顾问" in body


def test_create_product_success_persists_cents_and_shows_in_list(
    app: Flask,
    client: FlaskClient,
) -> None:
    """新增产品应精确存分，空分类存空字符串并显示破折号。"""
    _login(client, "admin")
    response = client.post(
        "/master?tab=product",
        data={
            "tab": "product",
            "action": "save",
            "product_code": "TEST-PRODUCT-001",
            "name": "测试产品",
            "category": "",
            "min_price": "19.90",
            "status": "在售",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "产品新增成功" in body
    assert "TEST-PRODUCT-001" in body
    assert "19.90" in body
    assert "—" in body

    product = _product_by_code(app, "TEST-PRODUCT-001")
    assert product is not None
    assert product["min_price_cents"] == 1990
    assert product["category"] == ""
    assert product["status"] == "在售"

    edit_response = client.get(f"/master?tab=product&edit={product['product_id']}")
    assert edit_response.status_code == 200
    edit_body = edit_response.get_data(as_text=True)
    assert 'id="product_category" name="category" value=""' in edit_body
    assert 'id="product_price" name="min_price" value="19.90"' in edit_body


def test_duplicate_product_code_is_rejected_without_new_row(
    app: Flask,
    client: FlaskClient,
) -> None:
    """重复产品编码应返回原页面、中文错误，且不新增记录。"""
    _login(client, "admin")
    conn = _connect(app)
    try:
        before = int(conn.execute("SELECT COUNT(*) FROM PRODUCT").fetchone()[0])
    finally:
        conn.close()

    response = client.post(
        "/master?tab=product",
        data={
            "tab": "product",
            "action": "save",
            "product_code": "PRODUCT-001",
            "name": "重复编码产品",
            "category": "测试",
            "min_price": "9.90",
            "status": "在售",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "产品编码已存在" in response.get_data(as_text=True)

    conn = _connect(app)
    try:
        after = int(conn.execute("SELECT COUNT(*) FROM PRODUCT").fetchone()[0])
    finally:
        conn.close()
    assert after == before


def test_toggle_product_changes_status_to_disabled(
    app: Flask,
    client: FlaskClient,
) -> None:
    """产品停用按钮应只更新状态并重定向回产品 Tab。"""
    _login(client, "admin")
    response = client.post(
        "/master?tab=product",
        data={"tab": "product", "action": "toggle", "record_id": "1"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "产品已停用" in response.get_data(as_text=True)

    conn = _connect(app)
    try:
        status = conn.execute(
            "SELECT status FROM PRODUCT WHERE product_id = 1"
        ).fetchone()["status"]
    finally:
        conn.close()
    assert status == "停用"


def test_create_and_edit_salesperson_preserves_hired_at(
    app: Flask,
    client: FlaskClient,
) -> None:
    """新增顾问写当天日期，编辑资料时不得覆盖原入职日期。"""
    _login(client, "admin")
    create_response = client.post(
        "/master?tab=salesperson",
        data={
            "tab": "salesperson",
            "action": "save",
            "emp_code": "TEST-SP-001",
            "name": "测试顾问",
            "phone": "",
            "status": "在职",
        },
        follow_redirects=True,
    )
    assert create_response.status_code == 200
    assert "销售顾问新增成功" in create_response.get_data(as_text=True)

    conn = _connect(app)
    try:
        created = conn.execute(
            "SELECT salesperson_id, emp_code, name, phone, status, hired_at "
            "FROM SALESPERSON WHERE emp_code = 'TEST-SP-001'"
        ).fetchone()
    finally:
        conn.close()
    assert created is not None
    assert created["hired_at"] == datetime.now(CHINA_TZ).date().isoformat()
    assert created["phone"] == ""
    original_hired_at = created["hired_at"]

    edit_response = client.post(
        "/master?tab=salesperson",
        data={
            "tab": "salesperson",
            "action": "save",
            "record_id": str(created["salesperson_id"]),
            "emp_code": "TEST-SP-001",
            "name": "测试顾问（已修改）",
            "phone": "000-0000-0001",
            "status": "停用",
        },
        follow_redirects=True,
    )
    assert edit_response.status_code == 200
    assert "销售顾问更新成功" in edit_response.get_data(as_text=True)

    conn = _connect(app)
    try:
        edited = conn.execute(
            "SELECT name, phone, status, hired_at FROM SALESPERSON "
            "WHERE salesperson_id = ?",
            (created["salesperson_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert edited["name"] == "测试顾问（已修改）"
    assert edited["phone"] == "000-0000-0001"
    assert edited["status"] == "停用"
    assert edited["hired_at"] == original_hired_at


def test_toggle_salesperson_changes_status(client: FlaskClient) -> None:
    """销售顾问停用按钮应切换状态并显示中文提示。"""
    _login(client, "admin")
    response = client.post(
        "/master?tab=salesperson",
        data={"tab": "salesperson", "action": "toggle", "record_id": "1"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "销售顾问已停用" in response.get_data(as_text=True)


def test_sales01_get_master_returns_403(client: FlaskClient) -> None:
    """销售顾问不得访问基础数据页面。"""
    _login(client, "sales01")
    response = client.get("/master")
    assert response.status_code == 403

def test_all_five_master_tabs_open(client: FlaskClient) -> None:
    """五个基础数据 Tab 均可通过 GET 打开并显示对应列表。"""
    _login(client, "admin")
    expected = {
        "product": "产品列表",
        "event": "活动列表",
        "booth": "摊位列表",
        "salesperson": "销售顾问列表",
        "shift": "班次列表",
    }
    for tab, marker in expected.items():
        response = client.get(f"/master?tab={tab}")
        assert response.status_code == 200
        assert marker in response.get_data(as_text=True)


def test_create_and_edit_event_persists_dates_and_has_no_toggle(
    app: Flask,
    client: FlaskClient,
) -> None:
    """活动日期只存到天，可编辑且列表不出现停用/启用按钮。"""
    _login(client, "admin")
    create_response = client.post(
        "/master?tab=event",
        data={
            "tab": "event",
            "action": "save",
            "event_code": "TEST-EVENT-001",
            "event_name": "测试音乐节",
            "event_type": "音乐节",
            "venue_id": "1",
            "start_date": "2026-12-01",
            "end_date": "2026-12-03",
            "description": "测试活动",
        },
        follow_redirects=True,
    )
    assert create_response.status_code == 200
    create_body = create_response.get_data(as_text=True)
    assert "活动新增成功" in create_body
    assert "2026-12-01 至 2026-12-03" in create_body
    assert 'value="toggle"' not in create_body

    conn = _connect(app)
    try:
        event = conn.execute(
            "SELECT event_id, start_date, end_date FROM EVENT "
            "WHERE event_code = 'TEST-EVENT-001'"
        ).fetchone()
    finally:
        conn.close()
    assert event is not None
    assert event["start_date"] == "2026-12-01"
    assert event["end_date"] == "2026-12-03"

    edit_response = client.get(f"/master?tab=event&edit={event['event_id']}")
    assert edit_response.status_code == 200
    edit_body = edit_response.get_data(as_text=True)
    assert 'value="2026-12-01"' in edit_body
    assert 'type="date" name="start_date"' in edit_body
    assert 'value="toggle"' not in edit_body


def test_event_end_date_before_start_date_is_rejected(
    app: Flask,
    client: FlaskClient,
) -> None:
    """活动结束日期早于开始日期时返回中文错误且不落库。"""
    _login(client, "admin")
    response = client.post(
        "/master?tab=event",
        data={
            "tab": "event",
            "action": "save",
            "event_code": "TEST-EVENT-BAD",
            "event_name": "非法日期活动",
            "event_type": "展销会",
            "venue_id": "1",
            "start_date": "2026-12-10",
            "end_date": "2026-12-09",
            "description": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "结束日期不能早于开始日期" in response.get_data(as_text=True)

    conn = _connect(app)
    try:
        count = int(
            conn.execute(
                "SELECT COUNT(*) FROM EVENT WHERE event_code = 'TEST-EVENT-BAD'"
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert count == 0


def test_duplicate_booth_code_in_same_event_is_rejected(
    app: Flask,
    client: FlaskClient,
) -> None:
    """同一活动下重复摊位编号返回中文提示且不新增记录。"""
    _login(client, "admin")
    conn = _connect(app)
    try:
        before = int(conn.execute("SELECT COUNT(*) FROM BOOTH").fetchone()[0])
    finally:
        conn.close()

    response = client.post(
        "/master?tab=booth",
        data={
            "tab": "booth",
            "action": "save",
            "event_id": "1",
            "booth_code": "A-01",
            "location": "重复位置",
            "status": "在用",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "摊位编号在该活动下已存在" in response.get_data(as_text=True)

    conn = _connect(app)
    try:
        after = int(conn.execute("SELECT COUNT(*) FROM BOOTH").fetchone()[0])
    finally:
        conn.close()
    assert after == before


def test_create_and_toggle_booth(app: Flask, client: FlaskClient) -> None:
    """摊位可新增并通过按钮切换状态。"""
    _login(client, "admin")
    create_response = client.post(
        "/master?tab=booth",
        data={
            "tab": "booth",
            "action": "save",
            "event_id": "1",
            "booth_code": "TEST-BOOTH-01",
            "location": "测试位置",
            "status": "在用",
        },
        follow_redirects=True,
    )
    assert create_response.status_code == 200
    assert "摊位新增成功" in create_response.get_data(as_text=True)

    conn = _connect(app)
    try:
        booth = conn.execute(
            "SELECT booth_id, event_id, status FROM BOOTH "
            "WHERE booth_code = 'TEST-BOOTH-01'"
        ).fetchone()
    finally:
        conn.close()
    assert booth is not None
    assert booth["event_id"] == 1
    assert booth["status"] == "在用"

    toggle_response = client.post(
        "/master?tab=booth",
        data={"tab": "booth", "action": "toggle", "record_id": str(booth["booth_id"])},
        follow_redirects=True,
    )
    assert toggle_response.status_code == 200
    assert "摊位已停用" in toggle_response.get_data(as_text=True)


def test_shift_datetime_local_conversion_and_edit_backfill(
    app: Flask,
    client: FlaskClient,
) -> None:
    """datetime-local 的 T 格式应转为空格秒级入库，并在编辑时转回 T 格式。"""
    _login(client, "admin")
    response = client.post(
        "/master?tab=shift",
        data={
            "tab": "shift",
            "action": "save",
            "salesperson_id": "1",
            "booth_id": "1",
            "start_time": "2026-11-18T09:00",
            "end_time": "2026-11-18T10:30",
            "status": "未开始",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "班次新增成功" in body
    assert "顾问01 · A-01（秋日音乐节）" in body
    assert 'value="toggle"' not in body

    conn = _connect(app)
    try:
        shift = conn.execute(
            "SELECT shift_id, start_time, end_time FROM SHIFT WHERE start_time = ?",
            ("2026-11-18 09:00:00",),
        ).fetchone()
    finally:
        conn.close()
    assert shift is not None
    assert shift["start_time"] == "2026-11-18 09:00:00"
    assert shift["end_time"] == "2026-11-18 10:30:00"
    assert "T" not in shift["start_time"]

    edit_response = client.get(f"/master?tab=shift&edit={shift['shift_id']}")
    assert edit_response.status_code == 200
    edit_body = edit_response.get_data(as_text=True)
    assert 'id="shift_start" type="datetime-local" name="start_time" value="2026-11-18T09:00"' in edit_body
    assert 'id="shift_end" type="datetime-local" name="end_time" value="2026-11-18T10:30"' in edit_body
    assert 'value="toggle"' not in edit_body


def test_shift_end_time_not_after_start_time_is_rejected(
    app: Flask,
    client: FlaskClient,
) -> None:
    """班次结束时间不晚于开始时间时返回中文错误且不落库。"""
    _login(client, "admin")
    response = client.post(
        "/master?tab=shift",
        data={
            "tab": "shift",
            "action": "save",
            "salesperson_id": "1",
            "booth_id": "1",
            "start_time": "2026-11-18T10:00",
            "end_time": "2026-11-18T09:00",
            "status": "未开始",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "结束时间必须晚于开始时间" in response.get_data(as_text=True)

    conn = _connect(app)
    try:
        count = int(
            conn.execute(
                "SELECT COUNT(*) FROM SHIFT WHERE start_time = '2026-11-18 10:00:00'"
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert count == 0

def test_event_with_missing_venue_is_rejected(
    app: Flask,
    client: FlaskClient,
) -> None:
    """活动引用不存在场地时返回中文错误且不落库。"""
    _login(client, "admin")
    response = client.post(
        "/master?tab=event",
        data={
            "tab": "event",
            "action": "save",
            "event_code": "TEST-EVENT-NO-VENUE",
            "event_name": "无场地活动",
            "event_type": "展销会",
            "venue_id": "99999",
            "start_date": "2026-12-01",
            "end_date": "2026-12-02",
            "description": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "所选场地不存在" in response.get_data(as_text=True)

    conn = _connect(app)
    try:
        count = int(
            conn.execute(
                "SELECT COUNT(*) FROM EVENT WHERE event_code = 'TEST-EVENT-NO-VENUE'"
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert count == 0
