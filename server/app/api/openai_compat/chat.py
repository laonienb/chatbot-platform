"""OpenAI 兼容层：POST /v1/chat/completions（非流式 + SSE 流式）。

无状态模式：调用方（AstrBot 等）自管多轮历史；平台按 model="persona:<slug>"
注入人设后经 LLM 网关出话，错误与响应格式遵循 OpenAI 规范。
"""

import time
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse, StreamingResponse

from app.api.deps import DbSession, get_api_key_principal
from app.billing.ratelimit import rate_limit_compat
from app.core.sse import SSE_DONE, SSE_HEADERS, format_sse
from app.llm.gateway import StreamDone
from app.models import ApiKey, User
from app.schemas.openai_compat import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Usage,
    build_completion_response,
)
from app.services.chat import (
    ensure_admitted,
    persona_system_prompt,
    resolve_model,
    run_completion,
    run_completion_stream,
)

router = APIRouter(prefix="/v1", tags=["openai-compat"])


def openai_error(status_code: int, message: str, err_type: str = "invalid_request_error", code: str | None = None):
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": err_type, "param": None, "code": code}},
    )


@router.post("/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    body: ChatCompletionRequest,
    _rl: Annotated[None, Depends(rate_limit_compat)],
    db: DbSession = None,  # Annotated 内含 Depends(get_db)；默认值仅占位
    principal: tuple[ApiKey, User] = Depends(get_api_key_principal),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    api_key, user = principal

    if api_key.model_whitelist is not None and body.model not in api_key.model_whitelist:
        return openai_error(
            403, f"model `{body.model}` is not allowed for this API key", code="model_not_allowed"
        )

    # persona 解析在开流之前完成，model_not_found 仍以标准 404 返回
    # （非 persona 模型由 resolve_model 原样返回，永不抛）
    try:
        persona, model = await resolve_model(db, body.model)
    except LookupError:
        return openai_error(404, f"The model `{body.model}` does not exist", code="model_not_found")

    # 准入（配额 429 / 余额 402）在开流之前 —— 与原生面同一套异常，由全局
    # 异常处理器按 /v1 路径渲染成 OpenAI 错误格式
    raw_messages = [m.model_dump() for m in body.messages]
    llm_messages = (
        [{"role": "system", "content": persona_system_prompt(persona)}] + raw_messages
        if persona
        else raw_messages
    )
    await ensure_admitted(db, user_id=user.id, llm_messages=llm_messages, model=model)

    if body.stream:
        completion_id = f"chatcmpl-{uuid4().hex[:24]}"
        created = int(time.time())

        def chunk(delta: dict) -> dict:
            return {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": body.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
            }

        async def event_stream():
            yield format_sse(chunk({"role": "assistant"}))
            try:
                async for piece in run_completion_stream(
                    db,
                    user=user,
                    api_key=api_key,
                    model_field=body.model,
                    messages=[m.model_dump() for m in body.messages],
                    temperature=body.temperature,
                    top_p=body.top_p,
                    max_tokens=body.max_tokens,
                    source_uid=body.user,
                    idempotency_key=idempotency_key,
                ):
                    if isinstance(piece, StreamDone):
                        continue  # usage 记账已在服务层完成
                    yield format_sse(chunk({"content": piece}))
            except Exception as e:  # 开流后只能以事件形式报错（遵循 OpenAI 流式行为）
                yield format_sse({"error": {"message": f"LLM upstream error: {e}", "type": "api_error", "param": None, "code": None}})
                return
            yield SSE_DONE

        return StreamingResponse(event_stream(), media_type="text/event-stream", headers=SSE_HEADERS)

    try:
        result = await run_completion(
            db,
            user=user,
            api_key=api_key,
            model_field=body.model,
            messages=[m.model_dump() for m in body.messages],
            temperature=body.temperature,
            top_p=body.top_p,
            max_tokens=body.max_tokens,
            source_uid=body.user,
            idempotency_key=idempotency_key,
        )
    except LookupError:
        return openai_error(
            404, f"The model `{body.model}` does not exist", code="model_not_found"
        )
    except Exception as e:  # 上游 LLM 网关异常 → OpenAI 风格 502
        return openai_error(502, f"LLM upstream error: {e}", err_type="api_error")

    return build_completion_response(
        model=body.model,
        content=result.content,
        usage=Usage(
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
        ),
    )
