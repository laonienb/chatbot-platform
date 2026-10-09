"""服务间鉴权（协议 §3）：静态 Bearer，接受新旧双 token（轮转窗口）。"""

from msvc.config import get_settings
from msvc.errors import ServiceError


def require_auth(authorization: str | None) -> None:
    """校验 Authorization 头。不匹配任何有效 token → 401 service_auth_failed（区别于上游凭证失效）。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise ServiceError(
            "service_auth_failed", "缺少服务间 Bearer token", err_type="auth_error"
        )
    token = authorization[len("Bearer ") :].strip()
    if not token or token not in get_settings().token_set:
        raise ServiceError(
            "service_auth_failed", "服务间 token 无效", err_type="auth_error"
        )
