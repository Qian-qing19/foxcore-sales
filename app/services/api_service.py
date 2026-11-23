"""微信小程序 JSON API 服务。

所有金额入参与返回值均使用整数分。销售写入通过适配器复用现有
``sales_service.create_sale``，从而保留最低价、可售条件、幂等和佣金事务。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re
import sqlite3
from typing import Any, Final

from flask import current_app
from itsdangerous import BadData, URLSafeTimedSerializer

from app.auth import authenticate_user
from app.db import get_db
from app.services import sales_service


CHINA_TZ: Final[timezone] = timezone(
    timedelta(hours=8),
    name="Asia/Shanghai",
)
TOKEN_SALT: Final[str] = "foxcore-api"
TOKEN_MAX_AGE_SECONDS: Final[int] = 12 * 60 * 60
POSITIVE_INTEGER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9]+$")


class ApiValidationError(ValueError):
    """表示 API 请求参数不合法。"""


class ApiAuthenticationError(Exception):
    """表示 API 登录凭据无效。"""


class ApiSaleForm(Mapping[str, Any]):
    """把 JSON 销售明细适配为销售服务所需的表单映射。

    ``sales_service`` 使用 ``getlist`` 读取三组并按行对齐的字段。本适配器
    保持该语义，同时把 API 的整数分单价转换成服务层当前的元字符串输入。
    """

    def __init__(
        self,
        *,
        client_txn_id: str,
        shift_id: int,
        product_ids: list[str],
        quantities: list[str],
        final_unit_prices: list[str],
    ) -> None:
        """保存标量字段和按下标对齐的明细列表。"""
        if not (
            len(product_ids) == len(quantities) == len(final_unit_prices)
        ):
            raise ApiValidationError("销售明细字段长度不一致")

        self._values = {
            "client_txn_id": client_txn_id,
            "shift_id": str(shift_id),
        }
        self._lists = {
            "product_id": list(product_ids),
            "quantity": list(quantities),
            "final_unit_price": list(final_unit_prices),
        }

    def __getitem__(self, key: str) -> Any:
        """返回销售表单的标量字段。"""
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        """迭代销售表单的标量字段名。"""
        return iter(self._values)

    def __len__(self) -> int:
        """返回销售表单的标量字段数量。"""
        return len(self._values)

    def get(self, key: str, default: Any = None) -> Any:
        """返回销售表单标量字段，不存在时返回默认值。"""
        return self._values.get(key, default)

    def getlist(self, key: str) -> list[str]:
        """按原始顺序返回重复明细字段。"""
        return list(self._lists.get(key, []))


def _api_serializer() -> URLSafeTimedSerializer:
    """创建使用应用 SECRET_KEY 的 API token 序列化器。"""
    return URLSafeTimedSerializer(
        current_app.config["SECRET_KEY"],
        salt=TOKEN_SALT,
    )


def _issue_token(user_id: int) -> str:
    """为启用用户签发 12 小时有效的 API token。"""
    return _api_serializer().dumps({"user_id": int(user_id)})


def _format_datetime(value: datetime) -> str:
    """按数据库约定格式化上海时区时间。"""
    return value.astimezone(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _is_json_integer(value: Any) -> bool:
    """判断值是否为 JSON 整数而非布尔值或浮点数。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_integer(value: Any, label: str) -> int:
    """校验并返回正整数。"""
    if not _is_json_integer(value) or int(value) <= 0:
        raise ApiValidationError(f"{label}必须为正整数")
    return int(value)


def _nonnegative_cents(value: Any, label: str) -> int:
    """校验并返回非负整数分。"""
    if not _is_json_integer(value) or int(value) < 0:
        raise ApiValidationError(f"{label}必须为非负整数分")
    return int(value)


def _cents_to_legacy_price(cents: int) -> str:
    """把整数分精确转换为销售服务接受的元字符串。"""
    amount = Decimal(int(cents)) / Decimal("100")
    return f"{amount:.2f}"


def login(username: Any, password: Any) -> dict[str, Any]:
    """校验启用账号并返回 API token 与用户信息。"""
    if not isinstance(username, str) or not isinstance(password, str):
        raise ApiValidationError("用户名和密码不能为空")
    normalized_username = username.strip()
    if not normalized_username or not password:
        raise ApiValidationError("用户名和密码不能为空")

    user = authenticate_user(normalized_username, password)
    if user is None:
        raise ApiAuthenticationError("用户名或密码错误")

    return {
        "token": _issue_token(int(user["user_id"])),
        "user": user_to_dict(user),
    }


def get_user_from_token(token: str) -> sqlite3.Row | None:
    """验证 API token 并返回仍处于启用状态的用户。"""
    if not token:
        return None
    try:
        payload = _api_serializer().loads(
            token,
            max_age=TOKEN_MAX_AGE_SECONDS,
        )
    except BadData:
        return None

    if not isinstance(payload, Mapping):
        return None
    user_id = payload.get("user_id")
    if not _is_json_integer(user_id) or int(user_id) <= 0:
        return None

    return get_db().execute(
        "SELECT user_id, username, role, salesperson_id "
        "FROM APP_USER WHERE user_id = ? AND status = '启用'",
        (int(user_id),),
    ).fetchone()


