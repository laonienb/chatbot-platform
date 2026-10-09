"""LLM 网关封装。

两个后端：
- mock：确定性假回复，用于本地开发与测试（无需任何真实 API Key）；
- litellm：经 LiteLLM 调真实模型（OpenAI / DeepSeek / Claude / Gemini…）。
由环境变量 LLM_BACKEND 选择。lazy import，未装 litellm 时 mock 后端仍可用。

⚠️ 导入顺序约束：litellm 的首次 import 若 `LITELLM_LOCAL_MODEL_COST_MAP` 尚未置位，
会去远程拉取价格表（实测 ConnectTimeout + 3 次重试 ≈ 9.6s，离线环境必现）。
该变量由 `app.config` 在任何 litellm 导入之前设置 —— 所以**不要在本模块顶层
import litellm**，也不要绕过 `app.config` 直接使用本模块。

流式协议：chat_stream 返回异步迭代器，逐个 yield 内容增量（str），
结束时 yield 一个 StreamDone 携带记账所需信息。

计量精度（计费 step 1）：
- litellm 流式路径攒下全部 chunks，收尾用 stream_chunk_builder 拼回完整
  ModelResponse —— 拿到的 prompt/completion token 是实测值，而非
  completion_tokens = len//4 这样的字符估算（后者对推理模型方向性错误）。
- 拿不到 usage 时回退 litellm.token_counter（tiktoken），再回退字符估算，
  并把 metering_source 标成 tiktoken / estimated，needs_review 同步置位，
  计价层据此决定是否需要人工复核。
"""

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.config import get_settings
from app.core.constants import MEMORY_MARKER

if TYPE_CHECKING:
    from app.llm.remote import RemoteBackend


@dataclass
class LLMResult:
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    # 计费扩展（step 1）：默认 0，只在能实测时填，mock 不产生这些
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    finish_reason: str | None = None
    metering_source: str = "provider"  # provider | tiktoken | estimated
    needs_review: bool = False  # 计量不可信 → 计价需复核
    # 模型服务成本归因（S4，协议 §5）。仅 RemoteBackend 填；mock/litellm 保持 None，
    # 结算层据 cost_status 走价格表回落。cost_usd 只影响 upstream 一侧，绝不用于扣积分。
    provider: str | None = None  # 实际供应商 → usage_logs.provider
    model_used: str | None = None  # 实际调用模型串 → usage_logs.model_upstream
    cost_usd: Decimal | None = None  # 十进制；不可得为 None，绝不 0（红线2）
    cost_status: str | None = None  # exact | estimated | unknown
    fallback_used: bool = False

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class StreamDone:
    """流式结束标记，携带与 LLMResult 对齐的记账信息。"""

    model: str
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    finish_reason: str | None = None
    metering_source: str = "provider"
    needs_review: bool = False
    # 模型服务成本归因（S4，协议 §5/§6），语义同 LLMResult
    provider: str | None = None
    model_used: str | None = None
    cost_usd: Decimal | None = None
    cost_status: str | None = None
    fallback_used: bool = False


