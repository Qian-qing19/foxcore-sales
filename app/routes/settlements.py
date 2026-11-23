"""佣金核算生成、详情与确认路由。"""

from __future__ import annotations

from typing import Any

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from app.auth import role_required
from app.services import settlement_service


bp = Blueprint("settlements", __name__)


def _render_index(
    form_values: Any = None,
    *,
    status_code: int = 200,
):
    """读取结算列表上下文并渲染生成表单。"""
    context = settlement_service.get_settlements_page(form_values)
    return render_template("settlements.html", **context), status_code


def _render_detail(settlement_id: int, *, status_code: int = 200):
    """读取结算详情上下文并渲染明细页。"""
    context = settlement_service.get_settlement_detail(settlement_id)
    return render_template("settlement_detail.html", **context), status_code


@bp.get("/settlements")
@role_required("合伙人")
def index():
    """显示结算单列表与生成表单。"""
    return _render_index()


@bp.post("/settlements/generate")
@role_required("合伙人")
def generate():
    """生成佣金结算草稿并跳转到详情页。"""
    try:
        result = settlement_service.generate_settlement(
            request.form.get("period_start", ""),
            request.form.get("period_end", ""),
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return _render_index(request.form)

    flash(
        f"结算草稿 {result['settlement_no']} 已生成，"
        f"共 {result['line_count']} 位顾问、{result['entry_count']} 条佣金",
        "success",
    )
    return redirect(
        url_for("settlements.detail", settlement_id=result["settlement_id"])
    )


@bp.get("/settlements/<int:settlement_id>")
@role_required("合伙人")
def detail(settlement_id: int):
    """显示结算单头、顾问汇总、异常清单与确认操作。"""
    try:
        return _render_detail(settlement_id)
    except (LookupError, ValueError):
        abort(404)


@bp.post("/settlements/<int:settlement_id>/confirm")
@role_required("合伙人")
def confirm(settlement_id: int):
    """确认草稿结算单并冻结其佣金金额。"""
    try:
        result = settlement_service.confirm_settlement(
            settlement_id,
            int(g.user["user_id"]),
        )
    except LookupError:
        abort(404)
    except ValueError as exc:
        flash(str(exc), "error")
        try:
            return _render_detail(settlement_id)
        except (LookupError, ValueError):
            abort(404)

    flash(
        f"结算单 {result['settlement_no']} 已确认，"
        f"{result['entry_count']} 条佣金已结算",
        "success",
    )
    return redirect(url_for("settlements.detail", settlement_id=settlement_id))