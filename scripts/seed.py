"""Foxcore 演示数据库种子数据。

使用固定随机种子 42，生成可重复执行的演示数据。
本阶段只生成销售主数据与销售明细，不生成任何佣金流水。
"""

from __future__ import annotations

from datetime import date, datetime, time as datetime_time, timedelta, timezone
import random
import sqlite3
from typing import Final

from werkzeug.security import generate_password_hash


SEED_RANDOM_SEED: Final[int] = 42
CHINA_TZ: Final[timezone] = timezone(timedelta(hours=8), name="Asia/Shanghai")

EXPECTED_SEED_COUNTS: Final[dict[str, int]] = {
    "VENUE": 3,
    "EVENT": 3,
    "BOOTH": 6,
    "SALESPERSON": 17,
    "SHIFT": 21,
    "PRODUCT": 28,
    "IMPORT_BATCH": 0,
    "APP_USER": 2,
    "SALE": 300,
    "SALE_ITEM": 600,
    "COMMISSION_RULE": 8,
    "PRODUCT_COMMISSION_RULE": 2,
    "SALESPERSON_COMMISSION_RULE": 1,
    "COMMISSION_ENTRY": 0,
    "COMMISSION_SETTLEMENT": 0,
    "COMMISSION_SETTLEMENT_LINE": 0,
}


