"""SSE 编码：平台第一方客户端与 OpenAI 兼容层共用 OpenAI chunk 格式。"""

import json


def format_sse(obj: object) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


SSE_DONE = "data: [DONE]\n\n"

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",  # Nginx 反代时禁用缓冲
}
