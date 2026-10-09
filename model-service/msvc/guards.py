"""入站边界守卫（协议 §1 红线 1/10、§4.2）——这是 P0-3 的实现，也是全协议最核心的边界。

工作单 P0-3 明确：模型服务是「禁止字段出现即 400」的**责任方**（入站校验），平台的出网
守卫只是过渡纵深。这里做到"声明即实现"：任何平台业务身份字段出现在请求里，立即拒绝，
绝不把它带进后续上游调用。校验**只针对列名明确的禁用字段**（宽松解析：未知扩展字段放行）。
"""

from msvc.errors import ServiceError

# §4.2 禁止字段：出现即 400。trace 在 v1 禁发（§4.2 v1.5 收编），一并拦。
FORBIDDEN_TOP_FIELDS = frozenset(
    {
        "user_id",
        "user",
        "conversation_id",
        "conversation",
        "persona",
        "persona_id",
        "trace",
    }
)
# 消息条目里也不得夹带身份字段（不得借 messages 传平台身份）
FORBIDDEN_MESSAGE_FIELDS = frozenset({"user_id", "conversation_id", "persona_id", "tenant"})


def guard_request(body: dict) -> None:
    """校验请求体：persona: 前缀（红线10）+ 禁字段（红线1）。"""
    model = body.get("model", "")
    if isinstance(model, str) and model.startswith("persona:"):
        raise ServiceError(
            "invalid_request",
            "模型服务不认 persona:<slug>（红线10：人格是平台业务语义，不得下移）",
            err_type="invalid_request_error",
            param="model",
            retryable=False,
        )
    leaked = [k for k in FORBIDDEN_TOP_FIELDS if k in body]
    if leaked:
        raise ServiceError(
            "invalid_request",
            f"请求体含禁字段 {leaked}：平台业务身份不得下移（红线1）",
            err_type="invalid_request_error",
            param=leaked[0],
            retryable=False,
        )
    for idx, msg in enumerate(body.get("messages") or []):
        if isinstance(msg, dict):
            bad = [k for k in FORBIDDEN_MESSAGE_FIELDS if k in msg]
            if bad:
                raise ServiceError(
                    "invalid_request",
                    f"messages[{idx}] 夹带身份字段 {bad}（红线1）",
                    err_type="invalid_request_error",
                    param=f"messages[{idx}]",
                    retryable=False,
                )
