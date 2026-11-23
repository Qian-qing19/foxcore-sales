"""基础数据维护路由。"""

from __future__ import annotations

import sqlite3

from flask import Blueprint, flash, redirect, render_template, request, url_for

from app.auth import role_required
from app.services import master_service


bp = Blueprint("master", __name__)


def _active_tab() -> str:
    """从查询参数或表单读取有效 Tab，非法值回退到产品。"""
    tab = request.values.get("tab", "product")
    return tab if tab in master_service.TAB_LABELS else "product"


def _parse_optional_id(raw: str | None, label: str) -> int | None:
    """解析可选的整数编号，空值返回 None。"""
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{label}编号无效") from None
    if value <= 0:
        raise ValueError(f"{label}编号无效")
    return value


def _integrity_message(tab: str) -> str:
    """把数据库完整性错误转换为当前 Tab 的中文提示。"""
    if tab == "product":
        return "保存失败：产品编码已存在或数据不符合约束。"
    if tab == "event":
        return "保存失败：活动编码已存在或数据不符合约束。"
    if tab == "booth":
        return "保存失败：摊位编号在该活动下已存在或数据不符合约束。"
    if tab == "salesperson":
        return "保存失败：员工编码已存在或数据不符合约束。"
    if tab == "shift":
        return "保存失败：班次数据不符合数据库约束。"
    return "保存失败：数据已存在或不符合数据库约束。"


def _render_master(
    tab: str,
    *,
    edit_id: int | None = None,
    form_data: dict[str, str] | None = None,
    status_code: int = 200,
):
    """读取页面数据并渲染基础数据页面。"""
    context = master_service.get_master_page(tab, edit_id, form_data)
    return render_template("master.html", **context), status_code


@bp.route("/master", methods=("GET", "POST"))
@role_required("合伙人")
def index():
    """显示并维护产品、活动、摊位、销售顾问和班次基础数据。"""
    tab = _active_tab()

    if request.method == "POST":
        action = request.form.get("action", "")
        record_id: int | None = None
        try:
            record_id = _parse_optional_id(request.form.get("record_id"), "记录")
            if action == "save":
                message = master_service.save_master_record(tab, request.form, record_id)
            elif action == "toggle":
                if record_id is None:
                    raise ValueError("记录编号不能为空")
                message = master_service.toggle_master_status(tab, record_id)
            else:
                raise ValueError("未知操作")
        except (ValueError, sqlite3.IntegrityError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else _integrity_message(tab)
            flash(message, "error")
            return _render_master(
                tab,
                edit_id=record_id if action == "save" else None,
                form_data=request.form.to_dict(),
            )

        flash(message, "success")
        return redirect(url_for("master.index", tab=tab))

    try:
        edit_id = _parse_optional_id(request.args.get("edit"), "记录")
        return _render_master(tab, edit_id=edit_id)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("master.index", tab=tab))
