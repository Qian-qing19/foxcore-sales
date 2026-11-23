"""佣金规则维护服务。

本模块负责规则草稿创建、发布、停用以及页面所需的选项与展示数据。
不包含销售明细的规则匹配或佣金计算逻辑。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
import sqlite3
from typing import Any

from app.db import get_db


DIMENSION_LABELS: dict[str, str] = {
    "PRODUCT": "产品",
    "EVENT_TYPE": "活动类型",
    "SALESPERSON": "销售顾问",
    "GLOBAL": "全局",
}
CALC_METHODS = ("按比例", "按件固定额")


def _clean(value: Any) -> str:
    """把表单值规范化为去除首尾空白的字符串。"""
    return str(value or "").strip()


def _required(form: Mapping[str, Any], field_name: str, label: str) -> str:
    """读取必填文本字段，空值时抛出中文校验错误。"""
    value = _clean(form.get(field_name))
    if not value:
        raise ValueError(f"{label}不能为空")
    return value


def _parse_date(raw: Any, label: str, *, required: bool) -> str | None:
    """校验日期并规范为 YYYY-MM-DD；可选日期空值返回 None。"""
    value = _clean(raw)
    if not value:
        if required:
            raise ValueError(f"{label}不能为空")
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"{label}格式无效") from None
    return parsed.isoformat()


def _parse_priority(raw: Any) -> int:
    """解析整数优先级，空值按 0 处理。"""
    value = _clean(raw)
    if not value:
        return 0
    try:
        return int(value)
    except ValueError:
        raise ValueError("优先级必须是整数") from None


def _parse_rate(raw: Any) -> float:
    """把百分比输入转换为 0~1 的比例，例如 5 转为 0.05。"""
    value = _clean(raw)
    if not value:
        raise ValueError("费率不能为空")
    try:
        percent = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError("费率必须是有效数字") from None
    if not percent.is_finite() or percent < 0 or percent > 100:
        raise ValueError("费率必须在 0 到 100 之间")
    return float(percent / Decimal("100"))


def _parse_fixed_amount(raw: Any) -> int:
    """把固定额元输入精确转换为分。"""
    value = _clean(raw)
    if not value:
        raise ValueError("固定额不能为空")
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError("固定额必须是有效金额") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("固定额必须大于等于 0")
    cents = amount * Decimal("100")
    if cents != cents.to_integral_value():
        raise ValueError("固定额最多保留两位小数")
    return int(cents)


def _form_list(form: Mapping[str, Any], field_name: str) -> list[str]:
    """读取多选表单值，兼容 MultiDict 与普通字典。"""
    getlist = getattr(form, "getlist", None)
    if callable(getlist):
        raw_values = list(getlist(field_name))
    else:
        raw_value = form.get(field_name, [])
        raw_values = list(raw_value) if isinstance(raw_value, (list, tuple)) else [raw_value]
    return [_clean(value) for value in raw_values if _clean(value)]


def _parse_target_ids(form: Mapping[str, Any], field_name: str, label: str) -> list[int]:
    """解析并去重多选目标编号。"""
    ids: list[int] = []
    for value in _form_list(form, field_name):
        try:
            record_id = int(value)
        except ValueError:
            raise ValueError(f"{label}目标编号无效") from None
        if record_id <= 0:
            raise ValueError(f"{label}目标编号无效")
        if record_id not in ids:
            ids.append(record_id)
    return ids


def _parse_rule_id(raw: Any) -> int:
    """解析规则编号。"""
    value = _clean(raw)
    try:
        rule_id = int(value)
    except ValueError:
        raise ValueError("规则编号无效") from None
    if rule_id <= 0:
        raise ValueError("规则编号无效")
    return rule_id


def _validate_target_rows(
    db: sqlite3.Connection,
    dimension: str,
    target_ids: list[int],
) -> None:
    """确认产品/顾问目标确实存在。"""
    if dimension not in ("PRODUCT", "SALESPERSON"):
        return
    if not target_ids:
        label = DIMENSION_LABELS[dimension]
        raise ValueError(f"{label}维度至少选择一个目标")
    table = "PRODUCT" if dimension == "PRODUCT" else "SALESPERSON"
    id_column = "product_id" if dimension == "PRODUCT" else "salesperson_id"
    placeholders = ", ".join("?" for _ in target_ids)
    rows = db.execute(
        f"SELECT {id_column} FROM {table} WHERE {id_column} IN ({placeholders})",
        target_ids,
    ).fetchall()
    if len(rows) != len(target_ids):
        label = DIMENSION_LABELS[dimension]
        raise ValueError(f"所选{label}不存在")


def _build_rule_values(form: Mapping[str, Any]) -> dict[str, Any]:
    """校验并构造规则主表写入值。"""
    rule_code = _required(form, "rule_code", "规则代码")
    dimension = _required(form, "dimension", "维度")
    if dimension not in DIMENSION_LABELS:
        raise ValueError("维度无效")

    event_type_value: str | None = None
    if dimension == "EVENT_TYPE":
        event_type_value = _required(form, "event_type_value", "活动类型")
    elif dimension != "EVENT_TYPE":
        event_type_value = None

    calc_method = _required(form, "calc_method", "计算方式")
    if calc_method not in CALC_METHODS:
        raise ValueError("计算方式无效")

    rate: float | None = None
    fixed_per_unit_cents: int | None = None
    if calc_method == "按比例":
        rate = _parse_rate(form.get("rate"))
    else:
        fixed_per_unit_cents = _parse_fixed_amount(form.get("fixed_amount"))

    effective_from = _parse_date(form.get("effective_from"), "生效开始日期", required=True)
    effective_to = _parse_date(form.get("effective_to"), "生效结束日期", required=False)
    if effective_from is None:
        raise ValueError("生效开始日期不能为空")
    if effective_to is not None and effective_to < effective_from:
        raise ValueError("生效结束日期不能早于开始日期")

    product_ids = (
        _parse_target_ids(form, "product_ids", "产品") if dimension == "PRODUCT" else []
    )
    salesperson_ids = (
        _parse_target_ids(form, "salesperson_ids", "销售顾问")
        if dimension == "SALESPERSON"
        else []
    )
    if dimension == "PRODUCT" and not product_ids:
        raise ValueError("产品维度至少选择一个产品")
    if dimension == "SALESPERSON" and not salesperson_ids:
        raise ValueError("销售顾问维度至少选择一个销售顾问")

    return {
        "rule_code": rule_code,
        "dimension": dimension,
        "event_type_value": event_type_value,
        "calc_method": calc_method,
        "rate": rate,
        "fixed_per_unit_cents": fixed_per_unit_cents,
        "priority": _parse_priority(form.get("priority")),
        "effective_from": effective_from,
        "effective_to": effective_to,
        "product_ids": product_ids,
        "salesperson_ids": salesperson_ids,
    }


def _insert_rule_targets(
    db: sqlite3.Connection,
    rule_id: int,
    product_ids: list[int],
    salesperson_ids: list[int],
) -> None:
    """写入规则与产品/销售顾问的关联记录。"""
    if product_ids:
        db.executemany(
            "INSERT INTO PRODUCT_COMMISSION_RULE (product_id, rule_id) VALUES (?, ?)",
            [(product_id, rule_id) for product_id in product_ids],
        )
    if salesperson_ids:
        db.executemany(
            "INSERT INTO SALESPERSON_COMMISSION_RULE (salesperson_id, rule_id) VALUES (?, ?)",
            [(salesperson_id, rule_id) for salesperson_id in salesperson_ids],
        )


def create_rule_draft(form: Mapping[str, Any], created_by: int) -> tuple[int, int]:
    """校验表单并创建草稿；同一规则代码的版本号自动递增。"""
    if created_by <= 0:
        raise ValueError("当前用户无效")
    values = _build_rule_values(form)
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        target_ids = (
            values["product_ids"]
            if values["dimension"] == "PRODUCT"
            else values["salesperson_ids"]
        )
        _validate_target_rows(db, values["dimension"], target_ids)
        version_no = int(
            db.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 "
                "FROM COMMISSION_RULE WHERE rule_code = ?",
                (values["rule_code"],),
            ).fetchone()[0]
        )
        cursor = db.execute(
            "INSERT INTO COMMISSION_RULE "
            "(rule_code, version_no, dimension, event_type_value, calc_method, rate, "
            "fixed_per_unit_cents, priority, effective_from, effective_to, status, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '草稿', ?)",
            (
                values["rule_code"],
                version_no,
                values["dimension"],
                values["event_type_value"],
                values["calc_method"],
                values["rate"],
                values["fixed_per_unit_cents"],
                values["priority"],
                values["effective_from"],
                values["effective_to"],
                created_by,
            ),
        )
        rule_id = int(cursor.lastrowid)
        _insert_rule_targets(
            db,
            rule_id,
            values["product_ids"],
            values["salesperson_ids"],
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return rule_id, version_no


def _target_ids_for_rule(db: sqlite3.Connection, rule_id: int, dimension: str) -> list[int]:
    """读取规则关联的产品或销售顾问编号。"""
    if dimension == "PRODUCT":
        rows = db.execute(
            "SELECT product_id FROM PRODUCT_COMMISSION_RULE WHERE rule_id = ? ORDER BY product_id",
            (rule_id,),
        ).fetchall()
    elif dimension == "SALESPERSON":
        rows = db.execute(
            "SELECT salesperson_id FROM SALESPERSON_COMMISSION_RULE "
            "WHERE rule_id = ? ORDER BY salesperson_id",
            (rule_id,),
        ).fetchall()
    else:
        return []
    return [int(row[0]) for row in rows]


def _validate_rule_for_publish(db: sqlite3.Connection, rule: sqlite3.Row) -> None:
    """发布前再次校验已存储规则的数据完整性和业务约束。"""
    rule_id = int(rule["rule_id"])
    dimension = str(rule["dimension"])
    if dimension not in DIMENSION_LABELS:
        raise ValueError("规则维度无效")

    effective_from = _parse_date(rule["effective_from"], "生效开始日期", required=True)
    effective_to = _parse_date(rule["effective_to"], "生效结束日期", required=False)
    if effective_from is None:
        raise ValueError("生效开始日期不能为空")
    if effective_to is not None and effective_to < effective_from:
        raise ValueError("生效结束日期不能早于开始日期")

    if dimension == "EVENT_TYPE":
        if not _clean(rule["event_type_value"]):
            raise ValueError("活动类型维度必须填写活动类型")
    elif rule["event_type_value"] is not None:
        raise ValueError("非活动类型维度的活动类型必须为空")

    calc_method = str(rule["calc_method"])
    if calc_method == "按比例":
        if rule["rate"] is None or rule["fixed_per_unit_cents"] is not None:
            raise ValueError("按比例规则的费率或固定额字段不合法")
        rate = Decimal(str(rule["rate"]))
        if not rate.is_finite() or rate < 0 or rate > 1:
            raise ValueError("费率必须在 0 到 100 之间")
    elif calc_method == "按件固定额":
        if rule["fixed_per_unit_cents"] is None or rule["rate"] is not None:
            raise ValueError("按件固定额规则的费率或固定额字段不合法")
        if int(rule["fixed_per_unit_cents"]) < 0:
            raise ValueError("固定额必须大于等于 0")
    else:
        raise ValueError("计算方式无效")

    if dimension in ("PRODUCT", "SALESPERSON"):
        target_ids = _target_ids_for_rule(db, rule_id, dimension)
        _validate_target_rows(db, dimension, target_ids)


def publish_rule(rule_id: int) -> tuple[str, int, int]:
    """发布草稿，并在同一事务内停用同代码旧的有效版本。"""
    rule_id = _parse_rule_id(rule_id)
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        rule = db.execute(
            "SELECT rule_id, rule_code, version_no, dimension, event_type_value, "
            "calc_method, rate, fixed_per_unit_cents, effective_from, effective_to, status "
            "FROM COMMISSION_RULE WHERE rule_id = ?",
            (rule_id,),
        ).fetchone()
        if rule is None:
            raise ValueError("规则不存在")
        if rule["status"] != "草稿":
            raise ValueError("只有草稿规则可以发布")
        _validate_rule_for_publish(db, rule)

        cursor = db.execute(
            "UPDATE COMMISSION_RULE SET status = '停用' "
            "WHERE rule_code = ? AND status = '有效' AND rule_id <> ?",
            (rule["rule_code"], rule_id),
        )
        superseded = max(cursor.rowcount, 0)
        cursor = db.execute(
            "UPDATE COMMISSION_RULE SET status = '有效' "
            "WHERE rule_id = ? AND status = '草稿'",
            (rule_id,),
        )
        if cursor.rowcount != 1:
            raise ValueError("规则状态已变化，请刷新后重试")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return str(rule["rule_code"]), int(rule["version_no"]), superseded


def disable_rule(rule_id: int) -> tuple[str, int]:
    """停用有效规则；只改状态，不删除历史数据。"""
    rule_id = _parse_rule_id(rule_id)
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        rule = db.execute(
            "SELECT rule_code, version_no, status FROM COMMISSION_RULE WHERE rule_id = ?",
            (rule_id,),
        ).fetchone()
        if rule is None:
            raise ValueError("规则不存在")
        if rule["status"] != "有效":
            raise ValueError("只有有效规则可以停用")
        db.execute(
            "UPDATE COMMISSION_RULE SET status = '停用' WHERE rule_id = ? AND status = '有效'",
            (rule_id,),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return str(rule["rule_code"]), int(rule["version_no"])


def _rate_input_value(rate: Any) -> str:
    """把数据库比例格式化为百分比输入值。"""
    if rate is None:
        return ""
    percent = Decimal(str(rate)) * Decimal("100")
    return format(percent.normalize(), "f")


def _fixed_input_value(cents: Any) -> str:
    """把分格式化为元输入值。"""
    if cents is None:
        return ""
    return f"{Decimal(int(cents)) / Decimal('100'):.2f}"


def _rate_display_value(rate: Any) -> str:
    """把数据库比例格式化为页面展示百分比。"""
    percent = Decimal(str(rate)) * Decimal("100")
    return f"{format(percent.normalize(), 'f')}%"


def _default_form_values() -> dict[str, Any]:
    """返回新建规则表单默认值。"""
    return {
        "rule_code": "",
        "dimension": "GLOBAL",
        "event_type_value": "",
        "calc_method": "按比例",
        "rate": "",
        "fixed_amount": "",
        "priority": "0",
        "effective_from": "",
        "effective_to": "",
        "product_ids": [],
        "salesperson_ids": [],
    }



def _form_values_from_submission(form: Mapping[str, Any]) -> dict[str, Any]:
    """从提交表单构造失败回显数据。"""
    values = _default_form_values()
    for field in (
        "rule_code",
        "dimension",
        "event_type_value",
        "calc_method",
        "rate",
        "fixed_amount",
        "priority",
        "effective_from",
        "effective_to",
    ):
        if field in form:
            values[field] = _clean(form.get(field))
    values["product_ids"] = _form_list(form, "product_ids")
    values["salesperson_ids"] = _form_list(form, "salesperson_ids")
    return values


def _form_values_from_source(db: sqlite3.Connection, source_rule_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """复制历史规则的业务字段和关联目标，但不复制编号、版本和状态。"""
    rule = db.execute(
        "SELECT rule_id, rule_code, version_no, dimension, event_type_value, calc_method, "
        "rate, fixed_per_unit_cents, priority, effective_from, effective_to, status "
        "FROM COMMISSION_RULE WHERE rule_id = ?",
        (source_rule_id,),
    ).fetchone()
    if rule is None:
        raise ValueError("要复制的新版本规则不存在")
    dimension = str(rule["dimension"])
    values = {
        "rule_code": rule["rule_code"],
        "dimension": dimension,
        "event_type_value": rule["event_type_value"] or "",
        "calc_method": rule["calc_method"],
        "rate": _rate_input_value(rule["rate"]),
        "fixed_amount": _fixed_input_value(rule["fixed_per_unit_cents"]),
        "priority": str(rule["priority"]),
        "effective_from": rule["effective_from"],
        "effective_to": rule["effective_to"] or "",
        "product_ids": [
            str(value) for value in _target_ids_for_rule(db, source_rule_id, "PRODUCT")
        ],
        "salesperson_ids": [
            str(value) for value in _target_ids_for_rule(db, source_rule_id, "SALESPERSON")
        ],
    }
    source = {
        "rule_code": rule["rule_code"],
        "version_no": rule["version_no"],
        "status": rule["status"],
    }
    return values, source


def get_rules_page(
    *,
    form_data: Mapping[str, Any] | None = None,
    source_rule_id: int | None = None,
) -> dict[str, Any]:
    """查询规则列表、表单选项及新建/复制版本的初始值。"""
    db = get_db()
    rules = [
        dict(row)
        for row in db.execute(
            "SELECT rule_id, rule_code, version_no, dimension, event_type_value, calc_method, "
            "rate, fixed_per_unit_cents, priority, effective_from, effective_to, status, "
            "created_by, created_at FROM COMMISSION_RULE "
            "ORDER BY rule_code, version_no DESC, rule_id DESC"
        ).fetchall()
    ]

    product_scopes: dict[int, list[str]] = {}
    for row in db.execute(
        "SELECT r.rule_id, p.product_id, p.product_code, p.name "
        "FROM PRODUCT_COMMISSION_RULE AS r "
        "JOIN PRODUCT AS p ON p.product_id = r.product_id "
        "ORDER BY p.product_code"
    ).fetchall():
        product_scopes.setdefault(int(row["rule_id"]), []).append(
            f"{row['product_code']} {row['name']}"
        )

    salesperson_scopes: dict[int, list[str]] = {}
    for row in db.execute(
        "SELECT r.rule_id, s.salesperson_id, s.emp_code, s.name "
        "FROM SALESPERSON_COMMISSION_RULE AS r "
        "JOIN SALESPERSON AS s ON s.salesperson_id = r.salesperson_id "
        "ORDER BY s.emp_code"
    ).fetchall():
        salesperson_scopes.setdefault(int(row["rule_id"]), []).append(
            f"{row['emp_code']} {row['name']}"
        )

    for rule in rules:
        dimension = str(rule["dimension"])
        rule["dimension_label"] = DIMENSION_LABELS.get(dimension, dimension)
        if dimension == "PRODUCT":
            rule["scope_text"] = "、".join(product_scopes.get(int(rule["rule_id"]), [])) or "—"
        elif dimension == "SALESPERSON":
            rule["scope_text"] = (
                "、".join(salesperson_scopes.get(int(rule["rule_id"]), [])) or "—"
            )
        elif dimension == "EVENT_TYPE":
            rule["scope_text"] = rule["event_type_value"] or "—"
        else:
            rule["scope_text"] = "全部"
        if rule["calc_method"] == "按比例":
            rule["value_text"] = _rate_display_value(rule["rate"])
        else:
            rule["value_text"] = (
                f"{Decimal(int(rule['fixed_per_unit_cents'])) / Decimal('100'):.2f} 元/件"
            )

    products = [
        dict(row)
        for row in db.execute(
            "SELECT product_id, product_code, name FROM PRODUCT "
            "ORDER BY product_code, product_id"
        ).fetchall()
    ]
    salespersons = [
        dict(row)
        for row in db.execute(
            "SELECT salesperson_id, emp_code, name FROM SALESPERSON "
            "ORDER BY emp_code, salesperson_id"
        ).fetchall()
    ]
    event_types = [
        str(row["event_type"])
        for row in db.execute(
            "SELECT DISTINCT event_type FROM EVENT "
            "WHERE event_type IS NOT NULL AND TRIM(event_type) <> '' "
            "ORDER BY event_type"
        ).fetchall()
    ]

    source: dict[str, Any] | None = None
    if form_data is not None:
        form_values = _form_values_from_submission(form_data)
    elif source_rule_id is not None:
        form_values, source = _form_values_from_source(db, _parse_rule_id(source_rule_id))
    else:
        form_values = _default_form_values()

    return {
        "rules": rules,
        "products": products,
        "salespersons": salespersons,
        "event_types": event_types,
        "dimension_options": [
            {"value": value, "label": label}
            for value, label in DIMENSION_LABELS.items()
        ],
        "calc_methods": CALC_METHODS,
        "form_values": form_values,
        "copy_source": source,
    }