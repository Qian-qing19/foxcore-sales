# -*- coding: utf-8 -*-
"""Update demo PRODUCT and SALESPERSON display data without changing schema."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "foxcore.db"

PRODUCT_UPDATES: tuple[tuple[str, str, str], ...] = (
    ("PRODUCT-001", "帆布托特包", "文创"),
    ("PRODUCT-002", "搪瓷马克杯", "家居"),
    ("PRODUCT-003", "黑胶唱片", "音像"),
    ("PRODUCT-004", "手工香薰蜡烛", "家居"),
    ("PRODUCT-005", "复古金属徽章", "配饰"),
    ("PRODUCT-006", "棉麻围巾", "服饰"),
    ("PRODUCT-007", "音乐节T恤", "服饰"),
    ("PRODUCT-008", "刺绣棒球帽", "服饰"),
    ("PRODUCT-009", "牛皮卡包", "配饰"),
    ("PRODUCT-010", "木质明信片", "文创"),
    ("PRODUCT-011", "手绘冰箱贴", "文创"),
    ("PRODUCT-012", "亚克力钥匙扣", "配饰"),
    ("PRODUCT-013", "帆布胸包", "配饰"),
    ("PRODUCT-014", "玻璃水杯", "家居"),
    ("PRODUCT-015", "绒布玩偶", "文创"),
    ("PRODUCT-016", "拼图挂饰", "文创"),
    ("PRODUCT-017", "手工皂礼盒", "家居"),
    ("PRODUCT-018", "编织手绳", "配饰"),
    ("PRODUCT-019", "金属书签", "文创"),
    ("PRODUCT-020", "复古胶片相机", "数码"),
    ("PRODUCT-021", "黑胶唱机模型", "音像"),
    ("PRODUCT-022", "音乐节手幅", "文创"),
    ("PRODUCT-023", "荧光手环", "配饰"),
    ("PRODUCT-024", "帆布笔袋", "文创"),
    ("PRODUCT-025", "陶瓷杯垫", "家居"),
    ("PRODUCT-026", "香氛喷雾", "家居"),
    ("PRODUCT-027", "帆布收纳袋", "家居"),
    ("PRODUCT-028", "限量海报", "文创"),
)

SALESPERSON_UPDATES: tuple[tuple[str, str], ...] = (
    ("SP-001", "林小满"),
    ("SP-002", "陈一鸣"),
    ("SP-003", "周雨桐"),
    ("SP-004", "赵子昂"),
    ("SP-005", "许知远"),
    ("SP-006", "沈星河"),
    ("SP-007", "唐可欣"),
    ("SP-008", "李沐阳"),
    ("SP-009", "王一诺"),
    ("SP-010", "张予曦"),
    ("SP-011", "刘思远"),
    ("SP-012", "孙嘉禾"),
    ("SP-013", "吴桐"),
    ("SP-014", "郑亦辰"),
    ("SP-015", "冯雨欣"),
    ("SP-016", "何俊熙"),
    ("SP-017", "罗佳宁"),
)


def configure_utf8_output() -> None:
    """Force text output to UTF-8 on supported Python runtimes."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def resolve_database_path() -> Path:
    """Return the configured database path, defaulting to data/foxcore.db."""
    configured = os.environ.get("DATABASE")
    if configured:
        return Path(configured).expanduser().resolve()
    return DEFAULT_DATABASE


def update_products(connection: sqlite3.Connection) -> int:
    """Update mapped product names and categories, returning changed rows."""
    sql = """
        UPDATE PRODUCT
        SET name = :name, category = :category
        WHERE product_code = :product_code
          AND (name <> :name OR category <> :category)
    """
    updated = 0
    for product_code, name, category in PRODUCT_UPDATES:
        cursor = connection.execute(
            sql,
            {
                "product_code": product_code,
                "name": name,
                "category": category,
            },
        )
        updated += max(cursor.rowcount, 0)
    return updated


def update_salespersons(connection: sqlite3.Connection) -> int:
    """Update mapped salesperson names, returning changed rows."""
    sql = """
        UPDATE SALESPERSON
        SET name = :name
        WHERE emp_code = :emp_code
          AND name <> :name
    """
    updated = 0
    for emp_code, name in SALESPERSON_UPDATES:
        cursor = connection.execute(sql, {"emp_code": emp_code, "name": name})
        updated += max(cursor.rowcount, 0)
    return updated


def fetch_product_samples(connection: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Return the first five mapped products in code order."""
    codes = [product_code for product_code, _, _ in PRODUCT_UPDATES]
    placeholders = ", ".join("?" for _ in codes)
    sql = f"""
        SELECT product_code, name, category
        FROM PRODUCT
        WHERE product_code IN ({placeholders})
        ORDER BY product_code
        LIMIT 5
    """
    return connection.execute(sql, codes).fetchall()


def fetch_salesperson_samples(connection: sqlite3.Connection) -> list[tuple[str, str]]:
    """Return the first five mapped salespersons in employee-code order."""
    codes = [emp_code for emp_code, _ in SALESPERSON_UPDATES]
    placeholders = ", ".join("?" for _ in codes)
    sql = f"""
        SELECT emp_code, name
        FROM SALESPERSON
        WHERE emp_code IN ({placeholders})
        ORDER BY emp_code
        LIMIT 5
    """
    return connection.execute(sql, codes).fetchall()


def print_samples(title: str, headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    """Print a compact five-row sample table."""
    print(f"\n{title}（前 5 条样例）")
    print(" | ".join(headers))
    for row in rows:
        print(" | ".join(str(value) for value in row))


def main() -> int:
    """Apply all demo-data updates and print counts and samples."""
    configure_utf8_output()
    database_path = resolve_database_path()

    if not database_path.is_file():
        print(f"数据库不存在：{database_path}", file=sys.stderr)
        return 1

    try:
        with sqlite3.connect(database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            product_updated = update_products(connection)
            salesperson_updated = update_salespersons(connection)
            product_samples = fetch_product_samples(connection)
            salesperson_samples = fetch_salesperson_samples(connection)
    except (OSError, sqlite3.Error) as exc:
        print(f"数据库操作失败：{exc}", file=sys.stderr)
        return 1

    print(f"数据库：{database_path}")
    print(f"PRODUCT 更新行数：{product_updated}")
    print_samples("PRODUCT", ("product_code", "name", "category"), product_samples)
    print(f"\nSALESPERSON 更新行数：{salesperson_updated}")
    print_samples("SALESPERSON", ("emp_code", "name"), salesperson_samples)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
