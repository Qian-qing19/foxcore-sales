"""数据库连接生命周期管理。"""

from __future__ import annotations

import sqlite3

from flask import current_app, g


def get_db() -> sqlite3.Connection:
    """取得当前请求的数据库连接，并确保外键约束开启。"""
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(_error: BaseException | None = None) -> None:
    """在应用上下文结束时关闭数据库连接。"""
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()