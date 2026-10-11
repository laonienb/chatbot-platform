"""错误契约（协议 §7）：统一 OpenAI 错误体 + 机器可判 `code`。

模型服务是唯一知道「本次失败是否可能已计费」的一方，故错误体带 `retryable`——
`true` 表失败发生在进入上游处理之前（无副作用）⇒ 平台侧不入幂等窗（§8.2.1）。
"""

from dataclasses import dataclass
from typing import Any

from starlette.responses import JSONResponse

# code → HTTP 状态（协议 §7 表）
CODE_HTTP: dict[str, int] = {
    "invalid_request": 400,
    "service_auth_failed": 401,
    "upstream_auth_failed": 401,
    "compliance_denied": 403,
    "model_not_found": 404,
    "context_length_exceeded": 413,
    "rate_limited": 429,
    "budget_exhausted": 429,
    "internal_error": 500,
    "upstream_error": 502,
    "service_unavailable": 503,
    "upstream_timeout": 504,
}


class ServiceError(Exception):
    """可映射为 OpenAI 错误体的服务异常。`retryable` 决定入窗与否（§8.2.1）。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        err_type: str = "api_error",
        param: str | None = None,
        retry_after: int | None = None,
        retryable: bool = False,
    ):
        self.code = code
        self.message = message
        self.err_type = err_type
        self.param = param
        self.retry_after = retry_after
        self.retryable = retryable
        super().__init__(message)

    @property
    def http_status(self) -> int:
        return CODE_HTTP.get(self.code, 502)

    def to_response(self) -> JSONResponse:
        headers = {"Retry-After": str(self.retry_after)} if self.retry_after is not None else {}
        return JSONResponse(
            {"error": self.to_payload()},
            status_code=self.http_status,
            headers=headers,
        )

    def to_payload(self) -> dict[str, Any]:
        """§7 错误体。HTTP 响应与**流内错误帧**共用此形状。

        `retry_after_s`（v1.7-澄清2 加法字段）：**流内错误帧没有 HTTP header 可带**，
        该字段是流式路径传递退避时长的唯一手段；有上游值才给，缺省整键不出现。
        """
        payload: dict[str, Any] = {
            "message": self.message,
            "type": self.err_type,
            "code": self.code,
            "param": self.param,
            "retryable": self.retryable,  # §7.1：缺省 false
        }
        if self.retry_after is not None:
            payload["retry_after_s"] = int(self.retry_after)
        return payload


@dataclass
class ErrorSpec:
    """测试/注入用的错误描述。"""

    code: str
    http: int | None = None
    message: str = ""
    retry_after: int | None = None
    retryable: bool = False


# ---------------------------------------------------------------- 真实上游异常族 → §7 code
#
# SW-4（协议 §24.2 R4）：此前只有 `_ScenarioError`（假桩专用）被映射，真实
# `LiteLLMProvider` 抛出的 litellm 异常族（BadRequestError / AuthenticationError /
# RateLimitError / APIConnectionError / Timeout / …）**全部逃逸**，后果两重：
#   ① 响应是裸 500，违反 §7（无 code/retryable/Retry-After）；
#   ② `_finish_window()` 不被调用 ⇒ 幂等**在飞位永久泄漏**，同 key 之后恒 409。
# 本函数把异常族映射成 ServiceError，判据用**鸭子类型**（`status_code` + 类名 + 消息），
# 不 import litellm —— 既不与真实依赖耦合，也让测试能以注入的假异常覆盖同一分支。


def _exc_status(exc: BaseException) -> int | None:
    for attr in ("status_code", "code", "http_status", "resp_status_code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int) and 100 <= val < 600:
            return val
    return None


def _exc_retry_after(exc: BaseException) -> int | None:
    for attr in ("retry_after", "retry_after_seconds"):
        val = getattr(exc, attr, None)
        if isinstance(val, (int, float)) and val >= 0:
            return int(val)
    headers = getattr(exc, "headers", None)
    if isinstance(headers, dict):
        raw = headers.get("Retry-After") or headers.get("retry-after")
        try:
            if raw is not None:
                return int(float(raw))
        except (TypeError, ValueError):
            return None
    return None


def from_upstream_exception(exc: BaseException) -> ServiceError:
    """把任意上游异常映射为带 §7 `code` 的 ServiceError。

    `retryable` 的轴是**§8.2.1 的性质**而非状态码段：
    - 请求未送达 / 上游瞬时拒绝（429、503 未就绪）⇒ `true`（不入窗，重发可能成功）；
    - 结果不明（超时、已发起调用后的 5xx）与上游永久性拒绝 ⇒ `false`（入窗，防双烧）。
    """
    if isinstance(exc, ServiceError):
        return exc

    name = type(exc).__name__
    status = _exc_status(exc)
    message = str(exc) or f"上游异常 {name}"
    lowered = message.lower()
    retry_after = _exc_retry_after(exc)

    # 上下文超长：同输入必同拒绝（永久性，入窗）——判据不依赖"是否已计费"（§8.2.1 v1.5）
    if "contextwindow" in name.lower() or "context_length" in name.lower() or (
        "context length" in lowered or "maximum context" in lowered or "too long" in lowered
    ):
        return ServiceError(
            "context_length_exceeded", message, err_type="invalid_request_error",
            param="messages", retryable=False,
        )
    if "authentication" in name.lower() or status == 401:
        return ServiceError(
            "upstream_auth_failed", message, err_type="auth_error",
            param="api_key", retryable=False,
        )
    if "notfound" in name.lower() or status == 404:
        return ServiceError("model_not_found", message, err_type="invalid_request_error", param="model")
    if "ratelimit" in name.lower() or status == 429:
        return ServiceError(
            "rate_limited", message, err_type="rate_limit_error",
            retry_after=retry_after, retryable=True,  # 上游瞬时拒绝 → 不入窗（§8.2.1）
        )
    if "budget" in name.lower() or "credit" in lowered or "quota exceeded" in lowered:
        return ServiceError("budget_exhausted", message, err_type="billing_error", retryable=False)
    # 请求未送达（连接层）：无副作用 → 不入窗，重发安全
    if "connection" in name.lower() or "dns" in name.lower() or "apiconnection" in name.lower():
        return ServiceError("upstream_error", message, err_type="api_connection_error", retryable=True)
    if "timeout" in name.lower() or status == 504:
        return ServiceError("upstream_timeout", message, err_type="timeout_error", retryable=False)
    if status == 503:
        return ServiceError("service_unavailable", message, err_type="api_error", retry_after=retry_after, retryable=True)
    if status == 500:
        return ServiceError("internal_error", message, err_type="api_error", retryable=False)
    if status in (400, 404, 413) or (status is not None and 400 <= status < 500):
        # 已到达上游的永久性拒绝：同输入必同拒绝 → 入窗
        return ServiceError("invalid_request", message, err_type="invalid_request_error", retryable=False)
    if status is not None and 500 <= status < 600:
        return ServiceError("upstream_error", message, err_type="api_error", retryable=False)
    # 兜底：未知异常按「已发起调用后结果不明」处理——**必须带 code**，绝不裸 500，
    # 且 retryable=False ⇒ 入窗防双烧（宁可多缓存，不可双烧真金白银）。
    return ServiceError("upstream_error", message, err_type="api_error", retryable=False)

