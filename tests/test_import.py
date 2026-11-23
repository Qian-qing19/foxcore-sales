"""历史销售 CSV 导入页面、模板与服务测试。"""

from __future__ import annotations

import codecs
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
from pathlib import Path
import sqlite3
from uuid import uuid4

from flask import Flask
from flask.testing import FlaskClient
import pytest

from app import create_app
from app.services.import_service import IMPORT_HEADERS, MAX_IMPORT_BYTES
from app.services import import_service
from init_db import initialize_database


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


@pytest.fixture()
def app(tmp_path: Path) -> Flask:
    """为每个导入测试创建独立临时数据库和应用实例。"""
    db_path = tmp_path / "foxcore-import.db"
    initialize_database(db_path)
    return create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret-key",
            "DATABASE": str(db_path),
        }
    )


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    """提供 Flask 测试客户端。"""
    return app.test_client()


def _connect(app: Flask) -> sqlite3.Connection:
    """连接测试数据库并启用行工厂与外键约束。"""
    conn = sqlite3.connect(app.config["DATABASE"])
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _login(client: FlaskClient, username: str, password: str = "123456"):
    """登录指定测试账号。"""
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def _shift_id(app: Flask) -> int:
    """返回种子数据中的第一个有效班次编号。"""
    conn = _connect(app)
    try:
        row = conn.execute("SELECT shift_id FROM SHIFT ORDER BY shift_id LIMIT 1").fetchone()
    finally:
        conn.close()
    assert row is not None
    return int(row["shift_id"])


