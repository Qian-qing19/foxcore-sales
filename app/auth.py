"""登录状态与角色权限辅助。"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
import sqlite3
from typing import Any

from flask import abort, g, redirect, session, url_for
from werkzeug.security import check_password_hash

from app.db import get_db


def authenticate_user(username: str, password: str) -> sqlite3.Row | None:
    """校验启用账号的用户名和密码，成功时返回用户记录。"""
    user = get_db().execute(
        "SELECT user_id, username, password_hash, role, salesperson_id "
        "FROM APP_USER WHERE username = ? AND status = '启用'",
        (username,),
    ).fetchone()
    if user is None or not check_password_hash(user["password_hash"], password):
        return None
    return user


def load_logged_in_user() -> None:
    """在请求开始前从 session 加载当前用户到 g.user。"""
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
        return

    user = get_db().execute(
        "SELECT user_id, username, role, salesperson_id "
        "FROM APP_USER WHERE user_id = ? AND status = '启用'",
        (user_id,),
    ).fetchone()
    if user is None:
        session.clear()
        g.user = None
        return
    g.user = user


def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """要求当前请求已登录。"""
    @wraps(view)
    def wrapped_view(*args: Any, **kwargs: Any) -> Any:
        if g.user is None:
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapped_view


def role_required(role: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """要求当前用户具有指定中文角色。"""
    def decorator(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapped_view(*args: Any, **kwargs: Any) -> Any:
            if g.user is None:
                return redirect(url_for("auth.login"))
            if g.user["role"] != role:
                abort(403)
            return view(*args, **kwargs)

        return wrapped_view

    return decorator