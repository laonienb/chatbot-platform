"""OpenAI 兼容层：POST /v1/chat/completions（M0 非流式）。

无状态模式：调用方（AstrBot 等）自管多轮历史；平台按 model="persona:<slug>"
注入人设后经 LLM 网关出话，错误与响应格式遵循 OpenAI 规范。
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api.deps import DbSession, get_api_key_principal
from app.models import ApiKey, User
from app.schemas.openai_compat import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    Usage,
    build_completion_response,
)
from app.services.chat import run_completion

router = APIRouter(prefix="/v1", tags=["openai-compat"])


def openai_error(status_code: int, message: str, err_type: str = "invalid_request_error", code: str | None = None):
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": err_type, "param": None, "code": code}},
    )


@router.post("/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    body: ChatCompletionRequest,
    principal: tuple[ApiKey, User] = Depends(get_api_key_principal),
    db: DbSession = None,
):
    api_key, user = principal

    if body.stream:
        return openai_error(400, "streaming is not supported yet (planned for M1)")
    if api_key.model_whitelist is not None and body.model not in api_key.model_whitelist:
        return openai_error(
            403, f"model `{body.model}` is not allowed for this API key", code="model_not_allowed"
        )

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
