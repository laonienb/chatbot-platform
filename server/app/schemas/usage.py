"""用量统计（来自 usage_logs 账本）与钱包余额。"""

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


class WalletOut(BaseModel):
    """积分余额（供 402 引导与余额卡片；从未有钱包记录的用户为 0）。"""

    balance: float  # 当前余额（结算即扣的真实值）
    lifetime_topup: float  # 累计入账（含注册赠送）