def user_to_dict(user: Mapping[str, Any]) -> dict[str, Any]:
    """把 APP_USER 行转换为可 JSON 序列化的用户对象。"""
    salesperson_id = user["salesperson_id"]
    return {
        "user_id": int(user["user_id"]),
        "username": str(user["username"]),
        "role": str(user["role"]),
        "salesperson_id": (
            int(salesperson_id) if salesperson_id is not None else None
        ),
    }


def require_salesperson_id(user: Mapping[str, Any]) -> int:
    """返回销售顾问编号；合伙人或未关联账号抛出权限错误。"""
    if str(user["role"]) != "销售顾问":
        raise PermissionError("仅销售顾问可访问此接口")
    salesperson_id = user["salesperson_id"]
    if salesperson_id is None or int(salesperson_id) <= 0:
        raise PermissionError("当前账号未关联销售顾问")
    return int(salesperson_id)


def get_current_shift(salesperson_id: int) -> dict[str, Any] | None:
    """返回销售顾问当前进行中的最新班次，没有时返回 None。"""
    now_text = _format_datetime(datetime.now(CHINA_TZ))
    row = get_db().execute(
        "SELECT sh.shift_id, sh.salesperson_id, sh.start_time, sh.end_time, "
        "sh.status, b.booth_id, b.booth_code, b.location, "
        "e.event_id, e.event_name, e.event_type "
        "FROM SHIFT AS sh "
        "JOIN BOOTH AS b ON b.booth_id = sh.booth_id "
        "JOIN EVENT AS e ON e.event_id = b.event_id "
        "JOIN SALESPERSON AS sp ON sp.salesperson_id = sh.salesperson_id "
        "WHERE sh.salesperson_id = ? "
        "AND sp.status = '在职' "
        "AND sh.status = '进行中' "
        "AND sh.start_time <= ? "
        "AND sh.end_time >= ? "
        "ORDER BY sh.start_time DESC, sh.shift_id DESC "
        "LIMIT 1",
        (salesperson_id, now_text, now_text),
    ).fetchone()
    if row is None:
        return None
    return {
        "shift_id": int(row["shift_id"]),
        "salesperson_id": int(row["salesperson_id"]),
        "start_time": str(row["start_time"]),
        "end_time": str(row["end_time"]),
        "status": str(row["status"]),
        "booth_id": int(row["booth_id"]),
        "booth_code": str(row["booth_code"]),
        "location": row["location"],
        "event_id": int(row["event_id"]),
        "event_name": str(row["event_name"]),
        "event_type": str(row["event_type"]),
    }


def list_active_products() -> list[dict[str, Any]]:
    """返回全部在售产品及整数分最低售价。"""
    rows = get_db().execute(
        "SELECT product_id, product_code, name, min_price_cents, category "
        "FROM PRODUCT WHERE status = '在售' ORDER BY product_code"
    ).fetchall()
    return [
        {
            "product_id": int(row["product_id"]),
            "product_code": str(row["product_code"]),
            "name": str(row["name"]),
            "min_price_cents": int(row["min_price_cents"]),
            "category": str(row["category"]),
        }
        for row in rows
    ]


def _build_sale_form(payload: Mapping[str, Any]) -> ApiSaleForm:
    """严格校验 JSON 销售请求并构造销售服务表单适配器。"""
    if not isinstance(payload, Mapping):
        raise ApiValidationError("请求体必须是 JSON 对象")

    client_txn_id = payload.get("client_txn_id")
    if not isinstance(client_txn_id, str) or not client_txn_id.strip():
        raise ApiValidationError("客户端交易编号不能为空")

    shift_id = _positive_integer(payload.get("shift_id"), "班次")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ApiValidationError("请至少提交一条销售明细")

    product_ids: list[str] = []
    quantities: list[str] = []
    final_unit_prices: list[str] = []
    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, Mapping):
            raise ApiValidationError(f"第 {index} 行销售明细格式无效")

        product_id = _positive_integer(
            item.get("product_id"),
            f"第 {index} 行产品",
        )
        quantity = _positive_integer(
            item.get("quantity"),
            f"第 {index} 行数量",
        )
        price_cents = _nonnegative_cents(
            item.get("final_unit_price_cents"),
            f"第 {index} 行最终单价",
        )
        product_ids.append(str(product_id))
        quantities.append(str(quantity))
        final_unit_prices.append(_cents_to_legacy_price(price_cents))

    return ApiSaleForm(
        client_txn_id=client_txn_id.strip(),
        shift_id=shift_id,
        product_ids=product_ids,
        quantities=quantities,
        final_unit_prices=final_unit_prices,
    )


