"""佣金规则维护路由。"""

from __future__ import annotations

import sqlite3

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from app.auth import role_required
from app.services import rule_service


bp = Blueprint("rules", __name__)


def _integrity_message(error: sqlite3.IntegrityError) -> str:
    """把 SQLite 完整性约束错误转换为中文提示。"""
    detail = str(error).upper()
    if "UNIQUE" in detail:
        return "规则保存失败：规则代码和版本号已存在，请重试"
    if "FOREIGN KEY" in detail:
        return "规则保存失败：所选产品、销售顾问或当前用户不存在"
    if "CHECK" in detail:
        return "规则保存失败：日期、费率、固定额或维度目标不符合约束"
    return "规则保存失败：数据与现有记录冲突"


def _form_rule_id() -> int:
    """解析 POST 中的规则编号。"""
    raw = request.form.get("rule_id", "").strip()
    try:
        rule_id = int(raw)
    except ValueError:
        raise ValueError("规则编号无效") from None
    if rule_id <= 0:
        raise ValueError("规则编号无效")
    return rule_id


def _render_rules(
    *,
    form_data=None,
    source_rule_id: int | None = None,
    status_code: int = 200,
):
    """读取规则页面数据并渲染。"""
    context = rule_service.get_rules_page(
        form_data=form_data,
        source_rule_id=source_rule_id,
    )
    return render_template("rules.html", **context), status_code


@bp.route("/rules", methods=("GET", "POST"))
@role_required("合伙人")
def index():
    """显示规则版本列表，并处理新建、发布和停用。"""
    if request.method == "POST":
        action = request.form.get("action", "")
        try:
            if action == "create":
                _rule_id, version_no = rule_service.create_rule_draft(
                    request.form,
                    int(g.user["user_id"]),
                )
            elif action == "publish":
                rule_code, version_no, superseded = rule_service.publish_rule(_form_rule_id())
                suffix = "，同代码旧有效版本已停用" if superseded else ""
                flash(f"规则 {rule_code} v{version_no} 已发布{suffix}", "success")
                return redirect(url_for("rules.index"))
            elif action == "disable":
                rule_code, version_no = rule_service.disable_rule(_form_rule_id())
                flash(f"规则 {rule_code} v{version_no} 已停用", "success")
                return redirect(url_for("rules.index"))
            else:
                raise ValueError("未知操作")
        except ValueError as exc:
            flash(str(exc), "error")
            form_data = request.form if action == "create" else None
            return _render_rules(form_data=form_data)
        except sqlite3.IntegrityError as exc:
            flash(_integrity_message(exc), "error")
            form_data = request.form if action == "create" else None
            return _render_rules(form_data=form_data)

        rule_code = request.form.get("rule_code", "").strip()
        flash(f"规则草稿已创建：{rule_code} v{version_no}", "success")
        return redirect(url_for("rules.index"))

    source_rule_id: int | None = None
    raw_source_id = request.args.get("new_version", "").strip()
    if raw_source_id:
        try:
            source_rule_id = int(raw_source_id)
        except ValueError:
            source_rule_id = -1
        if source_rule_id <= 0:
            flash("要复制的新版本规则编号无效", "error")
            source_rule_id = None

    try:
        return _render_rules(source_rule_id=source_rule_id)
    except ValueError as exc:
        flash(str(exc), "error")
        return _render_rules()