def _format_datetime(value: datetime) -> str:
    """将带时区时间格式化为数据库约定的本地时间字符串。"""
    return value.astimezone(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """返回种子涉及的全部表的当前行数。"""
    return {
        table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in EXPECTED_SEED_COUNTS
    }


def seed_database(conn: sqlite3.Connection) -> dict[str, int]:
    """幂等写入演示数据，并返回各表行数。

    所有写入使用固定主键配合 ``INSERT OR IGNORE``。在同一个数据库上重复执行
    不会产生额外的场地、班次、销售明细或账号。
    """
    conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite 外键约束未开启")

    rng = random.Random(SEED_RANDOM_SEED)
    now = datetime.now(CHINA_TZ).replace(microsecond=0)
    today = now.date()

    venues = [
        (1, "VENUE-001", "演示场地一", "演示路 1 号", "音乐节主场地", _format_datetime(now)),
        (2, "VENUE-002", "演示场地二", "演示路 2 号", "展销会主场地", _format_datetime(now)),
        (3, "VENUE-003", "演示场地三", "演示路 3 号", "音乐节备用场地", _format_datetime(now)),
    ]

    event_defs = [
        (
            1,
            1,
            "EVENT-2026-001",
            "秋日音乐节",
            "音乐节",
            today - timedelta(days=20),
            today + timedelta(days=2),
            "虚构演示活动",
        ),
        (
            2,
            2,
            "EVENT-2026-002",
            "创意展销会",
            "展销会",
            today - timedelta(days=12),
            today + timedelta(days=4),
            "虚构演示活动",
        ),
        (
            3,
            3,
            "EVENT-2026-003",
            "城市音乐夜",
            "音乐节",
            today - timedelta(days=8),
            today + timedelta(days=8),
            "虚构演示活动",
        ),
    ]
    events = [
        (
            event_id,
            venue_id,
            event_code,
            event_name,
            event_type,
            start_date.isoformat(),
            end_date.isoformat(),
            description,
        )
        for (
            event_id,
            venue_id,
            event_code,
            event_name,
            event_type,
            start_date,
            end_date,
            description,
        ) in event_defs
    ]
    event_start_dates = {item[0]: item[5] for item in event_defs}

    booths = [
        (1, 1, "A-01", "入口左侧", "在用"),
        (2, 1, "A-02", "主舞台前方", "在用"),
        (3, 2, "B-01", "展馆北侧", "在用"),
        (4, 2, "B-02", "展馆南侧", "在用"),
        (5, 3, "C-01", "草坪东侧", "在用"),
        (6, 3, "C-02", "草坪西侧", "在用"),
    ]
    booth_event_ids = {booth[0]: booth[1] for booth in booths}

    salespersons = [
        (
            salesperson_id,
            f"SP-{salesperson_id:03d}",
            f"顾问{salesperson_id:02d}",
            f"000-0000-{salesperson_id:04d}",
            "在职",
            (today - timedelta(days=90 + salesperson_id)).isoformat(),
        )
        for salesperson_id in range(1, 18)
    ]

    products = []
    categories = ("纪念品", "服饰", "文创", "户外用品")
    for product_id in range(1, 29):
        products.append(
            (
                product_id,
                f"PRODUCT-{product_id:03d}",
                f"演示商品{product_id:02d}",
                categories[(product_id - 1) % len(categories)],
                rng.randrange(15, 121) * 100,
                "在售",
                _format_datetime(now - timedelta(days=45 - product_id)),
            )
        )
    product_min_prices = {product[0]: product[4] for product in products}

    users = [
        (
            1,
            "admin",
            generate_password_hash("123456"),
            "合伙人",
            None,
            "启用",
            _format_datetime(now),
        ),
        (
            2,
            "sales01",
            generate_password_hash("123456"),
            "销售顾问",
            1,
            "启用",
            _format_datetime(now),
        ),
    ]

    current_start = now.replace(minute=0, second=0) - timedelta(hours=1)
    current_end = current_start + timedelta(hours=8)
    shift_records: list[dict[str, object]] = []
    for index, booth_id in enumerate((1, 3, 5), start=1):
        shift_records.append(
            {
                "shift_id": index,
                "salesperson_id": index,
                "booth_id": booth_id,
                "start": current_start,
                "end": current_end,
                "status": "进行中",
            }
        )

    for index in range(18):
        shift_id = len(shift_records) + 1
        salesperson_id = (index % 17) + 1
        booth_id = (index % 6) + 1
        event_id = booth_event_ids[booth_id]
        event_start = event_start_dates[event_id]
        shift_date = event_start + timedelta(days=1 + (index % 5))
        start_dt = datetime.combine(
            shift_date,
            datetime_time(9 + (index % 3)),
            CHINA_TZ,
        )
        shift_records.append(
            {
                "shift_id": shift_id,
                "salesperson_id": salesperson_id,
                "booth_id": booth_id,
                "start": start_dt,
                "end": start_dt + timedelta(hours=8),
                "status": "已结束",
            }
        )

    shifts = [
        (
            int(record["shift_id"]),
            int(record["salesperson_id"]),
            int(record["booth_id"]),
            _format_datetime(record["start"]),  # type: ignore[arg-type]
            _format_datetime(record["end"]),  # type: ignore[arg-type]
            str(record["status"]),
        )
        for record in shift_records
    ]
    historical_shifts = shift_records[3:]

    all_sale_ids = list(range(1, 301))
    rng.shuffle(all_sale_ids)
    void_sale_ids = set(all_sale_ids[:30])
    pending_sale_ids = set(all_sale_ids[30:45])

    sale_rows: list[tuple[object, ...]] = []
    sale_item_rows: list[tuple[object, ...]] = []
    for sale_id in range(1, 301):
        shift = rng.choice(historical_shifts)
        sale_datetime = shift["start"] + timedelta(minutes=rng.randint(30, 420))  # type: ignore[operator]
        product_ids = rng.sample(list(product_min_prices), 2)

        total_cents = 0
        for line_no, product_id in enumerate(product_ids, start=1):
            quantity = rng.randint(1, 3)
            final_unit_price_cents = product_min_prices[product_id] + rng.choice(
                (0, 50, 100, 200, 500, 1000)
            )
            line_cents = quantity * final_unit_price_cents
            total_cents += line_cents
            sale_item_rows.append(
                (
                    sale_id,
                    line_no,
                    product_id,
                    quantity,
                    final_unit_price_cents,
                    line_cents,
                )
            )

        if sale_id in void_sale_ids:
            status = "作废"
            void_reason = "演示作废：顾客取消"
            void_by = 1
            void_at = _format_datetime(sale_datetime + timedelta(minutes=20))
        elif sale_id in pending_sale_ids:
            status = "待计佣"
            void_reason = None
            void_by = None
            void_at = None
        else:
            status = "已登记"
            void_reason = None
            void_by = None
            void_at = None

        sale_rows.append(
            (
                sale_id,
                int(shift["shift_id"]),
                None,
                _format_datetime(sale_datetime),
                f"seed-sale-{sale_id:04d}",
                total_cents,
                status,
                void_reason,
                void_by,
                void_at,
            )
        )

    effective_from = (today - timedelta(days=365)).isoformat()
    rule_rows = [
        (
            1,
            "R-GLOBAL-RATE",
            1,
            "GLOBAL",
            None,
            "按比例",
            0.05,
            None,
            10,
            effective_from,
            None,
            "停用",
            1,
            _format_datetime(now),
        ),
        (
            2,
            "R-GLOBAL-RATE",
            2,
            "GLOBAL",
            None,
            "按比例",
            0.06,
            None,
            20,
            effective_from,
            None,
            "有效",
            1,
            _format_datetime(now),
        ),
        (
            3,
            "R-MUSIC-RATE",
            1,
            "EVENT_TYPE",
            "音乐节",
            "按比例",
            0.07,
            None,
            30,
            effective_from,
            None,
            "有效",
            1,
            _format_datetime(now),
        ),
        (
            4,
            "R-EXPO-FIXED",
            1,
            "EVENT_TYPE",
            "展销会",
            "按件固定额",
            None,
            500,
            30,
            effective_from,
            None,
            "有效",
            1,
            _format_datetime(now),
        ),
        (
            5,
            "R-PRODUCT-001",
            1,
            "PRODUCT",
            None,
            "按比例",
            0.08,
            None,
            40,
            effective_from,
            None,
            "有效",
            1,
            _format_datetime(now),
        ),
        (
            6,
            "R-PRODUCT-002",
            1,
            "PRODUCT",
            None,
            "按件固定额",
            None,
            300,
            40,
            effective_from,
            None,
            "有效",
            1,
            _format_datetime(now),
        ),
        (
            7,
            "R-SALESPERSON-TOP",
            1,
            "SALESPERSON",
            None,
            "按比例",
            0.09,
            None,
            50,
            effective_from,
            None,
            "有效",
            1,
            _format_datetime(now),
        ),
        (
            8,
            "R-LEGACY",
            1,
            "GLOBAL",
            None,
            "按比例",
            0.04,
            None,
            0,
            effective_from,
            None,
            "停用",
            1,
            _format_datetime(now),
        ),
    ]

    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO VENUE "
            "(venue_id, venue_code, name, address, description, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            venues,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO EVENT "
            "(event_id, venue_id, event_code, event_name, event_type, start_date, end_date, description) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            events,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO BOOTH "
            "(booth_id, event_id, booth_code, location, status) VALUES (?, ?, ?, ?, ?)",
            booths,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO SALESPERSON "
            "(salesperson_id, emp_code, name, phone, status, hired_at) VALUES (?, ?, ?, ?, ?, ?)",
            salespersons,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO SHIFT "
            "(shift_id, salesperson_id, booth_id, start_time, end_time, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            shifts,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO PRODUCT "
            "(product_id, product_code, name, category, min_price_cents, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            products,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO APP_USER "
            "(user_id, username, password_hash, role, salesperson_id, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            users,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO SALE "
            "(sale_id, shift_id, import_batch_id, sale_time, client_txn_id, total_cents, "
            "status, void_reason, void_by, void_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            sale_rows,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO SALE_ITEM "
            "(sale_id, line_no, product_id, quantity, final_unit_price_cents, line_cents) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            sale_item_rows,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO COMMISSION_RULE "
            "(rule_id, rule_code, version_no, dimension, event_type_value, calc_method, rate, "
            "fixed_per_unit_cents, priority, effective_from, effective_to, status, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rule_rows,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO PRODUCT_COMMISSION_RULE (product_id, rule_id) VALUES (?, ?)",
            [(1, 5), (2, 6)],
        )
        conn.executemany(
            "INSERT OR IGNORE INTO SALESPERSON_COMMISSION_RULE "
            "(salesperson_id, rule_id) VALUES (?, ?)",
            [(1, 7)],
        )

    counts = _table_counts(conn)
    if counts != EXPECTED_SEED_COUNTS:
        raise RuntimeError(f"种子数据行数不符合预期：{counts}")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("种子数据存在外键约束错误")
    return counts