def create_api_sale(
    payload: Mapping[str, Any],
    salesperson_id: int,
) -> dict[str, Any]:
    """复用销售登记服务写入销售、明细与佣金并返回整数分结果。"""
    form = _build_sale_form(payload)
    try:
        result = sales_service.create_sale(form, salesperson_id)
    except ValueError as exc:
        raise ApiValidationError(str(exc)) from exc

    sale = get_db().execute(
        "SELECT s.status, sh.salesperson_id "
        "FROM SALE AS s "
        "JOIN SHIFT AS sh ON sh.shift_id = s.shift_id "
        "WHERE s.sale_id = ?",
        (result.sale_id,),
    ).fetchone()
    if sale is None:
        raise ApiValidationError("销售单保存失败")
    if int(sale["salesperson_id"]) != salesperson_id:
        raise PermissionError("无权访问该销售单")

    return {
        "sale_id": result.sale_id,
        "total_cents": int(result.total_cents),
        "status": str(sale["status"]),
    }


def list_sales(salesperson_id: int, page_raw: Any) -> dict[str, Any]:
    """分页返回当前销售顾问的销售流水，每页固定 20 条。"""
    page_text = str(page_raw if page_raw is not None else "1").strip()
    if not POSITIVE_INTEGER_PATTERN.fullmatch(page_text) or int(page_text) <= 0:
        raise ApiValidationError("页码必须为正整数")

    try:
        context = sales_service.get_sales_list(
            {"page": page_text},
            user_role="销售顾问",
            user_salesperson_id=salesperson_id,
        )
    except ValueError as exc:
        raise ApiValidationError(str(exc)) from exc

    sales = [
        {
            "sale_id": int(row["sale_id"]),
            "sale_no": f"S{int(row['sale_id'])}",
            "sale_time": str(row["sale_time"]),
            "total_cents": int(row["total_cents"]),
            "status": str(row["status"]),
            "item_count": int(row["item_count"]),
            "commission_cents": int(row["commission_cents"]),
        }
        for row in context["sales"]
    ]
    return {
        "sales": sales,
        "page": int(context["page"]),
        "page_size": int(context["page_size"]),
        "total": int(context["total"]),
        "total_pages": int(context["total_pages"]),
    }


def _sum_sales_cents(
    salesperson_id: int,
    start_time: str,
    end_time: str,
) -> int:
    """汇总指定上海时区时间范围内的本人净销售额。"""
    row = get_db().execute(
        "SELECT COALESCE(SUM(si.line_cents), 0) "
        "FROM SALE_ITEM AS si "
        "JOIN SALE AS s ON s.sale_id = si.sale_id "
        "JOIN SHIFT AS sh ON sh.shift_id = s.shift_id "
        "WHERE sh.salesperson_id = ? "
        "AND s.status <> '作废' "
        "AND s.sale_time >= ? "
        "AND s.sale_time < ?",
        (salesperson_id, start_time, end_time),
    ).fetchone()
    return int(row[0] or 0) if row is not None else 0


def _sum_commission_cents(
    salesperson_id: int,
    start_time: str,
    end_time: str,
) -> int:
    """汇总指定上海时区时间范围内的本人有效佣金。"""
    row = get_db().execute(
        "SELECT COALESCE(SUM(ce.commission_cents), 0) "
        "FROM COMMISSION_ENTRY AS ce "
        "JOIN SALE AS s ON s.sale_id = ce.sale_id "
        "JOIN SHIFT AS sh ON sh.shift_id = s.shift_id "
        "WHERE sh.salesperson_id = ? "
        "AND s.status <> '作废' "
        "AND ce.status IN ('待结算', '已结算') "
        "AND s.sale_time >= ? "
        "AND s.sale_time < ?",
        (salesperson_id, start_time, end_time),
    ).fetchone()
    return int(row[0] or 0) if row is not None else 0


def get_summary(
    salesperson_id: int,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """按 Asia/Shanghai 本地时间汇总本人今日、本月销售与本月佣金。"""
    now = as_of or datetime.now(CHINA_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CHINA_TZ)
    else:
        now = now.astimezone(CHINA_TZ)

    today = now.date()
    tomorrow = today + timedelta(days=1)
    month_start = today.replace(day=1)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    today_start_text = f"{today.isoformat()} 00:00:00"
    tomorrow_start_text = f"{tomorrow.isoformat()} 00:00:00"
    month_start_text = f"{month_start.isoformat()} 00:00:00"
    next_month_start_text = f"{next_month.isoformat()} 00:00:00"

    return {
        "today_sales_cents": _sum_sales_cents(
            salesperson_id,
            today_start_text,
            tomorrow_start_text,
        ),
        "month_sales_cents": _sum_sales_cents(
            salesperson_id,
            month_start_text,
            next_month_start_text,
        ),
        "month_commission_cents": _sum_commission_cents(
            salesperson_id,
            month_start_text,
            next_month_start_text,
        ),
    }
