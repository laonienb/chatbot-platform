"""LLM 网关封装。

两个后端：
- mock：确定性假回复，用于本地开发与测试（无需任何真实 API Key）；
- litellm：经 LiteLLM 调真实模型（OpenAI / DeepSeek / Claude / Gemini…）。
由环境变量 LLM_BACKEND 选择。lazy import，未装 litellm 时 mock 后端仍可用。

流式协议：chat_stream 返回异步迭代器，逐个 yield 内容增量（str），
结束时 yield 一个 StreamDone 携带记账所需信息。
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from app.config import get_settings


@dataclass
class LLMResult:
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class StreamDone:
    """流式结束标记，携带与 LLMResult 对齐的记账信息。"""

    model: str
    prompt_tokens: int
    completion_tokens: int


def _estimate_tokens(text: str) -> int:
    # 粗略估算（≈4 字符/token），仅 mock 与兜底记账用
    return max(1, len(text) // 4)


class MockBackend:
    """确定性 mock：回复中携带 persona 标识与用户末句，便于验证人设注入与链路。"""

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        persona_tag = ""
        if system.startswith("[persona:"):
            persona_tag = system.split("]", 1)[0] + "] "
        content = f"{persona_tag}[mock:{model}] 收到：{last_user}"
        return LLMResult(
            content=content,
            model=model,
            prompt_tokens=sum(_estimate_tokens(m["content"]) for m in messages),
            completion_tokens=_estimate_tokens(content),
        )

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str | StreamDone]:
        result = await self.chat(messages, model, temperature, top_p, max_tokens)
        chunk_size = 8
        for i in range(0, len(result.content), chunk_size):
            yield result.content[i : i + chunk_size]
            await asyncio.sleep(0)  # 让出事件循环，模拟真实逐块到达
        yield StreamDone(
            model=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )


class LiteLLMBackend:
    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        import litellm  # lazy import：mock 模式无需安装/加载

        settings = get_settings()
        kwargs: dict[str, Any] = {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if top_p is not None:
            kwargs["top_p"] = top_p
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if settings.llm_api_key:
            kwargs["api_key"] = settings.llm_api_key
        if settings.llm_base_url:
            kwargs["api_base"] = settings.llm_base_url

        resp = await litellm.acompletion(model=model, messages=messages, **kwargs)
        usage = resp.usage
        return LLMResult(
            content=resp.choices[0].message.content or "",
            model=model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str | StreamDone]:
        import litellm  # lazy import：mock 模式无需安装/加载

        settings = get_settings()
        kwargs: dict[str, Any] = {}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if top_p is not None:
            kwargs["top_p"] = top_p
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if settings.llm_api_key:
            kwargs["api_key"] = settings.llm_api_key
        if settings.llm_base_url:
            kwargs["api_base"] = settings.llm_base_url

        resp = await litellm.acompletion(
            model=model, messages=messages, stream=True, stream_options={"include_usage": True}, **kwargs
        )
        prompt_tokens = 0
        parts: list[str] = []
        async for chunk in resp:
            if getattr(chunk, "usage", None) is not None:
                prompt_tokens = getattr(chunk.usage, "prompt_tokens", 0) or prompt_tokens
            if chunk.choices:
                content = chunk.choices[0].delta.content
                if content:
                    parts.append(content)
                    yield content
        yield StreamDone(
            model=model,
            prompt_tokens=prompt_tokens or _estimate_tokens("".join(m["content"] for m in messages)),
            completion_tokens=_estimate_tokens("".join(parts)),
        )


def get_llm_backend() -> MockBackend | LiteLLMBackend:
    backend = get_settings().llm_backend.lower()
    if backend == "litellm":
        return LiteLLMBackend()
    return MockBackend()


async def chat_stream(
    messages: list[dict[str, str]],
    model: str,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
) -> AsyncIterator[str | StreamDone]:
    """便捷入口：按配置选后端并开始流式输出。"""
    async for piece in get_llm_backend().chat_stream(messages, model, temperature, top_p, max_tokens):
        yield piece
