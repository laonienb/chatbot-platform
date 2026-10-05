"""计价引擎（billing step 0）。

两条设计红线：
1. **计价永不抛异常** —— token 已消耗之后，定价失败不能拖挂请求、也不能丢账单。
   统一返回带 needs_review 标记的 Decimal 结果，宁可记一笔待对账。
2. **模型串归一化** —— litellm.cost_per_token 对裸 deepseek-chat 会抛
   BadRequestError（即使它就在 model_cost 表里），因为无法反推 provider。
   计价入口负责剥 persona:<slug>、把裸模型名补上 provider 前缀。

计价口径：
- upstream = 我们付的上游原价（USD，litellm 价格表推导）→ 毛利监控
- billed   = 用户付的价（credits/usd/cny，rating_rules 推导）→ 实际扣减
"""

from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

_ZERO = Decimal("0")

# 裸模型名 → provider。按头部 token 匹配；未命中不猜 —— 让上游自己报 404。
# 计价层的职责是「不崩」，不是「猜对」：猜错会把账单记到错误的费率上。
_PROVIDER_BY_HEAD = {
    "gpt": "openai",
    "o1": "openai",
    "o3": "openai",
    "o4": "openai",
    "text": "openai",
    "davinci": "openai",
    "chatgpt": "openai",
    "claude": "anthropic",
    "gemini": "gemini",
    "deepseek": "deepseek",
    "grok": "xai",
    "llama": "openai",
    "mistral": "mistral",
    "qwen": "openai",  # DashScope 的 openai 兼容端点
    "glm": "openai",
    "moonshot": "moonshot",
    "kimi": "moonshot",
    "yi": "openai",
    "command": "cohere",
}


@dataclass(frozen=True)
class PriceQuote:
    """一次计价结果。needs_review=True 表示数字不可信，需人工/对账复核。"""

    upstream: Decimal  # 上游原价（USD）
    basis: str  # litellm_map | needs_review
    needs_review: bool


def normalize_model_string(raw: str | None) -> str | None:
    """补 provider 前缀。裸 deepseek-chat → deepseek/deepseek-chat。

    已带前缀的（含 'provider/model'）原样返回；persona:<slug> 不是真实
    模型，原样返回交给调用方（_price_upstream 会失败 → needs_review 兜底）。
    未命中的裸名返回 None（调用方据此标 needs_review，而不是猜一个 provider）。
    """
    if not raw:
        return raw
    if raw.startswith("persona:"):
        return raw  # 调用方 bug：persona 未解析就来计价 → needs_review
    if "/" in raw:
        return raw
    head = raw.split("-")[0].lower()
    prefix = _PROVIDER_BY_HEAD.get(head)
    return f"{prefix}/{raw}" if prefix else None


@lru_cache(maxsize=1)
def _known_providers() -> frozenset[str]:
    """litellm 认可的 provider 前缀集合（lazy import；mock 模式不加载 litellm）。"""
    from litellm.types.utils import LlmProviders

    return frozenset(p.value for p in LlmProviders)


@lru_cache(maxsize=4096)
def _price_upstream_cached(model: str, prompt: int, completion: int, cache_read: int) -> tuple[str, str, bool]:
    """底层：litellm.cost_per_token + 价格表。带缓存、失败不抛。"""
    try:
        import litellm  # lazy import：mock 模式无需装/加载

        prompt = max(0, int(prompt))
        completion = max(0, int(completion))
        cache_read = max(0, int(cache_read))

        provider = model.split("/", 1)[0] if "/" in model else None
        p, c = litellm.cost_per_token(
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            custom_llm_provider=provider,
        )
        quote = Decimal(str(p)) + Decimal(str(c))

        # 缓存读差价：cost_per_token 按全量 input 算过，这里把 cache_read 部分
        # 打到 cache 价（gpt-4o-mini=50%、deepseek=10%）。聊天场景人设
        # system prompt 每轮重复必然命中缓存，这个折扣必须进计价。
        if cache_read:
            info = getattr(litellm, "model_cost", {}).get(model) or getattr(
                litellm, "model_cost", {}
            ).get(model.split("/", 1)[-1])
            input_rate = info.get("input_cost_per_token") if info else None
            cache_rate = info.get("cache_read_input_token_cost") if info else None
            if input_rate is not None and cache_rate is not None:
                quote -= (Decimal(str(input_rate)) - Decimal(str(cache_rate))) * Decimal(cache_read)

        return str(round(quote, 10)), "litellm_map", False
    except Exception:
        return str(_ZERO), "needs_review", True


def quote_upstream_cost(
    model: str | None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cache_read_tokens: int = 0,
    reasoning_tokens: int = 0,  # noqa: ARG001 — 已含在 completion_tokens 内，litellm 价格表按 output 价收
) -> PriceQuote:
    """公开入口：**永不抛异常**。未知/未解析模型 → (0, needs_review=True)。

    reasoning_tokens 当前不单独补价：litellm 返回的 completion_tokens 已含
    推理 token，价格表也按 output 价统一收。留参数是为了价格表未来单独
    计价时无需改调用方。
    """
    norm = normalize_model_string(model) if model else None
    if not norm or norm.startswith("persona:"):
        # persona:<slug> 未经解析就到计价层（调用方 bug）。litellm 会把 "persona"
        # 当 provider 名，抛 BadRequestError 并内部重试，实测每次约 1.9s。结论与问完
        # litellm 相同（无可信价格），直接标 needs_review，零成本、无副作用。
        return PriceQuote(_ZERO, "needs_review", True)

    if "/" in norm:
        # 带前缀的模型：前缀必须是 litellm 认识的 provider，否则同 persona:
        # 一样每次白跑约 1.9s（litellm 抛 BadRequestError + 内部重试 +
        # 打印 provider 列表）。结论本就是 needs_review，直接短路。
        # 前缀集合来自 litellm 自身（LlmProviders），不硬编码、不写死版本。
        prefix = norm.split("/", 1)[0].lower()
        if prefix not in _known_providers():
            return PriceQuote(_ZERO, "needs_review", True)

    total, basis, needs_review = _price_upstream_cached(
        norm, int(prompt_tokens or 0), int(completion_tokens or 0), int(cache_read_tokens or 0)
    )
    return PriceQuote(Decimal(total), basis, needs_review)
