"""佣金规则页面与状态流转测试。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import sqlite3

from flask import Flask
from flask.testing import FlaskClient
import pytest

from app import create_app
from init_db import initialize_database


@pytest.fixture()
def app(tmp_path: Path) -> Flask:
    """为每个测试创建独立临时数据库和应用实例。"""
    db_path = tmp_path / "foxcore-rules.db"
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


def _rule_by_code(
    app: Flask,
    rule_code: str,
    version_no: int | None = None,
) -> dict[str, object] | None:
    """按代码和可选版本读取规则。"""
    conn = _connect(app)
    try:
        if version_no is None:
            row = conn.execute(
                "SELECT * FROM COMMISSION_RULE WHERE rule_code = ? ORDER BY version_no DESC",
                (rule_code,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM COMMISSION_RULE WHERE rule_code = ? AND version_no = ?",
                (rule_code, version_no),
            ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def _rule_count(app: Flask, rule_code: str) -> int:
    """统计指定规则代码的版本数。"""
    conn = _connect(app)
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM COMMISSION_RULE WHERE rule_code = ?",
                (rule_code,),
            ).fetchone()[0]
        )
    finally:
        conn.close()


def _create_payload(**overrides):
    """返回可用于新建 GLOBAL 比例规则的表单数据。"""
    payload = {
        "action": "create",
        "rule_code": "TEST-GLOBAL-5PCT",
        "dimension": "GLOBAL",
        "event_type_value": "",
        "calc_method": "按比例",
        "rate": "5",
        "fixed_amount": "",
        "priority": "7",
        "effective_from": "2026-11-01",
        "effective_to": "",
    }
    payload.update(overrides)
    return payload


def _post_action(client: FlaskClient, action: str, rule_id: int):
    """提交发布或停用操作。"""
    return client.post(
        "/rules",
        data={"action": action, "rule_id": str(rule_id)},
        follow_redirects=True,
    )


def test_admin_get_rules_returns_200_and_shows_all_seed_rows(
    app: Flask,
    client: FlaskClient,
) -> None:
    """合伙人可看到 8 条种子规则及 28 个产品选项。"""
    _login(client, "admin")
    response = client.get("/rules")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "佣金规则" in body
    assert "共 8 条" in body
    assert body.count('name="product_ids"') == 28

    conn = _connect(app)
    try:
        seed_rules = conn.execute(
            "SELECT rule_code, version_no FROM COMMISSION_RULE ORDER BY rule_id"
        ).fetchall()
    finally:
        conn.close()
    assert len(seed_rules) == 8
    for row in seed_rules:
        assert row["rule_code"] in body
        assert f"v{row['version_no']}" in body


def test_create_global_rule_creates_version_one_draft(
    app: Flask,
    client: FlaskClient,
) -> None:
    """新建 GLOBAL 规则应生成草稿和版本 1。"""
    _login(client, "admin")
    response = client.post("/rules", data=_create_payload(), follow_redirects=True)
    assert response.status_code == 200
    assert "规则草稿已创建" in response.get_data(as_text=True)

    rule = _rule_by_code(app, "TEST-GLOBAL-5PCT")
    assert rule is not None
    assert rule["version_no"] == 1
    assert rule["status"] == "草稿"
    assert rule["created_by"] == 1
    assert rule["event_type_value"] is None
    assert Decimal(str(rule["rate"])) == Decimal("0.05")


def test_publish_draft_changes_status_to_active(
    app: Flask,
    client: FlaskClient,
) -> None:
    """草稿发布后状态变为有效。"""
    _login(client, "admin")
    client.post("/rules", data=_create_payload(), follow_redirects=True)
    rule = _rule_by_code(app, "TEST-GLOBAL-5PCT")
    assert rule is not None

    response = _post_action(client, "publish", int(rule["rule_id"]))
    assert response.status_code == 200
    assert "已发布" in response.get_data(as_text=True)

    published = _rule_by_code(app, "TEST-GLOBAL-5PCT", 1)
    assert published is not None
    assert published["status"] == "有效"


def test_publishing_v2_disables_previous_active_version(
    app: Flask,
    client: FlaskClient,
) -> None:
    """发布同代码 v2 时，v1 在同一事务中自动停用。"""
    _login(client, "admin")
    client.post("/rules", data=_create_payload(), follow_redirects=True)
    v1 = _rule_by_code(app, "TEST-GLOBAL-5PCT", 1)
    assert v1 is not None
    _post_action(client, "publish", int(v1["rule_id"]))

    client.post(
        "/rules",
        data=_create_payload(rate="6", priority="8"),
        follow_redirects=True,
    )
    v2 = _rule_by_code(app, "TEST-GLOBAL-5PCT", 2)
    assert v2 is not None
    assert v2["status"] == "草稿"
    _post_action(client, "publish", int(v2["rule_id"]))

    old_rule = _rule_by_code(app, "TEST-GLOBAL-5PCT", 1)
    new_rule = _rule_by_code(app, "TEST-GLOBAL-5PCT", 2)
    assert old_rule is not None and old_rule["status"] == "停用"
    assert new_rule is not None and new_rule["status"] == "有效"

    conn = _connect(app)
    try:
        active_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM COMMISSION_RULE "
                "WHERE rule_code = 'TEST-GLOBAL-5PCT' AND status = '有效'"
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert active_count == 1


def test_product_dimension_without_target_is_rejected(
    app: Flask,
    client: FlaskClient,
) -> None:
    """产品维度未选择产品时返回中文错误且不落库。"""
    _login(client, "admin")
    response = client.post(
        "/rules",
        data=_create_payload(
            rule_code="TEST-PRODUCT-NO-TARGET",
            dimension="PRODUCT",
        ),
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "至少选择一个产品" in response.get_data(as_text=True)
    assert _rule_count(app, "TEST-PRODUCT-NO-TARGET") == 0


def test_rate_above_100_percent_is_rejected(
    app: Flask,
    client: FlaskClient,
) -> None:
    """费率超过 100% 时返回中文错误且不落库。"""
    _login(client, "admin")
    response = client.post(
        "/rules",
        data=_create_payload(rule_code="TEST-RATE-120", rate="120"),
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "费率必须在 0 到 100 之间" in response.get_data(as_text=True)
    assert _rule_count(app, "TEST-RATE-120") == 0


def test_effective_end_before_start_is_rejected(
    app: Flask,
    client: FlaskClient,
) -> None:
    """生效结束日期早于开始日期时返回中文错误且不落库。"""
    _login(client, "admin")
    response = client.post(
        "/rules",
        data=_create_payload(
            rule_code="TEST-BAD-DATE",
            effective_from="2026-11-10",
            effective_to="2026-11-09",
        ),
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "生效结束日期不能早于开始日期" in response.get_data(as_text=True)
    assert _rule_count(app, "TEST-BAD-DATE") == 0


def test_salesperson_get_rules_returns_403(client: FlaskClient) -> None:
    """销售顾问访问规则页面应返回 403。"""
    _login(client, "sales01")
    response = client.get("/rules")
    assert response.status_code == 403


def test_blank_effective_to_is_stored_as_null(
    app: Flask,
    client: FlaskClient,
) -> None:
    """生效结束日期留空时应以 NULL 入库。"""
    _login(client, "admin")
    client.post(
        "/rules",
        data=_create_payload(rule_code="TEST-OPEN-ENDED", effective_to=""),
        follow_redirects=True,
    )
    rule = _rule_by_code(app, "TEST-OPEN-ENDED")
    assert rule is not None
    assert rule["effective_to"] is None


def test_rate_rule_stores_fixed_amount_as_null(
    app: Flask,
    client: FlaskClient,
) -> None:
    """按比例规则保存后固定额字段必须为 NULL。"""
    _login(client, "admin")
    client.post(
        "/rules",
        data=_create_payload(rule_code="TEST-RATE-NULL-FIXED"),
        follow_redirects=True,
    )
    rule = _rule_by_code(app, "TEST-RATE-NULL-FIXED")
    assert rule is not None
    assert rule["fixed_per_unit_cents"] is None


def test_fixed_amount_is_stored_in_cents_and_rate_is_null(
    app: Flask,
    client: FlaskClient,
) -> None:
    """按件固定额 1.50 元应存为 150 分，费率字段为 NULL。"""
    _login(client, "admin")
    client.post(
        "/rules",
        data=_create_payload(
            rule_code="TEST-FIXED-150",
            calc_method="按件固定额",
            rate="",
            fixed_amount="1.50",
        ),
        follow_redirects=True,
    )
    rule = _rule_by_code(app, "TEST-FIXED-150")
    assert rule is not None
    assert rule["fixed_per_unit_cents"] == 150
    assert rule["rate"] is None


def test_disable_active_rule_keeps_history(
    app: Flask,
    client: FlaskClient,
) -> None:
    """停用有效规则只改状态，不删除记录。"""
    _login(client, "admin")
    client.post("/rules", data=_create_payload(), follow_redirects=True)
    rule = _rule_by_code(app, "TEST-GLOBAL-5PCT")
    assert rule is not None
    _post_action(client, "publish", int(rule["rule_id"]))

    response = _post_action(client, "disable", int(rule["rule_id"]))
    assert response.status_code == 200
    assert "已停用" in response.get_data(as_text=True)

    disabled = _rule_by_code(app, "TEST-GLOBAL-5PCT")
    assert disabled is not None
    assert disabled["status"] == "停用"
    assert _rule_count(app, "TEST-GLOBAL-5PCT") == 1


def test_new_version_form_copies_business_fields_and_targets(
    app: Flask,
    client: FlaskClient,
) -> None:
    """新版本表单复制代码、业务字段及产品关联，但不复用旧规则编号。"""
    _login(client, "admin")
    client.post(
        "/rules",
        data=_create_payload(
            rule_code="TEST-PRODUCT-COPY",
            dimension="PRODUCT",
            product_ids=["1", "2"],
            priority="12",
        ),
        follow_redirects=True,
    )
    v1 = _rule_by_code(app, "TEST-PRODUCT-COPY", 1)
    assert v1 is not None

    response = client.get(f"/rules?new_version={v1['rule_id']}")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "正在基于 TEST-PRODUCT-COPY v1" in body
    assert 'id="rule_code" name="rule_code" value="TEST-PRODUCT-COPY"' in body
    assert 'id="priority" name="priority" type="number" step="1" value="12"' in body
    assert 'id="product_1" type="checkbox" name="product_ids" value="1" checked' in body
    assert 'id="product_2" type="checkbox" name="product_ids" value="2" checked' in body

    client.post(
        "/rules",
        data=_create_payload(
            rule_code="TEST-PRODUCT-COPY",
            dimension="PRODUCT",
            product_ids=["1", "2"],
            priority="12",
        ),
        follow_redirects=True,
    )
    v2 = _rule_by_code(app, "TEST-PRODUCT-COPY", 2)
    assert v2 is not None
    assert v2["rule_id"] != v1["rule_id"]

    conn = _connect(app)
    try:
        linked_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM PRODUCT_COMMISSION_RULE WHERE rule_id = ?",
                (v2["rule_id"],),
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert linked_count == 2


def test_missing_product_target_returns_chinese_error_not_500(
    app: Flask,
    client: FlaskClient,
) -> None:
    """不存在的产品目标应以中文校验错误返回。"""
    _login(client, "admin")
    response = client.post(
        "/rules",
        data=_create_payload(
            rule_code="TEST-MISSING-PRODUCT",
            dimension="PRODUCT",
            product_ids=["99999"],
        ),
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "所选产品不存在" in response.get_data(as_text=True)
    assert _rule_count(app, "TEST-MISSING-PRODUCT") == 0