def _estimate_tokens(text: str) -> int:
    # 粗略估算（≈4 字符/token），仅 mock 与兜底记账用
    return max(1, len(text) // 4)


def stream_done_from_result(r: LLMResult) -> StreamDone:
    """LLMResult → StreamDone，逐字段对齐（含模型服务成本归因字段）。

    非流式路径结算时用它，避免手写构造漏掉新增字段（cost_usd/provider 等）导致
    settle_usage 拿不到上游成本、白白回落价格表。
    """
    return StreamDone(
        model=r.model,
        prompt_tokens=r.prompt_tokens,
        completion_tokens=r.completion_tokens,
        cache_read_tokens=r.cache_read_tokens,
        reasoning_tokens=r.reasoning_tokens,
        finish_reason=r.finish_reason,
        metering_source=r.metering_source,
        needs_review=r.needs_review,
        provider=r.provider,
        model_used=r.model_used,
        cost_usd=r.cost_usd,
        cost_status=r.cost_status,
        fallback_used=r.fallback_used,
    )


# ---------- tiktoken 本地计数（计费步1：精确计量的核心） ----------
# 关键约束：tiktoken.get_encoding 首次调用会**联网下载 BPE 文件**（实测
# 36s，离线环境直接超时挂住）。因此只在「本地缓存已命中」时用 tiktoken，
# 否则返回 None 让调用方回退字符估算 —— 计量标记 metering_source 会如实
# 记成 estimated，绝不能为了精确把请求挂死在一次网络下载上。
_TIKTOKEN_ENCODING = None  # 惰性缓存：None=未探, False=不可用, 对象=可用
_ENCODING_NAME = "cl100k_base"


def _get_encoding() -> Any:
    """返回 tiktoken 编码；**绝不触发联网下载**。缓存缺失 / tiktoken 缺失 → None。

    tiktoken.get_encoding 在 BPE 文件未落盘时会联网下载（实测 36s，离线
    环境挂住），因此先查 data-gym 缓存目录，命中才加载（本地 IO，~0s）。
    """
    global _TIKTOKEN_ENCODING
    if _TIKTOKEN_ENCODING is not False:
        return _TIKTOKEN_ENCODING
    try:
        import tiktoken

        # 缓存目录：TIKTOKEN_CACHE_DIR > LOCALAPPDATA/data-gym-cache > ~/.cache/data-gym-cache
        if os.environ.get("TIKTOKEN_CACHE_DIR"):
            cache_dir = Path(os.environ["TIKTOKEN_CACHE_DIR"])
        elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
            cache_dir = Path(os.environ["LOCALAPPDATA"]) / "data-gym-cache"
        else:
            cache_dir = Path.home() / ".cache" / "data-gym-cache"
        if not cache_dir.is_dir() or not any(cache_dir.iterdir()):
            _TIKTOKEN_ENCODING = False  # 无缓存 → 禁用（避免联网）
            return None
        enc = tiktoken.get_encoding(_ENCODING_NAME)  # 缓存命中 → 本地 IO
        _TIKTOKEN_ENCODING = enc
        return enc
    except Exception:
        _TIKTOKEN_ENCODING = False
        return None


def _tiktoken_count(text: str, model: str | None) -> int | None:
    """tiktoken 实际计数。本地缓存未命中 / tiktoken 缺失时返回 None（回退估算）。"""
    if not text:
        return 0
    enc = _get_encoding()
    if enc is None:
        return None
    try:
        return len(enc.encode(text, disallowed_special=()))
    except Exception:
        return None


def _usage_parts(obj: Any, model: str) -> tuple[int, int, int, int, str | None]:
    """从 ModelResponse（或带 usage 的对象）提 (prompt, completion, cache, reasoning, finish)。"""
    usage = getattr(obj, "usage", None)
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    completion = getattr(usage, "completion_tokens", 0) or 0
    # cache 明细：prompt_tokens_details.cached_tokens（OpenAI/DeepSeek 格式）
    details = getattr(usage, "prompt_tokens_details", None)
    cache_read = getattr(details, "cached_tokens", 0) or 0
    # reasoning 明细：completion_tokens_details.reasoning_tokens（推理模型计价用）
    comp_details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(comp_details, "reasoning_tokens", 0) or 0
    finish = None
    try:
        finish = obj.choices[0].finish_reason
    except Exception:
        pass
    return int(prompt), int(completion), int(cache_read), int(reasoning), finish


def _backend_kwargs(api_base: str | None, api_key: str | None) -> dict[str, Any]:
    """按请求覆盖凭证（来自 llm_models 注册表）；空则回落到全局配置。"""
    settings = get_settings()
    kwargs: dict[str, Any] = {}
    base = api_base if api_base is not None else (settings.llm_base_url or None)
    key = api_key if api_key is not None else (settings.llm_api_key or None)
    if base:
        kwargs["api_base"] = base
    if key:
        kwargs["api_key"] = key
    return kwargs


class MockBackend:
    """确定性 mock：回复中携带 persona 标识与用户末句，便于验证人设注入与链路。"""

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
        idempotency_key: str | None = None,  # noqa: ARG002 — 仅 remote 后端消费
    ) -> LLMResult:
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        persona_tag = ""
        if system.startswith("[persona:"):
            persona_tag = system.split("]", 1)[0] + "] "
        content = f"{persona_tag}[mock:{model}] 收到：{last_user}"
        if MEMORY_MARKER in "\n".join(m["content"] for m in messages if m["role"] == "system"):
            # 回显第一条记忆，便于测试注入链路
            facts = [
                line[2:]
                for m in messages
                if m["role"] == "system"
                for line in m["content"].splitlines()
                if line.startswith("- ")
            ]
            if facts:
                content += f" |已知:{facts[0]}"
        prompt_tokens = sum(_estimate_tokens(m["content"]) for m in messages)
        completion_tokens = _estimate_tokens(content)
        return LLMResult(
            content=content,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            finish_reason="stop",
            metering_source="estimated",  # mock 就是估算，诚实标注
            needs_review=False,  # mock 本身是假数，不进真实计价流，不标复核
        )

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
        idempotency_key: str | None = None,  # noqa: ARG002 — 仅 remote 后端消费
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
            cache_read_tokens=result.cache_read_tokens,
            reasoning_tokens=result.reasoning_tokens,
            finish_reason=result.finish_reason,
            metering_source=result.metering_source,
            needs_review=result.needs_review,
        )


