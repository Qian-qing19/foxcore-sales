"""销售多维报表与 CSV 导出路由。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from flask import Blueprint, Response, flash, render_template, request

from app.auth import role_required
from app.services import report_service


bp = Blueprint("reports", __name__)


def _render_report(
    query: Mapping[str, Any] | None = None,
    *,
    error_message: str | None = None,
) -> str:
    """读取报表上下文并渲染报表页。"""
    if error_message:
        flash(error_message, "error")
    context = report_service.get_report_data(query)
    return render_template("reports.html", **context)


@bp.get("/reports")
@role_required("合伙人")
def index() -> str:
    """按筛选条件显示多维销售报表。"""
    try:
        return _render_report(request.args)
    except ValueError as exc:
        return _render_report({}, error_message=str(exc))


@bp.get("/reports/export")
@role_required("合伙人")
def export() -> Response | str:
    """按与页面相同的筛选条件导出 UTF-8 BOM CSV。"""
    try:
        context = report_service.get_report_data(request.args)
    except ValueError as exc:
        return _render_report({}, error_message=str(exc))

    csv_text = report_service.build_report_csv(context)
    filename = report_service.export_filename(context["filters"])
    return Response(
        csv_text.encode("utf-8"),
        status=200,
        content_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )