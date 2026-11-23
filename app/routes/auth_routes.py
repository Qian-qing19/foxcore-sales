"""登录与退出路由。"""

from __future__ import annotations

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for

from app.auth import authenticate_user


bp = Blueprint("auth", __name__)


def _landing_endpoint(role: str) -> str:
    """按角色返回登录后的落地地址。"""
    if role == "合伙人":
        return "/"
    return "/sales/new"


@bp.route("/login", methods=("GET", "POST"))
def login():
    """显示登录页并处理账号密码登录。"""
    if g.user is not None:
        return redirect(_landing_endpoint(g.user["role"]))

    username = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = authenticate_user(username, password)
        if user is None:
            flash("用户名或密码错误，请重新输入。", "error")
            return render_template("login.html", username=username)

        session.clear()
        session["user_id"] = user["user_id"]
        session["role"] = user["role"]
        session["salesperson_id"] = user["salesperson_id"]
        return redirect(_landing_endpoint(user["role"]))

    return render_template("login.html", username=username)


@bp.get("/logout")
def logout():
    """清除登录状态并返回登录页。"""
    session.clear()
    return redirect(url_for("auth.login"))