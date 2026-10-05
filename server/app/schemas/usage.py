"""用量统计（来自 usage_logs 账本）。"""

from pydantic import BaseModel


class UsageByModel(BaseModel):
    model: str
    requests: int
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class UsageOut(BaseModel):
    days: int  # 统计窗口
    total_requests: int
    total_prompt_tokens: int
    total_completion_tokens: int
    by_model: list[UsageByModel]
