"""token 归因（billing step 1b）：把一次请求的 prompt token 拆到人设/记忆/历史/输入。

为什么需要：聊天平台的成本大头往往是 persona.system_prompt 每轮重复注入 +
长期记忆块 —— 没有归因，这部分在数据里不可见，既无法优化也无法差异化定价。
"""

from collections.abc import Sequence
from typing import Any

from app.llm.gateway import _estimate_tokens, _tiktoken_count

# 与 build_llm_messages 的组装顺序对齐
_SYSTEM_PERSONA_PREFIX = "[persona:"


def _count(text: str, model: str | None) -> int:
    n = _tiktoken_count(text, model)
    return n if n is not None else _estimate_tokens(text)


def attribute_prompt_tokens(
    llm_messages: Sequence[dict[str, Any]],
    *,
    model: str | None,
    user_input: str | None = None,
) -> dict[str, int]:
    """按消息在组装序列中的位置拆分 prompt token。

    约定（与 services.chat.build_llm_messages 一致）：
    - role=system 且以 [persona: 开头 → persona_prompt
    - role=system 其他（MEMORY_MARKER 记忆块）→ memory
    - role=user → history（含最后一轮输入）
    - 最后一条 user 的 content == user_input → 单独计入 current_input

    返回 {"persona_prompt","memory","history","current_input"}，
    四项之和 == 整个 llm_messages 的 token 总数（逐条计数后求和）。
    """
    attribution = {"persona_prompt": 0, "memory": 0, "history": 0, "current_input": 0}
    last_user_idx = None
    for i, m in enumerate(llm_messages):
        if m.get("role") == "user":
            last_user_idx = i

    for i, m in enumerate(llm_messages):
        content = m.get("content") or ""
        role = m.get("role")
        n = _count(content, model)
        if role == "system" and content.startswith(_SYSTEM_PERSONA_PREFIX):
            attribution["persona_prompt"] += n
        elif role == "system":
            attribution["memory"] += n
        elif role == "user" and i == last_user_idx and user_input is not None and content == user_input:
            attribution["current_input"] += n
        else:
            attribution["history"] += n
    return attribution


def prompt_total(llm_messages: Sequence[dict[str, Any]], *, model: str | None) -> int:
    """整个 prompt 的 token 总数（逐条计数，与 attribution 之和对齐）。"""
    return sum(_count(m.get("content") or "", model) for m in llm_messages)


def estimate_completion_tokens(text: str) -> int:
    """completion 估算（仅用于预扣阶段的保守估计；结算以实测值覆盖）。"""
    if not text:
        return 0
    n = _tiktoken_count(text, None)
    return n if n is not None else _estimate_tokens(text)
