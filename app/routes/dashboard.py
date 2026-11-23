"""工作台路由。"""

from __future__ import annotations

from flask import Blueprint, abort, g, redirect, render_template, url_for

from app.auth import login_required
from app.services import dashboard_service


bp = Blueprint("dashboard", __name__)


@bp.get("/")
@login_required
def index():
    """合伙人显示经营工作台，销售顾问跳转销售登记页。"""
    if g.user["role"] == "合伙人":
        context = dashboard_service.get_dashboard_data()
        return render_template("dashboard.html", **context)
    if g.user["role"] == "销售顾问":
        return redirect(url_for("sales.new_sale"))
    abort(403)
