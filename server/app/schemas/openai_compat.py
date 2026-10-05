"""OpenAI 兼容层的请求/响应结构（遵循 OpenAI 规范）。"""

import time
from uuid import uuid4

from pydantic import BaseModel, Field


class ChatMessageIn(BaseModel):
    role: str = Field(pattern=r"^(system|user|assistant|tool)$")
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = Field(min_length=1, max_length=128)
    messages: list[ChatMessageIn] = Field(min_length=1)
    stream: bool = False  # M0 不支持流式；M1 落地 SSE
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1, le=200000)
    user: str | None = Field(default=None, max_length=128)  # 来源身份（如 qq:12345），用于归因


# ---------- 响应 ----------


class ChoiceMessage(BaseModel):
    role: str = "assistant"
    content: str


class Choice(BaseModel):
    index: int = 0
    message: ChoiceMessage
    finish_reason: str = "stop"


class Usage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: Usage


def build_completion_response(model: str, content: str, usage: Usage) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id=f"chatcmpl-{uuid4().hex[:24]}",
        created=int(time.time()),
        model=model,
        choices=[Choice(message=ChoiceMessage(content=content))],
        usage=usage,
    )


class ErrorBody(BaseModel):
    message: str
    type: str = "invalid_request_error"
    code: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