class LiteLLMBackend:
    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
        idempotency_key: str | None = None,  # noqa: ARG002 — 仅 remote 后端消费
    ) -> LLMResult:
        import litellm  # lazy import：mock 模式无需安装/加载

        kwargs = _backend_kwargs(api_base, api_key)
        if temperature is not None:
            kwargs["temperature"] = temperature
        if top_p is not None:
            kwargs["top_p"] = top_p
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        resp = await litellm.acompletion(model=model, messages=messages, **kwargs)
        prompt, completion, cache_read, reasoning, finish = _usage_parts(resp, model)
        try:
            content = resp.choices[0].message.content or ""
        except Exception:
            content = ""

        metering = "provider"
        if prompt == 0 or completion == 0:
            # usage 缺失 → tiktoken 补；tiktoken 也失败再回退字符估算
            metering = "tiktoken"
            prompt = prompt or _tiktoken_count(content, model) or _estimate_tokens(
                "".join(m["content"] for m in messages)
            )
            completion = completion or _tiktoken_count(content, model) or _estimate_tokens(content)
            if completion == 0 or prompt == 0:
                metering = "estimated"

        return LLMResult(
            content=content,
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_read_tokens=cache_read,
            reasoning_tokens=reasoning,
            finish_reason=finish,
            metering_source=metering,
            needs_review=metering != "provider",
        )

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
        idempotency_key: str | None = None,  # noqa: ARG002 — 仅 remote 后端消费
    ) -> AsyncIterator[str | StreamDone]:
        import litellm  # lazy import：mock 模式无需安装/加载

        kwargs = _backend_kwargs(api_base, api_key)
        if temperature is not None:
            kwargs["temperature"] = temperature
        if top_p is not None:
            kwargs["top_p"] = top_p
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        resp = await litellm.acompletion(
            model=model, messages=messages, stream=True, stream_options={"include_usage": True}, **kwargs
        )

        parts: list[str] = []
        chunks: list[Any] = []  # 攒下全部 chunk，收尾用 builder 拼回真实 usage
        async for chunk in resp:
            chunks.append(chunk)
            if chunk.choices:
                content = chunk.choices[0].delta.content
                if content:
                    parts.append(content)
                    yield content

        # 收尾：builder 拼回完整响应拿真实 usage；失败或缺字段回退 tiktoken/估算
        prompt = completion = cache_read = reasoning = 0
        finish: str | None = None
        metering = "tiktoken"
        try:
            built = litellm.stream_chunk_builder(chunks, messages=messages)
            if built is not None:
                prompt, completion, cache_read, reasoning, finish = _usage_parts(built, model)
                if prompt and completion:
                    metering = "provider"
        except Exception:
            pass

        if prompt == 0:
            joined_msg = "".join(m["content"] for m in messages)
            prompt = _tiktoken_count(joined_msg, model) or _estimate_tokens(joined_msg)
        if completion == 0:
            joined = "".join(parts)
            completion = _tiktoken_count(joined, model) or _estimate_tokens(joined)
            metering = "estimated" if not completion else metering

        yield StreamDone(
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_read_tokens=cache_read,
            reasoning_tokens=reasoning,
            finish_reason=finish,
            metering_source=metering,
            needs_review=metering != "provider",
        )


def get_llm_backend() -> "MockBackend | LiteLLMBackend | RemoteBackend":
    backend = get_settings().llm_backend.lower()
    if backend == "litellm":
        return LiteLLMBackend()
    if backend == "remote":
        from app.llm.remote import RemoteBackend  # lazy：非 remote 模式无需 httpx 客户端

        return RemoteBackend()
    return MockBackend()


async def chat_stream(
    messages: list[dict[str, str]],
    model: str,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    idempotency_key: str | None = None,
) -> AsyncIterator[str | StreamDone]:
    """便捷入口：按配置选后端并开始流式输出。"""
    backend = get_llm_backend()
    async for piece in backend.chat_stream(
        messages, model, temperature, top_p, max_tokens, api_base, api_key, idempotency_key
    ):
        yield piece
