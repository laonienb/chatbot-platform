"""Step 0+1b：网关计量升级 —— StreamDone 扩充字段（cache/reasoning/finish_reason/needs_review）。"""

from dataclasses import dataclass


@dataclass
class LLMResult:
    content: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int = 0
    reasoning_tokens: int = 0
    finish_reason: str | None = None
    metering_source: str = "provider"  # provider | tiktoken | estimated
    needs_review: bool = False  # 计量不可信，计价需复核
