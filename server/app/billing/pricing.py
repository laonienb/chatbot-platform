"""计价引擎（步骤0+3 核心）。

两条设计红线：
1. **计价永不抛异常** —— token 已经消耗之后，定价失败不能把请求拖挂，
   也不能让账单丢失。统一返回带标记的 Decimal 结果（needs_review=True）。
2. **模型串必须带 provider 前缀**（openai/gpt-4o-mini、deepseek/deepseek-chat）。
   实测：litellm.cost_per_token 对裸的 deepseek-chat 会抛 BadRequestError，
   即使它就在 model_cost 表里 —— 因为无法反推 provider。
"""

from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

_USD_PER_MTOK_FALLBACK = Decimal("0")  # 无法定价时记 0 + needs_review


@dataclass(frozen=True)
class PriceQuote:
    """一次计价的结果。needs_review=True 表示这个数字不可信，需要人工/对账复核。"""

    upstream: Decimal  # 上游原价（USD 计价，由 LiteLLM 价格表推导）
    basis: str  # 'litellm_map' | 'needs_review'
    needs_review: bool


def normalize_model_string(raw: str | None) -> str | None:
    """补 provider 前缀。裸 deepseek-chat → deepseek/deepseek-chat。

    已带前缀的（含 'provider/model'）原样返回；未知的裸名尽力归一化，
    归一不出来的标记 needs_review 路由给调用方。
    """
    if not raw:
        return raw
    if "/" in raw:
        return raw
    # 常见 provider 的裸模型名映射；未命中则不猜 —— 让上游报 404，
    # 计价层负责不崩，而不是负责猜对。
    head = raw.split("-")[0].lower()
    known = {
        "gpt": "openai",
        "o1": "openai",
        "o3": "openai",
        "text": "openai",
        "claude": "anthropic",
        "gemini": "gemini",
        "deepseek": "deepseek",
        "qwen": "openai",  # DashScope 的 openai 兼容端点
        "glm": "openai",
        "moonshot": "openai",
        "grok": "xai",
        "llama": "openai",
        "mistral": "mistral",
        "kimi": "moonshot",
    }
    prefix = known.get(head)
    return f"{prefix}/{raw}" if prefix else raw


@lru_cache(maxsize=2048)
def _price_upstream(model: str, prompt: int, completion: int, cache_read: int, reasoning: int) -> tuple[Decimal, str, bool]:
    """底层：litellm.cost_per_token + 价格表。**带缓存，失败不抛**。"""
    try:
        import litellm  # lazy import：mock 模式无需装/加载

        p, c = litellm.cost_per_token(
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            custom_llm_provider=model.split("/", 1)[0] if "/" in model else None,
        )
        quote = Decimal(str(p)) + Decimal(str(c))

        # 缓存读差价：litellm 没给 rate 字段时用价格表手工补
        # （deepseek-chat cache=10%、gpt-4o-mini cache=50%，直接算进去）
        if cache_read:
            info = getattr(litellm, "model_cost", {}).get(model.split("/", 1)[-1]) or getattr(litellm, "model_cost", {}).get(model)
            cache_rate = info.get("cache_read_input_token_cost") if info else None
            if cache_rate is not None:
                # 从原价扣掉 cache_read 的差价（cost_per_token 已按全量 input 算过）
                full = Decimal(str(info.get("input_cost_per_token", 0) or 0)) * Decimal(cache_read)
                cached = Decimal(str(cache_rate)) * Decimal(cache_read)
                quote -= (full - cached)

        # reasoning token 已包含在 completion_tokens 里（litellm 返回的
        # completion_tokens 已是总数），这里仅在有 reasoning 且价格表单独
        # 计价时补充差额 —— 当前 litellm 价格表按 output 价统一收，无需补。
        return round(quote, 10), "litellm_map", False
    except Exception:
        return _USD_PER_MTOK_FALLBACK, "needs_review", True


def quote_upstream_cost(
    model: str | None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_read_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> PriceQuote:
    """公开入口：永不抛异常。未知模型 → (0, needs_review=True)。"""
    if not model:
        return PriceQuote(_USD_PER_MTOK_FALLBACK, "needs_review", True)
    norm = normalize_model_string(model)
    upstream, basis, needs_review = _price_upstream(
        norm or model,
        int(prompt_tokens or 0),
        int(completion_tokens or 0),
        int(cache_read_tokens or 0),
        int(reasoning_tokens or 0),
    )
    return PriceQuote(upstream, basis, needs_review)



