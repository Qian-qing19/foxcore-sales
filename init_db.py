"""Foxcore 数据库初始化入口。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import sys

from scripts.seed import seed_database


PROJECT_ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = PROJECT_ROOT / "db" / "schema.sql"
DB_PATH = PROJECT_ROOT / "data" / "foxcore.db"

TABLE_NAMES = (
    "VENUE",
    "EVENT",
    "BOOTH",
    "SALESPERSON",
    "SHIFT",
    "PRODUCT",
    "IMPORT_BATCH",
    "APP_USER",
    "SALE",
    "SALE_ITEM",
    "COMMISSION_RULE",
    "PRODUCT_COMMISSION_RULE",
    "SALESPERSON_COMMISSION_RULE",
    "COMMISSION_ENTRY",
    "COMMISSION_SETTLEMENT",
    "COMMISSION_SETTLEMENT_LINE",
)


def _connect(db_path: Path) -> sqlite3.Connection:
    """创建连接并显式开启外键约束。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        conn.close()
        raise RuntimeError("SQLite 外键约束未开启")
    return conn


def _remove_database_files(db_path: Path) -> None:
    """删除数据库及可能存在的 WAL/SHM 附属文件。"""
    for candidate in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        candidate.unlink(missing_ok=True)


def _quote_identifier(name: str) -> str:
    """安全引用仅来自内部常量清单的表名。"""
    return '"' + name.replace('"', '""') + '"'


def collect_table_stats(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    """读取 16 张业务表的列数和行数。"""
    actual_tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    expected_tables = set(TABLE_NAMES)
    if actual_tables != expected_tables:
        missing = sorted(expected_tables - actual_tables)
        extra = sorted(actual_tables - expected_tables)
        raise RuntimeError(f"数据表不一致，缺少={missing}，多余={extra}")

    stats: dict[str, tuple[int, int]] = {}
    for table in TABLE_NAMES:
        column_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM pragma_table_info(?)",
                (table,),
            ).fetchone()[0]
        )
        row_count = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {_quote_identifier(table)}"
            ).fetchone()[0]
        )
        stats[table] = (column_count, row_count)
    return stats


def initialize_database(
    db_path: Path = DB_PATH,
    *,
    reset: bool = False,
    include_seed: bool = True,
) -> dict[str, tuple[int, int]]:
    """建库并可选写入种子数据，返回每张表的列数与行数。"""
    db_path = Path(db_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if reset:
        _remove_database_files(db_path)

    conn = _connect(db_path)
    try:
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(schema_sql)
        conn.execute("PRAGMA foreign_keys = ON")

        if include_seed:
            seed_database(conn)

        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_errors:
            raise RuntimeError(f"外键校验失败：{fk_errors}")
        return collect_table_stats(conn)
    finally:
        conn.close()


def _build_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器。"""
    parser = argparse.ArgumentParser(description="初始化 Foxcore SQLite 数据库")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="先删除现有数据库，再重新建库",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="只建表，不写入种子数据",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行命令行初始化并打印表统计。"""
    args = _build_parser().parse_args(argv)
    try:
        stats = initialize_database(
            DB_PATH,
            reset=args.reset,
            include_seed=not args.no_seed,
        )
    except Exception as exc:
        print(f"初始化失败：{exc}", file=sys.stderr)
        return 1

    print(f"数据库路径：{DB_PATH.resolve()}")
    print(f"{'表名':<34}{'列数':>8}{'行数':>10}")
    print("-" * 52)
    for table in TABLE_NAMES:
        columns, rows = stats[table]
        print(f"{table:<34}{columns:>8}{rows:>10}")
    print(f"共 {len(stats)} 张表")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())