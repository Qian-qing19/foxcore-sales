"""销售登记与销售流水路由。"""

from __future__ import annotations

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

from app.auth import login_required, role_required
from app.services import sales_service


bp = Blueprint("sales", __name__)


@bp.route("/sales/new", methods=("GET", "POST"))
@role_required("销售顾问")
def new_sale():
    """显示销售登记页并处理销售提交。"""
    salesperson_id = int(g.user["salesperson_id"] or 0)

    if request.method == "POST":
        try:
            result = sales_service.create_sale(request.form, salesperson_id)
        except ValueError as exc:
            flash(str(exc), "error")
            context = sales_service.get_sale_form_context(
                salesperson_id,
                request.form,
            )
            return render_template("sales_new.html", **context), 200

        if result.duplicated:
            flash(f"该笔已登记，单号 {result.sale_no}", "success")
        else:
            flash(
                f"本次登记成功，单号 {result.sale_no}，合计 {result.total_yuan} 元",
                "success",
            )
        return redirect(url_for("sales.new_sale"))

    context = sales_service.get_sale_form_context(salesperson_id)
    return render_template("sales_new.html", **context)


@bp.get("/sales")
@login_required
def list_sales():
    """按角色数据范围显示可筛选、可分页的销售流水。"""
    user_role = str(g.user["role"])
    user_salesperson_id = g.user["salesperson_id"]
    try:
        context = sales_service.get_sales_list(
            request.args,
            user_role=user_role,
            user_salesperson_id=user_salesperson_id,
        )
    except PermissionError:
        abort(403)
    except ValueError as exc:
        flash(str(exc), "error")
        try:
            context = sales_service.get_sales_list(
                {},
                user_role=user_role,
                user_salesperson_id=user_salesperson_id,
            )
        except PermissionError:
            abort(403)
    return render_template("sales_list.html", **context)


@bp.get("/sales/<int:sale_id>")
@login_required
def sale_detail(sale_id: int):
    """显示销售单、明细与逐行佣金。"""
    try:
        context = sales_service.get_sale_detail(
            sale_id,
            user_role=str(g.user["role"]),
            user_salesperson_id=g.user["salesperson_id"],
        )
    except PermissionError:
        abort(403)
    except (LookupError, ValueError):
        abort(404)
    return render_template("sales_detail.html", **context)


@bp.post("/sales/<int:sale_id>/void")
@role_required("合伙人")
def void_sale(sale_id: int):
    """作废销售单，并冲减仍处于待结算状态的佣金。"""
    void_reason = request.form.get("void_reason", "")
    try:
        result = sales_service.void_sale(
            sale_id,
            void_reason,
            int(g.user["user_id"]),
        )
    except LookupError:
        abort(404)
    except (PermissionError, ValueError) as exc:
        flash(str(exc), "error")
        try:
            context = sales_service.get_sale_detail(
                sale_id,
                user_role=str(g.user["role"]),
                user_salesperson_id=g.user["salesperson_id"],
            )
        except PermissionError:
            abort(403)
        except (LookupError, ValueError):
            abort(404)
        context["void_reason"] = void_reason
        return render_template("sales_detail.html", **context), 200

    flash(
        f"销售单 S{sale_id} 已作废，"
        f"冲减 {result['reversed_entries']} 条待结算佣金",
        "success",
    )
    return redirect(url_for("sales.sale_detail", sale_id=sale_id))
