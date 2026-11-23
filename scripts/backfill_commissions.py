"""为历史销售补算佣金流水。

默认使用应用配置中的数据库路径，可通过 DATABASE 环境变量覆盖。
"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import create_app
from app.db import get_db
from app.services.commission_service import create_entries_for_sale


def backfill_commissions(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """补算无佣金流水的历史销售，返回处理数、新增数及剩余待计佣数。"""
    sales = conn.execute(
        "SELECT sale_id, status FROM SALE AS s "
        "WHERE s.status IN ('已登记', '待计佣') "
        "AND NOT EXISTS ("
        "    SELECT 1 FROM COMMISSION_ENTRY AS ce WHERE ce.sale_id = s.sale_id"
        ") "
        "ORDER BY s.sale_id"
    ).fetchall()

    processed_count = 0
    created_count = 0
    with conn:
        for sale in sales:
            if sale["status"] == "作废":
                continue
            processed_count += 1
            created_count += create_entries_for_sale(conn, int(sale["sale_id"]))

        pending_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM SALE WHERE status = '待计佣'"
            ).fetchone()[0]
        )
    return processed_count, created_count, pending_count


def main() -> int:
    """在应用数据库上执行佣金补算并输出汇总。"""
    try:
        app = create_app()
        with app.app_context():
            processed, created, pending = backfill_commissions(get_db())
    except Exception as exc:
        print(f"佣金补算失败：{exc}", file=sys.stderr)
        return 1

    print(
        f"处理 {processed} 笔，生成 {created} 条佣金，仍待计佣 {pending} 笔"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())