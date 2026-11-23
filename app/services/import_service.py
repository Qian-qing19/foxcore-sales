"""历史销售 CSV 导入服务。"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import io
import re
import sqlite3
from typing import Any, Final

from app.db import get_db
from app.services.commission_service import create_entries_for_sale


CHINA_TZ: Final[timezone] = timezone(timedelta(hours=8), name="Asia/Shanghai")
IMPORT_HEADERS: Final[tuple[str, ...]] = (
    "client_txn_id",
    "sale_time",
    "shift_id",
    "product_code",
    "quantity",
    "final_unit_price",
)
MAX_IMPORT_BYTES: Final[int] = 2 * 1024 * 1024
MAX_VISIBLE_ERRORS: Final[int] = 50
SALE_TIME_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"
QUANTITY_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9]+$")
PRICE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9]+(?:\.[0-9]{1,2})?$")
SKIPPED_PATTERN: Final[re.Pattern[str]] = re.compile(r"跳过\s*(\d+)\s*行")
SQLITE_MAX_INTEGER: Final[int] = 2**63 - 1


@dataclass(frozen=True)
class ImportRowError:
    """一张导入错误明细。"""

    row_number: int
    reason: str


@dataclass(frozen=True)
class ImportResult:
    """一次导入的汇总结果。"""

    batch_id: int
    file_name: str
    row_count: int
    success_count: int
    skipped_count: int
    error_count: int
    status: str
    note: str
    errors: tuple[ImportRowError, ...]
    created_sale_ids: tuple[int, ...]


def _clean(value: Any) -> str:
    """把输入值规范化为去除首尾空白的字符串。"""
    return str(value or "").strip()


def _now_text() -> str:
    """返回 Asia/Shanghai 当前时间。"""
    return datetime.now(CHINA_TZ).replace(microsecond=0).strftime(SALE_TIME_FORMAT)


def _parse_skipped_count(note: Any) -> int:
    """从批次备注中读取跳过行数，旧批次没有备注时返回 0。"""
    match = SKIPPED_PATTERN.search(_clean(note))
    return int(match.group(1)) if match else 0


def _parse_positive_int(raw: str, label: str) -> int:
    """解析正整数并返回整数。"""
    text = _clean(raw)
    if not QUANTITY_PATTERN.fullmatch(text) or int(text) <= 0:
        raise ValueError(f"{label}必须为正整数")
    return int(text)


def _parse_sale_time(raw: str) -> str:
    """校验并返回规范化的销售时间。"""
    text = _clean(raw)
    try:
        parsed = datetime.strptime(text, SALE_TIME_FORMAT)
    except ValueError:
        raise ValueError("sale_time 格式应为 YYYY-MM-DD HH:MM:SS") from None
    if parsed.strftime(SALE_TIME_FORMAT) != text:
        raise ValueError("sale_time 格式应为 YYYY-MM-DD HH:MM:SS")
    return text


def _parse_price_cents(raw: str) -> int:
    """把最多两位小数的元金额精确转换为分。"""
    text = _clean(raw)
    if not PRICE_PATTERN.fullmatch(text):
        raise ValueError("final_unit_price 必须为最多两位小数的非负金额")
    try:
        cents = (Decimal(text) * Decimal("100")).to_integral_value()
    except InvalidOperation:
        raise ValueError("final_unit_price 必须为最多两位小数的非负金额") from None
    if cents > SQLITE_MAX_INTEGER:
        raise ValueError("final_unit_price 超出系统支持范围")
    return int(cents)


def _read_csv_rows(content: bytes) -> tuple[list[tuple[int, list[str]]], int]:
    """解码 CSV、校验表头并返回非空数据行与原始行数。"""
    if not content:
        raise ValueError("CSV 文件为空")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("CSV 文件解码失败，请使用 UTF-8 或 Excel 导出的 UTF-8 CSV") from None
    if not text.strip():
        raise ValueError("CSV 文件为空")

    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        raw_header = next(reader)
    except StopIteration:
        raise ValueError("CSV 文件为空") from None
    except csv.Error as exc:
        raise ValueError(f"CSV 格式错误：{exc}") from None

    header = tuple(_clean(cell) for cell in raw_header)
    if header != IMPORT_HEADERS:
        expected = ",".join(IMPORT_HEADERS)
        raise ValueError(f"CSV 表头必须为：{expected}")

    rows: list[tuple[int, list[str]]] = []
    total_rows = 0
    try:
        for row_number, row in enumerate(reader, start=2):
            if not any(_clean(cell) for cell in row):
                continue
            total_rows += 1
            rows.append((row_number, row))
    except csv.Error as exc:
        raise ValueError(f"CSV 格式错误：{exc}") from None

    if total_rows == 0:
        raise ValueError("CSV 文件没有数据行")
    return rows, total_rows


def build_import_template() -> bytes:
    """生成带 UTF-8 BOM、标准表头和两行示例的导入模板。"""
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(IMPORT_HEADERS)
    writer.writerow(
        ["IMPORT-DEMO-001", "2026-11-15 10:00:00", "1", "PRODUCT-001", "2", "99.00"]
    )
    writer.writerow(
        ["IMPORT-DEMO-001", "2026-11-15 10:00:00", "1", "PRODUCT-002", "1", "99.00"]
    )
    return output.getvalue().encode("utf-8-sig")


def get_recent_import_batches(limit: int = 5) -> list[dict[str, Any]]:
    """返回最近导入批次，并将备注中的跳过行数转换为页面字段。"""
    safe_limit = max(1, min(int(limit), 5))
    rows = get_db().execute(
        "SELECT import_batch_id, file_name, imported_at, row_count, "
        "success_count, error_count, status, note "
        "FROM IMPORT_BATCH "
        "ORDER BY imported_at DESC, import_batch_id DESC "
        "LIMIT ?",
        (safe_limit,),
    ).fetchall()
    batches: list[dict[str, Any]] = []
    for row in rows:
        skipped_count = _parse_skipped_count(row["note"])
        success_count = max(int(row["success_count"]) - skipped_count, 0)
        batches.append(
            {
                **dict(row),
                "success_count": success_count,
                "skipped_count": skipped_count,
                "error_count": int(row["error_count"]),
                "row_count": int(row["row_count"]),
            }
        )
    return batches


def get_import_page_context() -> dict[str, Any]:
    """构造导入页所需的最近批次上下文。"""
    return {
        "recent_batches": get_recent_import_batches(5),
        "max_visible_errors": MAX_VISIBLE_ERRORS,
    }


def _fetch_reference_data(
    db: sqlite3.Connection,
    shift_ids: set[int],
    product_codes: set[str],
) -> tuple[dict[int, sqlite3.Row], dict[str, sqlite3.Row]]:
    """批量读取班次和产品，避免逐行查询。"""
    shifts: dict[int, sqlite3.Row] = {}
    products: dict[str, sqlite3.Row] = {}
    for shift_id in shift_ids:
        row = db.execute(
            "SELECT shift_id FROM SHIFT WHERE shift_id = ?",
            (shift_id,),
        ).fetchone()
        if row is not None:
            shifts[shift_id] = row
    for product_code in product_codes:
        row = db.execute(
            "SELECT product_id, product_code, min_price_cents "
            "FROM PRODUCT WHERE product_code = ?",
            (product_code,),
        ).fetchone()
        if row is not None:
            products[product_code] = row
    return shifts, products


def import_sales_csv(
    file_name: str,
    content: bytes,
    imported_by: int,
) -> ImportResult:
    """导入历史销售 CSV；行级错误保留，批次写入使用单个事务。"""
    clean_file_name = _clean(file_name)
    if not clean_file_name.lower().endswith(".csv"):
        raise ValueError("仅支持 .csv 文件")
    if imported_by <= 0:
        raise ValueError("导入用户无效")
    if len(content) > MAX_IMPORT_BYTES:
        raise ValueError("CSV 文件不能超过 2MB")

    raw_rows, row_count = _read_csv_rows(content)
    prepared_rows: list[dict[str, Any]] = []
    errors: list[ImportRowError] = []
    skipped_count = 0
    seen_existing_ids: set[str] = set()
    db = get_db()

    for row_number, raw_row in raw_rows:
        if len(raw_row) != len(IMPORT_HEADERS):
            errors.append(
                ImportRowError(
                    row_number=row_number,
                    reason=f"列数应为 {len(IMPORT_HEADERS)} 列",
                )
            )
            continue
        row = dict(zip(IMPORT_HEADERS, raw_row, strict=True))
        client_txn_id = _clean(row["client_txn_id"])
        if not client_txn_id:
            errors.append(ImportRowError(row_number, "client_txn_id 不能为空"))
            continue

        if client_txn_id in seen_existing_ids:
            skipped_count += 1
            continue
        existing = db.execute(
            "SELECT 1 FROM SALE WHERE client_txn_id = ?",
            (client_txn_id,),
        ).fetchone()
        if existing is not None:
            seen_existing_ids.add(client_txn_id)
            skipped_count += 1
            continue

        try:
            sale_time = _parse_sale_time(row["sale_time"])
            shift_id = _parse_positive_int(row["shift_id"], "shift_id")
            product_code = _clean(row["product_code"])
            if not product_code:
                raise ValueError("product_code 不能为空")
            quantity = _parse_positive_int(row["quantity"], "quantity")
            final_unit_price_cents = _parse_price_cents(row["final_unit_price"])
        except ValueError as exc:
            errors.append(ImportRowError(row_number, str(exc)))
            continue

        prepared_rows.append(
            {
                "row_number": row_number,
                "client_txn_id": client_txn_id,
                "sale_time": sale_time,
                "shift_id": shift_id,
                "product_code": product_code,
                "quantity": quantity,
                "final_unit_price_cents": final_unit_price_cents,
            }
        )

    shifts, products = _fetch_reference_data(
        db,
        {int(row["shift_id"]) for row in prepared_rows},
        {str(row["product_code"]) for row in prepared_rows},
    )
    valid_rows: list[dict[str, Any]] = []
    for row in prepared_rows:
        if int(row["shift_id"]) not in shifts:
            errors.append(ImportRowError(int(row["row_number"]), "班次不存在"))
            continue
        product = products.get(str(row["product_code"]))
        if product is None:
            errors.append(ImportRowError(int(row["row_number"]), "产品编码不存在"))
            continue
        price_cents = int(row["final_unit_price_cents"])
        if price_cents < int(product["min_price_cents"]):
            errors.append(
                ImportRowError(
                    int(row["row_number"]),
                    "单价低于最低售价，需合伙人批准",
                )
            )
            continue
        quantity = int(row["quantity"])
        line_cents = quantity * price_cents
        if line_cents > SQLITE_MAX_INTEGER:
            errors.append(
                ImportRowError(int(row["row_number"]), "销售金额超出系统支持范围")
            )
            continue
        valid_rows.append(
            {
                **row,
                "product_id": int(product["product_id"]),
                "line_cents": line_cents,
            }
        )

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in valid_rows:
        grouped.setdefault(str(row["client_txn_id"]), []).append(row)

    ready_groups: list[list[dict[str, Any]]] = []
    for rows in grouped.values():
        identity = {(str(row["sale_time"]), int(row["shift_id"])) for row in rows}
        if len(identity) != 1:
            for row in rows:
                errors.append(
                    ImportRowError(
                        int(row["row_number"]),
                        "同一 client_txn_id 的 sale_time 和 shift_id 必须一致",
                    )
                )
            continue
        if sum(int(row["line_cents"]) for row in rows) > SQLITE_MAX_INTEGER:
            for row in rows:
                errors.append(
                    ImportRowError(int(row["row_number"]), "销售合计金额超出系统支持范围")
                )
            continue
        ready_groups.append(rows)

    imported_count = 0
    created_sale_ids: list[int] = []
    note = f"跳过 {skipped_count} 行"
    imported_at = _now_text()

    try:
        with db:
            batch_cursor = db.execute(
                "INSERT INTO IMPORT_BATCH "
                "(file_name, imported_by, imported_at, row_count, success_count, "
                "error_count, status, note) "
                "VALUES (?, ?, ?, 0, 0, 0, '失败', ?)",
                (clean_file_name, imported_by, imported_at, note),
            )
            batch_id = int(batch_cursor.lastrowid)

            for rows in ready_groups:
                first = rows[0]
                total_cents = sum(int(row["line_cents"]) for row in rows)
                sale_cursor = db.execute(
                    "INSERT INTO SALE "
                    "(shift_id, import_batch_id, sale_time, client_txn_id, "
                    "total_cents, status) "
                    "VALUES (?, ?, ?, ?, ?, '待计佣')",
                    (
                        int(first["shift_id"]),
                        batch_id,
                        str(first["sale_time"]),
                        str(first["client_txn_id"]),
                        total_cents,
                    ),
                )
                sale_id = int(sale_cursor.lastrowid)
                db.executemany(
                    "INSERT INTO SALE_ITEM "
                    "(sale_id, line_no, product_id, quantity, "
                    "final_unit_price_cents, line_cents) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            sale_id,
                            line_no,
                            int(row["product_id"]),
                            int(row["quantity"]),
                            int(row["final_unit_price_cents"]),
                            int(row["line_cents"]),
                        )
                        for line_no, row in enumerate(rows, start=1)
                    ],
                )
                create_entries_for_sale(db, sale_id)
                imported_count += len(rows)
                created_sale_ids.append(sale_id)

            error_count = len(errors)
            database_success_count = imported_count + skipped_count
            if database_success_count + error_count != row_count:
                raise ValueError("导入计数不一致，数据已整体回滚")
            if error_count == 0:
                status = "成功"
            elif database_success_count > 0:
                status = "部分成功"
            else:
                status = "失败"
            db.execute(
                "UPDATE IMPORT_BATCH "
                "SET row_count = ?, success_count = ?, error_count = ?, "
                "status = ?, note = ? "
                "WHERE import_batch_id = ?",
                (
                    row_count,
                    database_success_count,
                    error_count,
                    status,
                    note,
                    batch_id,
                ),
            )
    except Exception:
        raise ValueError("导入失败，数据已整体回滚，请检查文件内容后重试") from None

    errors.sort(key=lambda item: item.row_number)
    return ImportResult(
        batch_id=batch_id,
        file_name=clean_file_name,
        row_count=row_count,
        success_count=imported_count,
        skipped_count=skipped_count,
        error_count=len(errors),
        status=status,
        note=note,
        errors=tuple(errors),
        created_sale_ids=tuple(created_sale_ids),
    )

