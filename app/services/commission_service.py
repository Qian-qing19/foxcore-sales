"""佣金规则匹配与销售佣金计提服务。"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, ROUND_HALF_UP
import sqlite3
from typing import Any


def match_rule(
    conn: sqlite3.Connection,
    product_id: int,
    salesperson_id: int,
    event_type: str,
    sale_date: str,
) -> sqlite3.Row | None:
    """按生效期、维度和优先级为销售明细匹配一条有效佣金规则。"""
    return conn.execute(
        "SELECT cr.* "
        "FROM COMMISSION_RULE AS cr "
        "WHERE cr.status = '有效' "
        "AND cr.effective_from <= ? "
        "AND (cr.effective_to IS NULL OR cr.effective_to >= ?) "
        "AND ("
        "    cr.dimension = 'GLOBAL' "
        "    OR (cr.dimension = 'PRODUCT' AND EXISTS ("
        "        SELECT 1 FROM PRODUCT_COMMISSION_RULE AS pcr "
        "        WHERE pcr.rule_id = cr.rule_id AND pcr.product_id = ?"
        "    )) "
        "    OR (cr.dimension = 'SALESPERSON' AND EXISTS ("
        "        SELECT 1 FROM SALESPERSON_COMMISSION_RULE AS scr "
        "        WHERE scr.rule_id = cr.rule_id AND scr.salesperson_id = ?"
        "    )) "
        "    OR (cr.dimension = 'EVENT_TYPE' AND cr.event_type_value = ?)"
        ") "
        "ORDER BY cr.priority DESC, cr.version_no DESC, cr.effective_from DESC "
        "LIMIT 1",
        (sale_date, sale_date, product_id, salesperson_id, event_type),
    ).fetchone()


def calc_commission(
    rule: sqlite3.Row | Mapping[str, Any],
    base_cents: int,
    quantity: int,
) -> int:
    """按规则计算明细佣金，比例金额使用 Decimal 并以 HALF_UP 取整。"""
    calc_method = str(rule["calc_method"])
    if calc_method == "按比例":
        rate = rule["rate"]
        if rate is None:
            raise ValueError("比例佣金规则缺少费率")
        amount = (
            Decimal(int(base_cents)) * Decimal(str(rate))
        ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return int(amount)

    if calc_method == "按件固定额":
        fixed_per_unit_cents = rule["fixed_per_unit_cents"]
        if fixed_per_unit_cents is None:
            raise ValueError("按件固定额规则缺少单件金额")
        return int(fixed_per_unit_cents) * int(quantity)

    raise ValueError(f"不支持的佣金计算方式：{calc_method}")


def create_entries_for_sale(conn: sqlite3.Connection, sale_id: int) -> int:
    """为销售明细补生成佣金流水，返回本次新增的 entry 数量。

    已存在的 entry（包括已关联结算单的 entry）只跳过、不更新或删除；最后根据
    全部明细是否都有 entry 重新计算销售状态。作废销售不做处理。
    """
    sale = conn.execute(
        "SELECT s.sale_time, s.status, sh.salesperson_id, e.event_type "
        "FROM SALE AS s "
        "JOIN SHIFT AS sh ON sh.shift_id = s.shift_id "
        "JOIN BOOTH AS b ON b.booth_id = sh.booth_id "
        "JOIN EVENT AS e ON e.event_id = b.event_id "
        "WHERE s.sale_id = ?",
        (sale_id,),
    ).fetchone()
    if sale is None:
        raise ValueError("销售单不存在")
    if sale["status"] == "作废":
        return 0

    items = conn.execute(
        "SELECT line_no, product_id, quantity, line_cents "
        "FROM SALE_ITEM WHERE sale_id = ? ORDER BY line_no",
        (sale_id,),
    ).fetchall()
    existing_lines = {
        int(row["line_no"])
        for row in conn.execute(
            "SELECT line_no FROM COMMISSION_ENTRY WHERE sale_id = ?",
            (sale_id,),
        ).fetchall()
    }
    sale_date = str(sale["sale_time"])[:10]
    created_count = 0

    for item in items:
        line_no = int(item["line_no"])
        if line_no in existing_lines:
            continue
        rule = match_rule(
            conn,
            int(item["product_id"]),
            int(sale["salesperson_id"]),
            str(sale["event_type"]),
            sale_date,
        )
        if rule is None:
            continue

        commission_cents = calc_commission(
            rule,
            int(item["line_cents"]),
            int(item["quantity"]),
        )
        conn.execute(
            "INSERT INTO COMMISSION_ENTRY "
            "(sale_id, line_no, rule_id, settlement_id, base_cents, "
            "commission_cents, status) "
            "VALUES (?, ?, ?, NULL, ?, ?, '待结算')",
            (
                sale_id,
                line_no,
                int(rule["rule_id"]),
                int(item["line_cents"]),
                commission_cents,
            ),
        )
        existing_lines.add(line_no)
        created_count += 1

    item_lines = {int(item["line_no"]) for item in items}
    all_items_have_entries = bool(item_lines) and item_lines.issubset(existing_lines)
    sale_status = "已计佣" if all_items_have_entries else "待计佣"
    conn.execute(
        "UPDATE SALE SET status = ? WHERE sale_id = ?",
        (sale_status, sale_id),
    )
    return created_count