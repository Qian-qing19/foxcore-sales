"""微信小程序 JSON API 路由。"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
import json
from typing import Any

from flask import Blueprint, Response, g, request

from app.services import api_service


bp = Blueprint("api", __name__, url_prefix="/api")


def _json_response(payload: Any, status: int = 200) -> Response:
    """以 UTF-8 JSON 返回成功结果。"""
    return Response(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        status=status,
        content_type="application/json; charset=utf-8",
    )


def _error(message: str, status: int) -> Response:
    """返回统一的 JSON 错误体。"""
    return _json_response({"error": message}, status=status)


def api_login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """要求请求携带有效 Bearer token，并把用户放入 g.api_user。"""
    @wraps(view)
    def wrapped_view(*args: Any, **kwargs: Any) -> Any:
        authorization = request.headers.get("Authorization", "")
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
            return _error("请先登录", 401)

        user = api_service.get_user_from_token(parts[1])
        if user is None:
            return _error("登录已失效，请重新登录", 401)
        g.api_user = user
        return view(*args, **kwargs)

    return wrapped_view


def _json_object() -> dict[str, Any]:
    """读取 JSON 对象请求体，非法格式按 400 处理。"""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise api_service.ApiValidationError("请求体必须是 JSON 对象")
    return payload


@bp.errorhandler(api_service.ApiValidationError)
def handle_validation_error(error: api_service.ApiValidationError) -> Response:
    """把参数校验失败转换为 400 JSON。"""
    return _error(str(error) or "请求参数无效", 400)


@bp.errorhandler(api_service.ApiAuthenticationError)
def handle_authentication_error(
    error: api_service.ApiAuthenticationError,
) -> Response:
    """把登录凭据失败转换为 401 JSON。"""
    return _error(str(error) or "用户名或密码错误", 401)


@bp.errorhandler(PermissionError)
def handle_permission_error(error: PermissionError) -> Response:
    """把角色或数据范围错误转换为 403 JSON。"""
    return _error(str(error) or "无权访问此接口", 403)


@bp.post("/login")
def login() -> Response:
    """校验账号密码并签发 API token。"""
    payload = _json_object()
    result = api_service.login(
        payload.get("username"),
        payload.get("password"),
    )
    return _json_response(result)


@bp.get("/me")
@api_login_required
def me() -> Response:
    """返回当前 token 对应的用户信息。"""
    return _json_response(api_service.user_to_dict(g.api_user))


@bp.get("/shifts/current")
@api_login_required
def current_shift() -> Response:
    """返回销售顾问当前进行中的班次，没有时为 JSON null。"""
    salesperson_id = api_service.require_salesperson_id(g.api_user)
    return _json_response(api_service.get_current_shift(salesperson_id))


@bp.get("/products")
@api_login_required
def products() -> Response:
    """返回全部在售产品。"""
    return _json_response(api_service.list_active_products())


@bp.post("/sales")
@api_login_required
def create_sale() -> Response:
    """登记一笔销售并返回销售编号、合计分与状态。"""
    salesperson_id = api_service.require_salesperson_id(g.api_user)
    payload = _json_object()
    return _json_response(
        api_service.create_api_sale(payload, salesperson_id),
    )


@bp.get("/sales")
@api_login_required
def sales() -> Response:
    """分页返回当前销售顾问本人的销售流水。"""
    salesperson_id = api_service.require_salesperson_id(g.api_user)
    return _json_response(
        api_service.list_sales(
            salesperson_id,
            request.args.get("page", "1"),
        ),
    )


@bp.get("/summary")
@api_login_required
def summary() -> Response:
    """返回当前销售顾问今日、本月销售与本月佣金。"""
    salesperson_id = api_service.require_salesperson_id(g.api_user)
    return _json_response(api_service.get_summary(salesperson_id))
