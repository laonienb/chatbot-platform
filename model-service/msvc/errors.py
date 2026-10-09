"""错误契约（协议 §7）：统一 OpenAI 错误体 + 机器可判 `code`。

模型服务是唯一知道「本次失败是否可能已计费」的一方，故错误体带 `retryable`——
`true` 表失败发生在进入上游处理之前（无副作用）⇒ 平台侧不入幂等窗（§8.2.1）。
"""

from dataclasses import dataclass

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
            {
                "error": {
                    "message": self.message,
                    "type": self.err_type,
                    "code": self.code,
                    "param": self.param,
                    "retryable": self.retryable,  # §7.1：缺省 false
                }
            },
            status_code=self.http_status,
            headers=headers,
        )


@dataclass
class ErrorSpec:
    """测试/注入用的错误描述。"""

    code: str
    http: int | None = None
    message: str = ""
    retry_after: int | None = None
    retryable: bool = False