def _product(app: Flask, product_code: str) -> sqlite3.Row:
    """按产品编码读取产品和最低价。"""
    conn = _connect(app)
    try:
        row = conn.execute(
            "SELECT product_id, product_code, min_price_cents FROM PRODUCT "
            "WHERE product_code = ?",
            (product_code,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row


def _money(cents: int) -> str:
    """把分格式化为 CSV 使用的两位小数元金额。"""
    return f"{Decimal(cents) / Decimal('100'):.2f}"


def _sale_time() -> str:
    """返回当前上海时区、符合导入格式的销售时间。"""
    return datetime.now(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _csv_bytes(rows: list[list[str]], *, encoding: str = "utf-8-sig") -> bytes:
    """根据数据行构造 CSV 字节。"""
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(IMPORT_HEADERS)
    writer.writerows(rows)
    return output.getvalue().encode(encoding)


def _upload(
    client: FlaskClient,
    content: bytes,
    *,
    filename: str = "history-sales.csv",
    follow_redirects: bool = True,
):
    """提交 CSV 上传表单。"""
    return client.post(
        "/import",
        data={"csv_file": (io.BytesIO(content), filename)},
        content_type="multipart/form-data",
        follow_redirects=follow_redirects,
    )


def _sale_count(app: Flask) -> int:
    """返回销售主表总行数。"""
    conn = _connect(app)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM SALE").fetchone()[0])
    finally:
        conn.close()


def _latest_batch(app: Flask) -> sqlite3.Row:
    """返回最新导入批次。"""
    conn = _connect(app)
    try:
        row = conn.execute(
            "SELECT * FROM IMPORT_BATCH ORDER BY import_batch_id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row


def _same_sale_rows(app: Flask, client_txn_id: str) -> list[list[str]]:
    """构造同一 client_txn_id 的两行合法销售数据。"""
    shift_id = str(_shift_id(app))
    sale_time = _sale_time()
    product_1 = _product(app, "PRODUCT-001")
    product_2 = _product(app, "PRODUCT-002")
    return [
        [
            client_txn_id,
            sale_time,
            shift_id,
            "PRODUCT-001",
            "2",
            _money(int(product_1["min_price_cents"]) + 100),
        ],
        [
            client_txn_id,
            sale_time,
            shift_id,
            "PRODUCT-002",
            "1",
            _money(int(product_2["min_price_cents"]) + 100),
        ],
    ]


def test_salesperson_cannot_access_import(client: FlaskClient) -> None:
    """销售顾问访问 /import 返回 403。"""
    _login(client, "sales01")
    response = client.get("/import")
    assert response.status_code == 403


def test_admin_import_page_and_template(client: FlaskClient) -> None:
    """合伙人可打开导入页，模板带 BOM、正确表头和两行示例。"""
    _login(client, "admin")
    page = client.get("/import")
    assert page.status_code == 200
    assert "最近 5 个导入批次" in page.get_data(as_text=True)

    response = client.get("/import/template")
    assert response.status_code == 200
    assert response.data.startswith(codecs.BOM_UTF8)
    rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))
    assert tuple(rows[0]) == IMPORT_HEADERS
    assert len(rows) == 3


def test_same_client_txn_rows_merge_and_create_commissions(
    app: Flask,
    client: FlaskClient,
) -> None:
    """同一交易两行应合并为一单、两条明细和两条佣金流水。"""
    _login(client, "admin")
    client_txn_id = f"import-{uuid4()}"
    rows = _same_sale_rows(app, client_txn_id)
    response = _upload(client, _csv_bytes(rows))

    assert response.status_code == 200
    conn = _connect(app)
    try:
        sale = conn.execute(
            "SELECT sale_id, total_cents, status FROM SALE WHERE client_txn_id = ?",
            (client_txn_id,),
        ).fetchone()
        assert sale is not None
        sale_id = int(sale["sale_id"])
        items = conn.execute(
            "SELECT COUNT(*) FROM SALE_ITEM WHERE sale_id = ?",
            (sale_id,),
        ).fetchone()[0]
        entries = conn.execute(
            "SELECT COUNT(*) FROM COMMISSION_ENTRY WHERE sale_id = ?",
            (sale_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert int(sale["total_cents"]) > 0
    assert sale["status"] == "已计佣"
    assert int(items) == 2
    assert int(entries) == 2
    batch = _latest_batch(app)
    assert tuple(batch[column] for column in ("row_count", "success_count", "error_count", "status")) == (
        2,
        2,
        0,
        "成功",
    )


def test_duplicate_client_txn_is_skipped_without_new_rows(
    app: Flask,
    client: FlaskClient,
) -> None:
    """重复 client_txn_id 应整单跳过，不新增销售、明细或佣金。"""
    _login(client, "admin")
    client_txn_id = f"import-{uuid4()}"
    rows = _same_sale_rows(app, client_txn_id)
    first = _upload(client, _csv_bytes(rows))
    assert first.status_code == 200

    conn = _connect(app)
    try:
        sale = conn.execute(
            "SELECT sale_id FROM SALE WHERE client_txn_id = ?",
            (client_txn_id,),
        ).fetchone()
        assert sale is not None
        sale_id = int(sale["sale_id"])
        before_sales = int(conn.execute("SELECT COUNT(*) FROM SALE").fetchone()[0])
        before_items = int(conn.execute("SELECT COUNT(*) FROM SALE_ITEM").fetchone()[0])
        before_entries = int(
            conn.execute("SELECT COUNT(*) FROM COMMISSION_ENTRY").fetchone()[0]
        )
    finally:
        conn.close()

    second = _upload(client, _csv_bytes(rows))
    assert second.status_code == 200
    assert "跳过" in second.get_data(as_text=True)

    conn = _connect(app)
    try:
        after_sales = int(conn.execute("SELECT COUNT(*) FROM SALE").fetchone()[0])
        after_items = int(conn.execute("SELECT COUNT(*) FROM SALE_ITEM").fetchone()[0])
        after_entries = int(
            conn.execute("SELECT COUNT(*) FROM COMMISSION_ENTRY").fetchone()[0]
        )
        linked_items = int(
            conn.execute(
                "SELECT COUNT(*) FROM SALE_ITEM WHERE sale_id = ?",
                (sale_id,),
            ).fetchone()[0]
        )
    finally:
        conn.close()

    assert (after_sales, after_items, after_entries) == (
        before_sales,
        before_items,
        before_entries,
    )
    assert linked_items == 2
    batch = _latest_batch(app)
    assert int(batch["row_count"]) == 2
    assert int(batch["success_count"]) == 2
    assert int(batch["error_count"]) == 0
    assert batch["status"] == "成功"
    assert "跳过 2 行" in batch["note"]


def test_missing_product_marks_row_failed_without_sale(
    app: Flask,
    client: FlaskClient,
) -> None:
    """不存在的产品编码只记录该行错误，不写销售。"""
    _login(client, "admin")
    before_sales = _sale_count(app)
    client_txn_id = f"missing-product-{uuid4()}"
    rows = [
        [
            client_txn_id,
            _sale_time(),
            str(_shift_id(app)),
            "NO-SUCH-PRODUCT",
            "1",
            "99.00",
        ]
    ]

    response = _upload(client, _csv_bytes(rows))
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "产品编码不存在" in body
    assert "本次导入错误明细" in body
    assert "第 2 行" in body
    assert _sale_count(app) == before_sales
    batch = _latest_batch(app)
    assert (int(batch["row_count"]), int(batch["success_count"]), int(batch["error_count"])) == (
        1,
        0,
        1,
    )
    assert batch["status"] == "失败"


def test_price_below_minimum_is_rejected(app: Flask, client: FlaskClient) -> None:
    """低于产品最低售价的行失败，不写销售。"""
    _login(client, "admin")
    product = _product(app, "PRODUCT-001")
    before_sales = _sale_count(app)
    rows = [
        [
            f"low-price-{uuid4()}",
            _sale_time(),
            str(_shift_id(app)),
            "PRODUCT-001",
            "1",
            _money(max(int(product["min_price_cents"]) - 1, 0)),
        ]
    ]

    response = _upload(client, _csv_bytes(rows))
    assert response.status_code == 200
    assert "单价低于最低售价" in response.get_data(as_text=True)
    assert _sale_count(app) == before_sales


def test_zero_quantity_is_rejected(app: Flask, client: FlaskClient) -> None:
    """quantity=0 应失败且不写销售。"""
    _login(client, "admin")
    product = _product(app, "PRODUCT-001")
    before_sales = _sale_count(app)
    rows = [
        [
            f"zero-quantity-{uuid4()}",
            _sale_time(),
            str(_shift_id(app)),
            "PRODUCT-001",
            "0",
            _money(int(product["min_price_cents"]) + 100),
        ]
    ]

    response = _upload(client, _csv_bytes(rows))
    assert response.status_code == 200
    assert "quantity必须为正整数" in response.get_data(as_text=True)
    assert _sale_count(app) == before_sales


@pytest.mark.parametrize(
    ("filename", "content", "expected_message"),
    [
        ("history.txt", _csv_bytes([["x", _sale_time(), "1", "PRODUCT-001", "1", "99.00"]]), "仅支持 .csv 文件"),
        ("empty.csv", b"", "CSV 文件为空"),
        ("header-only.csv", _csv_bytes([]), "CSV 文件没有数据行"),
    ],
)
def test_non_csv_and_empty_uploads_show_chinese_error(
    app: Flask,
    client: FlaskClient,
    filename: str,
    content: bytes,
    expected_message: str,
) -> None:
    """非 CSV、空文件和仅表头文件均返回中文提示而不是 500。"""
    _login(client, "admin")
    response = _upload(client, content, filename=filename)
    assert response.status_code == 200
    assert expected_message in response.get_data(as_text=True)
    conn = _connect(app)
    try:
        assert int(conn.execute("SELECT COUNT(*) FROM IMPORT_BATCH").fetchone()[0]) == 0
    finally:
        conn.close()


def test_oversized_file_is_rejected(app: Flask, client: FlaskClient) -> None:
    """超过 2MB 的文件在解析前返回中文提示。"""
    _login(client, "admin")
    response = _upload(client, b"x" * (MAX_IMPORT_BYTES + 1))
    assert response.status_code == 200
    assert "不能超过 2MB" in response.get_data(as_text=True)


def test_import_batch_counts_and_partial_status(
    app: Flask,
    client: FlaskClient,
) -> None:
    """混合成功和失败行应写部分成功批次，计数与页面结果一致。"""
    _login(client, "admin")
    product = _product(app, "PRODUCT-001")
    shift_id = str(_shift_id(app))
    sale_time = _sale_time()
    rows = [
        [
            f"partial-ok-{uuid4()}",
            sale_time,
            shift_id,
            "PRODUCT-001",
            "1",
            _money(int(product["min_price_cents"]) + 100),
        ],
        [
            f"partial-bad-{uuid4()}",
            sale_time,
            shift_id,
            "NO-SUCH-PRODUCT",
            "1",
            "99.00",
        ],
    ]

    response = _upload(client, _csv_bytes(rows))
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'data-result-row-count="2"' in body
    assert 'data-result-success-count="1"' in body
    assert 'data-result-error-count="1"' in body
    assert "部分成功" in body

    batch = _latest_batch(app)
    assert int(batch["row_count"]) == 2
    assert int(batch["success_count"]) == 1
    assert int(batch["error_count"]) == 1
    assert batch["status"] == "部分成功"
    assert batch["note"] == "跳过 0 行"

def test_unexpected_failure_rolls_back_entire_import(
    app: Flask,
    client: FlaskClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """计佣等内部步骤异常时，批次、销售、明细和佣金全部回滚。"""
    _login(client, "admin")
    rows = _same_sale_rows(app, f"rollback-{uuid4()}")
    conn = _connect(app)
    try:
        before = (
            int(conn.execute("SELECT COUNT(*) FROM IMPORT_BATCH").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM SALE").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM SALE_ITEM").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM COMMISSION_ENTRY").fetchone()[0]),
        )
    finally:
        conn.close()

    def fail_commission(_conn: sqlite3.Connection, _sale_id: int) -> int:
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(import_service, "create_entries_for_sale", fail_commission)
    response = _upload(client, _csv_bytes(rows))
    assert response.status_code == 200
    assert "数据已整体回滚" in response.get_data(as_text=True)

    conn = _connect(app)
    try:
        after = (
            int(conn.execute("SELECT COUNT(*) FROM IMPORT_BATCH").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM SALE").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM SALE_ITEM").fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM COMMISSION_ENTRY").fetchone()[0]),
        )
    finally:
        conn.close()
    assert after == before

