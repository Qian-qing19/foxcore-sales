"""历史销售 CSV 导入路由。"""

from __future__ import annotations

import re

from flask import (
    Blueprint,
    Response,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from app.auth import role_required
from app.services import import_service


bp = Blueprint("imports", __name__)


def _safe_file_name(raw_name: str | None) -> str:
    """去掉浏览器可能提交的目录部分，仅保留文件名。"""
    return re.split(r"[\\/]", str(raw_name or ""))[-1]


def _render_import_page(
    *, import_result: import_service.ImportResult | None = None
) -> str:
    """渲染导入页与本次结果。"""
    context = import_service.get_import_page_context()
    errors: list[import_service.ImportRowError] = []
    if import_result is not None:
        errors = list(import_result.errors[: import_service.MAX_VISIBLE_ERRORS])
    return render_template(
        "import.html",
        **context,
        import_result=import_result,
        import_errors=errors,
    )


@bp.route("/import", methods=("GET", "POST"))
@role_required("合伙人")
def index():
    """显示 CSV 导入页并处理合伙人上传。"""
    if request.method == "POST":
        upload = request.files.get("csv_file")
        if upload is None or not upload.filename:
            flash("请选择要导入的 CSV 文件", "error")
            return _render_import_page(), 200

        file_name = _safe_file_name(upload.filename)
        content = upload.read(import_service.MAX_IMPORT_BYTES + 1)
        try:
            result = import_service.import_sales_csv(
                file_name,
                content,
                int(g.user["user_id"]),
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return _render_import_page(), 200

        summary = (
            f"导入{result.status}：文件 {result.file_name}，共 {result.row_count} 行，"
            f"成功 {result.success_count} 行，跳过 {result.skipped_count} 行，"
            f"失败 {result.error_count} 行。"
        )
        if result.error_count:
            flash(summary, "error")
            return _render_import_page(import_result=result), 200

        flash(summary, "success")
        return redirect(url_for("imports.index"))

    return _render_import_page()


@bp.get("/import/template")
@role_required("合伙人")
def download_template() -> Response:
    """下载带 BOM 和两行示例的历史销售 CSV 模板。"""
    return Response(
        import_service.build_import_template(),
        status=200,
        content_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="sales_import_template.csv"',
        },
    )
