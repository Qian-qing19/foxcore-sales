"""Foxcore Flask 应用工厂。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import mimetypes
import os
from pathlib import Path
from typing import Any

from flask import Flask, Response

from app.auth import load_logged_in_user
from app.db import close_db
from app.routes.api import bp as api_bp
from app.routes.auth_routes import bp as auth_bp
from app.routes.dashboard import bp as dashboard_bp
from app.routes.imports import bp as imports_bp
from app.routes.master import bp as master_bp
from app.routes.reports import bp as reports_bp
from app.routes.rules import bp as rules_bp
from app.routes.sales import bp as sales_bp
from app.routes.settlements import bp as settlements_bp


mimetypes.add_type("text/css", ".css")
mimetypes.add_type("text/javascript", ".js")

def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    """创建并配置 Flask 应用。"""
    project_root = Path(__file__).resolve().parent.parent
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY") or "foxcore-dev-secret-key",
        DATABASE=os.environ.get(
            "DATABASE",
            str(project_root / "data" / "foxcore.db"),
        ),
    )

    if test_config is not None:
        app.config.update(test_config)

    app.before_request(load_logged_in_user)
    app.teardown_appcontext(close_db)

    app.register_blueprint(api_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(sales_bp)
    app.register_blueprint(settlements_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(rules_bp)
    app.register_blueprint(master_bp)
    app.register_blueprint(imports_bp)

    @app.template_filter("yuan")
    def yuan_filter(value: Any) -> str:
        """将整数分转换为两位小数元。"""
        if value is None or value == "":
            return "0.00"
        try:
            cents = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return str(value)
        return f"{cents / Decimal('100'):.2f}"

    @app.errorhandler(403)
    def forbidden(_error: Exception) -> Response:
        """以中文 HTML 返回越权访问错误。"""
        html = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>403 禁止访问</title>
</head>
<body>
  <main>
    <h1>403 禁止访问</h1>
    <p>当前账号无权访问此页面。</p>
    <p><a href="/login">返回登录页</a></p>
  </main>
</body>
</html>"""
        return Response(html, status=403, content_type="text/html; charset=utf-8")

    return app