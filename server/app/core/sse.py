"""SSE 编码：平台第一方客户端与 OpenAI 兼容层共用 OpenAI chunk 格式。"""

import json


def format_sse(obj: object) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


SSE_DONE = "data: [DONE]\n\n"

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",  # Nginx 反代时禁用缓冲
}


def stream_error_frame(
    code: str | None,
    message: str,
    *,
    err_type: str = "api_error",
    param: str | None = None,
    retryable: bool = False,
    retry_after_s: int | None = None,
) -> dict:
    """§6.7 的对外流内错误帧（OpenAI 同形：`data: {"error": {…}}`，无 `event:` 名）。

    发完错误帧**必须仍发 `SSE_DONE`**（G3-1）：`[DONE]` 是客户端判定的唯一正常终止信号，
    少了它就无法区分「错误收尾」与「连接异常断开」——平台自己也是按 `[DONE]` 收流的。

    `retry_after_s`：v1.7-澄清2 的加法字段。流内帧**没有 HTTP header 可携带** `Retry-After`，
    这是流式路径传退避时长的唯一手段；缺失则客户端按自有策略退避。
    """
    err: dict = {
        "message": message,
        "type": err_type,
        "code": code,
        "param": param,
        "retryable": retryable,
    }
    if retry_after_s is not None:
        err["retry_after_s"] = int(retry_after_s)
    return {"error": err}
