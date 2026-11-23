"""登录、会话与角色权限测试。"""

from __future__ import annotations

from pathlib import Path

from flask import Flask
from flask.testing import FlaskClient
import pytest

from app import create_app
from init_db import initialize_database


@pytest.fixture()
def app(tmp_path: Path) -> Flask:
    """为每个测试创建独立临时数据库和应用实例。"""
    db_path = tmp_path / "foxcore-test.db"
    initialize_database(db_path)
    application = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret-key",
            "DATABASE": str(db_path),
        }
    )
    return application


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    """提供 Flask 测试客户端。"""
    return app.test_client()


def _login(client: FlaskClient, username: str, password: str):
    """提交登录表单。"""
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def test_get_login_returns_200(client: FlaskClient) -> None:
    """登录页可通过 GET 打开并输出中文。"""
    response = client.get("/login")
    assert response.status_code == 200
    assert "销售跟踪系统" in response.get_data(as_text=True)


def test_wrong_password_returns_200_and_stays_logged_out(client: FlaskClient) -> None:
    """错误密码应返回登录页且不建立会话。"""
    response = _login(client, "admin", "wrong-password")
    assert response.status_code == 200
    assert "用户名或密码错误" in response.get_data(as_text=True)
    with client.session_transaction() as session:
        assert "user_id" not in session


def test_static_css_returns_css_content_type(client: FlaskClient) -> None:
    """Windows 的 MIME 注册表不应导致 CSS 以错误类型返回。"""
    response = client.get("/static/css/app.css")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("text/css")


def test_admin_login_redirects_to_dashboard(client: FlaskClient) -> None:
    """合伙人登录后跳转工作台。"""
    response = _login(client, "admin", "123456")
    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def test_dashboard_requires_login(client: FlaskClient) -> None:
    """未登录访问工作台应跳转登录页。"""
    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_salesperson_dashboard_redirects_to_new_sale(client: FlaskClient) -> None:
    """销售顾问访问 / 应跳转销售登记页而不是 403。"""
    login_response = _login(client, "sales01", "123456")
    assert login_response.status_code == 302

    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"] == "/sales/new"

def test_salesperson_landing_and_partner_page_forbidden(client: FlaskClient) -> None:
    """销售顾问登录后进入登记页，访问合伙人页面返回 403。"""
    login_response = _login(client, "sales01", "123456")
    assert login_response.status_code == 302
    assert login_response.headers["Location"] == "/sales/new"

    response = client.get("/master")
    assert response.status_code == 403