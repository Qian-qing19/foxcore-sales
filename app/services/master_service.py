"""基础数据维护服务。

本模块集中承载产品、活动、摊位、销售顾问和班次的查询与写入逻辑。
路由层只负责参数解析、鉴权和页面渲染。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.db import get_db


TAB_LABELS: dict[str, str] = {
    "product": "产品",
    "event": "活动",
    "booth": "摊位",
    "salesperson": "销售顾问",
    "shift": "班次",
}
PRODUCT_STATUSES = ("在售", "停用")
BOOTH_STATUSES = ("在用", "停用")
SALESPERSON_STATUSES = ("在职", "停用")
SHIFT_STATUSES = ("未开始", "进行中", "已结束", "已结算")
CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _clean(value: Any) -> str:
    """把表单值规范化为去除首尾空白的字符串。"""
    return str(value or "").strip()


def _required(form: Mapping[str, str], field_name: str, label: str) -> str:
    """读取必填文本字段，空值时抛出中文校验错误。"""
    value = _clean(form.get(field_name))
    if not value:
        raise ValueError(f"{label}不能为空")
    return value


def _status(form: Mapping[str, str], allowed: tuple[str, ...], label: str) -> str:
    """读取并校验状态枚举。"""
    value = _clean(form.get("status"))
    if value not in allowed:
        raise ValueError(f"{label}状态无效")
    return value


def _parse_yuan_to_cents(raw: Any) -> int:
    """把元金额精确转换为分，拒绝非法值及超过两位的小数。"""
    value = _clean(raw)
    if not value:
        raise ValueError("最低售价不能为空")
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError("最低售价必须是有效金额") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("最低售价必须大于等于 0")
    if amount.as_tuple().exponent < -2:
        raise ValueError("最低售价最多保留两位小数")
    cents = amount * Decimal("100")
    if cents != cents.to_integral_value():
        raise ValueError("最低售价最多保留两位小数")
    return int(cents)


def _parse_record_id(record_id: int | None) -> int | None:
    """校验可选的记录编号。"""
    if record_id is None:
        return None
    if record_id <= 0:
        raise ValueError("记录编号无效")
    return record_id


def _parse_reference_id(form: Mapping[str, str], field_name: str, label: str) -> int:
    """解析必填的外键编号。"""
    raw = _clean(form.get(field_name))
    if not raw:
        raise ValueError(f"{label}不能为空")
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{label}编号无效") from None
    if value <= 0:
        raise ValueError(f"{label}编号无效")
    return value


def _parse_date(raw: Any, label: str) -> str:
    """把 HTML date 值校验并规范为 YYYY-MM-DD。"""
    value = _clean(raw)
    if not value:
        raise ValueError(f"{label}不能为空")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"{label}格式无效") from None
    return parsed.date().isoformat()


def _parse_datetime_local(raw: Any, label: str) -> str:
    """把 datetime-local 值转换为数据库约定的秒级本地时间。"""
    value = _clean(raw)
    if not value:
        raise ValueError(f"{label}不能为空")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{label}格式无效") from None
    if parsed.tzinfo is not None:
        raise ValueError(f"{label}必须是本地时间")
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _to_datetime_local(value: str) -> str:
    """把数据库时间转换为 datetime-local 表单值。"""
    parsed = datetime.fromisoformat(value)
    return parsed.strftime("%Y-%m-%dT%H:%M")


def _product_form_values(form_data: Mapping[str, str] | None = None) -> dict[str, str]:
    """返回产品表单默认值，并用提交数据覆盖以保留错误输入。"""
    values = {
        "product_code": "",
        "name": "",
        "category": "",
        "min_price": "0.00",
        "status": "在售",
    }
    if form_data is not None:
        for key in values:
            if key in form_data:
                values[key] = _clean(form_data.get(key))
    return values


def _event_form_values(form_data: Mapping[str, str] | None = None) -> dict[str, str]:
    """返回活动表单默认值，并用提交数据覆盖以保留错误输入。"""
    values = {
        "event_code": "",
        "event_name": "",
        "event_type": "",
        "venue_id": "",
        "start_date": "",
        "end_date": "",
        "description": "",
    }
    if form_data is not None:
        for key in values:
            if key in form_data:
                values[key] = _clean(form_data.get(key))
    return values


def _booth_form_values(form_data: Mapping[str, str] | None = None) -> dict[str, str]:
    """返回摊位表单默认值，并用提交数据覆盖以保留错误输入。"""
    values = {
        "event_id": "",
        "booth_code": "",
        "location": "",
        "status": "在用",
    }
    if form_data is not None:
        for key in values:
            if key in form_data:
                values[key] = _clean(form_data.get(key))
    return values


def _salesperson_form_values(form_data: Mapping[str, str] | None = None) -> dict[str, str]:
    """返回销售顾问表单默认值，并用提交数据覆盖以保留错误输入。"""
    values = {
        "emp_code": "",
        "name": "",
        "phone": "",
        "status": "在职",
    }
    if form_data is not None:
        for key in values:
            if key in form_data:
                values[key] = _clean(form_data.get(key))
    return values


def _shift_form_values(form_data: Mapping[str, str] | None = None) -> dict[str, str]:
    """返回班次表单默认值，并用提交数据覆盖以保留错误输入。"""
    values = {
        "salesperson_id": "",
        "booth_id": "",
        "start_time": "",
        "end_time": "",
        "status": "未开始",
    }
    if form_data is not None:
        for key in values:
            if key in form_data:
                values[key] = _clean(form_data.get(key))
    return values


def get_master_page(
    tab: str | None,
    edit_id: int | None = None,
    form_data: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """读取当前 Tab 的列表、编辑记录、表单值和关联选项。"""
    active_tab = tab if tab in TAB_LABELS else "product"
    edit_id = _parse_record_id(edit_id)
    db = get_db()
    records: list[dict[str, Any]] = []
    edit_record: dict[str, Any] | None = None
    form_values: dict[str, str] = {}
    options: dict[str, list[dict[str, Any]]] = {}

    if active_tab == "product":
        records = [
            dict(row)
            for row in db.execute(
                "SELECT product_id, product_code, name, category, min_price_cents, status "
                "FROM PRODUCT ORDER BY product_id"
            ).fetchall()
        ]
        form_values = _product_form_values(form_data)
        if edit_id is not None:
            row = db.execute(
                "SELECT product_id, product_code, name, category, min_price_cents, status "
                "FROM PRODUCT WHERE product_id = ?",
                (edit_id,),
            ).fetchone()
            if row is None:
                raise ValueError("要编辑的产品不存在")
            edit_record = dict(row)
            if form_data is None:
                form_values = {
                    "product_code": row["product_code"],
                    "name": row["name"],
                    "category": row["category"] or "",
                    "min_price": f"{Decimal(row['min_price_cents']) / Decimal('100'):.2f}",
                    "status": row["status"],
                }
    elif active_tab == "event":
        records = [
            dict(row)
            for row in db.execute(
                "SELECT e.event_id, e.event_code, e.event_name, e.event_type, "
                "e.venue_id, e.start_date, e.end_date, e.description, v.name AS venue_name "
                "FROM EVENT AS e JOIN VENUE AS v ON v.venue_id = e.venue_id "
                "ORDER BY e.start_date DESC, e.event_id DESC"
            ).fetchall()
        ]
        options["venues"] = [
            dict(row)
            for row in db.execute(
                "SELECT venue_id, venue_code, name FROM VENUE ORDER BY name, venue_id"
            ).fetchall()
        ]
        form_values = _event_form_values(form_data)
        if edit_id is not None:
            row = db.execute(
                "SELECT event_id, event_code, event_name, event_type, venue_id, "
                "start_date, end_date, description FROM EVENT WHERE event_id = ?",
                (edit_id,),
            ).fetchone()
            if row is None:
                raise ValueError("要编辑的活动不存在")
            edit_record = dict(row)
            if form_data is None:
                form_values = {
                    "event_code": row["event_code"],
                    "event_name": row["event_name"],
                    "event_type": row["event_type"],
                    "venue_id": str(row["venue_id"]),
                    "start_date": row["start_date"],
                    "end_date": row["end_date"],
                    "description": row["description"] or "",
                }
    elif active_tab == "booth":
        records = [
            dict(row)
            for row in db.execute(
                "SELECT b.booth_id, b.event_id, b.booth_code, b.location, b.status, "
                "e.event_code, e.event_name "
                "FROM BOOTH AS b JOIN EVENT AS e ON e.event_id = b.event_id "
                "ORDER BY e.start_date DESC, b.booth_code, b.booth_id"
            ).fetchall()
        ]
        options["events"] = [
            dict(row)
            for row in db.execute(
                "SELECT event_id, event_code, event_name, start_date, end_date "
                "FROM EVENT ORDER BY start_date DESC, event_id DESC"
            ).fetchall()
        ]
        form_values = _booth_form_values(form_data)
        if edit_id is not None:
            row = db.execute(
                "SELECT booth_id, event_id, booth_code, location, status "
                "FROM BOOTH WHERE booth_id = ?",
                (edit_id,),
            ).fetchone()
            if row is None:
                raise ValueError("要编辑的摊位不存在")
            edit_record = dict(row)
            if form_data is None:
                form_values = {
                    "event_id": str(row["event_id"]),
                    "booth_code": row["booth_code"],
                    "location": row["location"] or "",
                    "status": row["status"],
                }
    elif active_tab == "salesperson":
        records = [
            dict(row)
            for row in db.execute(
                "SELECT salesperson_id, emp_code, name, phone, status, hired_at "
                "FROM SALESPERSON ORDER BY salesperson_id"
            ).fetchall()
        ]
        form_values = _salesperson_form_values(form_data)
        if edit_id is not None:
            row = db.execute(
                "SELECT salesperson_id, emp_code, name, phone, status, hired_at "
                "FROM SALESPERSON WHERE salesperson_id = ?",
                (edit_id,),
            ).fetchone()
            if row is None:
                raise ValueError("要编辑的销售顾问不存在")
            edit_record = dict(row)
            if form_data is None:
                form_values = {
                    "emp_code": row["emp_code"],
                    "name": row["name"],
                    "phone": row["phone"] or "",
                    "status": row["status"],
                }
    elif active_tab == "shift":
        records = [
            dict(row)
            for row in db.execute(
                "SELECT s.shift_id, s.salesperson_id, s.booth_id, s.start_time, "
                "s.end_time, s.status, sp.name AS salesperson_name, b.booth_code, "
                "e.event_name "
                "FROM SHIFT AS s "
                "JOIN SALESPERSON AS sp ON sp.salesperson_id = s.salesperson_id "
                "JOIN BOOTH AS b ON b.booth_id = s.booth_id "
                "JOIN EVENT AS e ON e.event_id = b.event_id "
                "ORDER BY s.start_time DESC, s.shift_id DESC"
            ).fetchall()
        ]
        options["salespersons"] = [
            dict(row)
            for row in db.execute(
                "SELECT salesperson_id, emp_code, name, status "
                "FROM SALESPERSON ORDER BY name, salesperson_id"
            ).fetchall()
        ]
        options["booths"] = [
            dict(row)
            for row in db.execute(
                "SELECT b.booth_id, b.booth_code, b.status, e.event_name "
                "FROM BOOTH AS b JOIN EVENT AS e ON e.event_id = b.event_id "
                "ORDER BY e.start_date DESC, b.booth_code, b.booth_id"
            ).fetchall()
        ]
        form_values = _shift_form_values(form_data)
        if edit_id is not None:
            row = db.execute(
                "SELECT shift_id, salesperson_id, booth_id, start_time, end_time, status "
                "FROM SHIFT WHERE shift_id = ?",
                (edit_id,),
            ).fetchone()
            if row is None:
                raise ValueError("要编辑的班次不存在")
            edit_record = dict(row)
            if form_data is None:
                form_values = {
                    "salesperson_id": str(row["salesperson_id"]),
                    "booth_id": str(row["booth_id"]),
                    "start_time": _to_datetime_local(row["start_time"]),
                    "end_time": _to_datetime_local(row["end_time"]),
                    "status": row["status"],
                }

    return {
        "tab": active_tab,
        "tabs": list(TAB_LABELS.items()),
        "records": records,
        "edit_record": edit_record,
        "form_values": form_values,
        "options": options,
        "product_statuses": PRODUCT_STATUSES,
        "booth_statuses": BOOTH_STATUSES,
        "salesperson_statuses": SALESPERSON_STATUSES,
        "shift_statuses": SHIFT_STATUSES,
    }


def save_master_record(
    tab: str,
    form: Mapping[str, str],
    record_id: int | None = None,
) -> str:
    """新增或更新指定 Tab 的基础数据，成功时返回中文提示。"""
    record_id = _parse_record_id(record_id)
    if tab == "product":
        return _save_product(form, record_id)
    if tab == "event":
        return _save_event(form, record_id)
    if tab == "booth":
        return _save_booth(form, record_id)
    if tab == "salesperson":
        return _save_salesperson(form, record_id)
    if tab == "shift":
        return _save_shift(form, record_id)
    raise ValueError("未知的基础数据类型")


def toggle_master_status(tab: str, record_id: int) -> str:
    """切换产品、摊位或销售顾问的启用状态，并返回中文提示。"""
    record_id = _parse_record_id(record_id)
    if record_id is None:
        raise ValueError("记录编号不能为空")
    db = get_db()

    if tab == "product":
        row = db.execute(
            "SELECT status FROM PRODUCT WHERE product_id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            raise ValueError("产品不存在")
        next_status = "停用" if row["status"] == "在售" else "在售"
        with db:
            db.execute(
                "UPDATE PRODUCT SET status = ? WHERE product_id = ?",
                (next_status, record_id),
            )
        return f"产品已{next_status}"

    if tab == "booth":
        row = db.execute(
            "SELECT status FROM BOOTH WHERE booth_id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            raise ValueError("摊位不存在")
        next_status = "停用" if row["status"] == "在用" else "在用"
        with db:
            db.execute(
                "UPDATE BOOTH SET status = ? WHERE booth_id = ?",
                (next_status, record_id),
            )
        return f"摊位已{next_status}"

    if tab == "salesperson":
        row = db.execute(
            "SELECT status FROM SALESPERSON WHERE salesperson_id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            raise ValueError("销售顾问不存在")
        next_status = "停用" if row["status"] == "在职" else "在职"
        with db:
            db.execute(
                "UPDATE SALESPERSON SET status = ? WHERE salesperson_id = ?",
                (next_status, record_id),
            )
        return f"销售顾问已{next_status}"

    raise ValueError("该基础数据不支持状态切换")


def _save_product(form: Mapping[str, str], record_id: int | None) -> str:
    """新增或更新产品记录。"""
    product_code = _required(form, "product_code", "产品编码")
    name = _required(form, "name", "产品名称")
    category = _clean(form.get("category"))
    min_price_cents = _parse_yuan_to_cents(form.get("min_price"))
    status = _status(form, PRODUCT_STATUSES, "产品")
    db = get_db()

    with db:
        if record_id is None:
            db.execute(
                "INSERT INTO PRODUCT "
                "(product_code, name, category, min_price_cents, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (product_code, name, category, min_price_cents, status),
            )
            return "产品新增成功"
        cursor = db.execute(
            "UPDATE PRODUCT SET product_code = ?, name = ?, category = ?, "
            "min_price_cents = ?, status = ? WHERE product_id = ?",
            (product_code, name, category, min_price_cents, status, record_id),
        )
        if cursor.rowcount == 0:
            raise ValueError("要编辑的产品不存在")
    return "产品更新成功"


def _save_event(form: Mapping[str, str], record_id: int | None) -> str:
    """新增或更新活动记录。"""
    event_code = _required(form, "event_code", "活动编码")
    event_name = _required(form, "event_name", "活动名称")
    event_type = _required(form, "event_type", "活动类型")
    venue_id = _parse_reference_id(form, "venue_id", "场地")
    start_date = _parse_date(form.get("start_date"), "开始日期")
    end_date = _parse_date(form.get("end_date"), "结束日期")
    description = _clean(form.get("description"))
    if start_date > end_date:
        raise ValueError("结束日期不能早于开始日期")
    db = get_db()

    if db.execute("SELECT 1 FROM VENUE WHERE venue_id = ?", (venue_id,)).fetchone() is None:
        raise ValueError("所选场地不存在")
    with db:
        if record_id is None:
            db.execute(
                "INSERT INTO EVENT "
                "(venue_id, event_code, event_name, event_type, start_date, end_date, description) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (venue_id, event_code, event_name, event_type, start_date, end_date, description),
            )
            return "活动新增成功"
        cursor = db.execute(
            "UPDATE EVENT SET venue_id = ?, event_code = ?, event_name = ?, "
            "event_type = ?, start_date = ?, end_date = ?, description = ? "
            "WHERE event_id = ?",
            (venue_id, event_code, event_name, event_type, start_date, end_date, description, record_id),
        )
        if cursor.rowcount == 0:
            raise ValueError("要编辑的活动不存在")
    return "活动更新成功"


def _save_booth(form: Mapping[str, str], record_id: int | None) -> str:
    """新增或更新摊位记录，组合唯一冲突由数据库约束兜底。"""
    event_id = _parse_reference_id(form, "event_id", "活动")
    booth_code = _required(form, "booth_code", "摊位编号")
    location = _clean(form.get("location"))
    status = _status(form, BOOTH_STATUSES, "摊位")
    db = get_db()

    if db.execute("SELECT 1 FROM EVENT WHERE event_id = ?", (event_id,)).fetchone() is None:
        raise ValueError("所选活动不存在")
    with db:
        if record_id is None:
            db.execute(
                "INSERT INTO BOOTH (event_id, booth_code, location, status) VALUES (?, ?, ?, ?)",
                (event_id, booth_code, location, status),
            )
            return "摊位新增成功"
        cursor = db.execute(
            "UPDATE BOOTH SET event_id = ?, booth_code = ?, location = ?, status = ? "
            "WHERE booth_id = ?",
            (event_id, booth_code, location, status, record_id),
        )
        if cursor.rowcount == 0:
            raise ValueError("要编辑的摊位不存在")
    return "摊位更新成功"


def _save_salesperson(form: Mapping[str, str], record_id: int | None) -> str:
    """新增或更新销售顾问；编辑时保留原入职日期。"""
    emp_code = _required(form, "emp_code", "员工编码")
    name = _required(form, "name", "姓名")
    phone = _clean(form.get("phone"))
    status = _status(form, SALESPERSON_STATUSES, "销售顾问")
    db = get_db()

    with db:
        if record_id is None:
            hired_at = datetime.now(CHINA_TZ).date().isoformat()
            db.execute(
                "INSERT INTO SALESPERSON (emp_code, name, phone, status, hired_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (emp_code, name, phone, status, hired_at),
            )
            return "销售顾问新增成功"
        cursor = db.execute(
            "UPDATE SALESPERSON SET emp_code = ?, name = ?, phone = ?, status = ? "
            "WHERE salesperson_id = ?",
            (emp_code, name, phone, status, record_id),
        )
        if cursor.rowcount == 0:
            raise ValueError("要编辑的销售顾问不存在")
    return "销售顾问更新成功"


def _save_shift(form: Mapping[str, str], record_id: int | None) -> str:
    """新增或更新班次，并校验关联记录和时间先后。"""
    salesperson_id = _parse_reference_id(form, "salesperson_id", "销售顾问")
    booth_id = _parse_reference_id(form, "booth_id", "摊位")
    start_time = _parse_datetime_local(form.get("start_time"), "开始时间")
    end_time = _parse_datetime_local(form.get("end_time"), "结束时间")
    status = _status(form, SHIFT_STATUSES, "班次")
    if end_time <= start_time:
        raise ValueError("结束时间必须晚于开始时间")
    db = get_db()

    if db.execute(
        "SELECT 1 FROM SALESPERSON WHERE salesperson_id = ?",
        (salesperson_id,),
    ).fetchone() is None:
        raise ValueError("所选销售顾问不存在")
    if db.execute("SELECT 1 FROM BOOTH WHERE booth_id = ?", (booth_id,)).fetchone() is None:
        raise ValueError("所选摊位不存在")
    with db:
        if record_id is None:
            db.execute(
                "INSERT INTO SHIFT "
                "(salesperson_id, booth_id, start_time, end_time, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (salesperson_id, booth_id, start_time, end_time, status),
            )
            return "班次新增成功"
        cursor = db.execute(
            "UPDATE SHIFT SET salesperson_id = ?, booth_id = ?, start_time = ?, "
            "end_time = ?, status = ? WHERE shift_id = ?",
            (salesperson_id, booth_id, start_time, end_time, status, record_id),
        )
        if cursor.rowcount == 0:
            raise ValueError("要编辑的班次不存在")
    return "班次更新成功"
