import os
from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# LiteLLM 价格表：跳过远程拉取，直接用包内备份（实测：远程拉取带 3 次重试，
# 冷启动 9s+，离线/内网环境直接超时）。价格表随 litellm 版本走，本地备份
# 足够；需要更新时升级 litellm 而不是每次启动联网。
# 必须在任何 litellm import 之前设置 —— config 是 app 的入口模块，放这里最早。
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")


class Settings(BaseSettings):
    """全局配置。所有项均可被环境变量 / .env 覆盖。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 基础
    app_name: str = "chatbot-platform"
    debug: bool = False

    # 数据库：开发/测试可用 sqlite+aiosqlite，生产用 postgresql+asyncpg
    database_url: str = "sqlite+aiosqlite:///./dev.db"

    # 认证
    secret_key: str = "dev-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30

    # LLM 网关：mock 用于本地开发/测试（无需任何真实 API Key）
    llm_backend: str = "mock"  # mock | litellm
    llm_default_model: str = "gpt-4o-mini"
    llm_api_key: str = ""  # LiteLLM 后端使用的上游 Key（或由各模型 env 提供）
    llm_base_url: str = ""  # 可选：统一指向自建 LiteLLM 代理

    # CORS
    cors_origins: str = "http://localhost:3000"

    # 限流（billing step 6）：全局默认 RPM；API Key 可按 key 覆盖
    rpm_default: int = 120

    # 周期 token 额度（billing step 5）：None/0 = 不限额。超限准入拒绝 429。
    quota_monthly_tokens: int | None = None

    # 注册赠送积分：余额准入（402）的启动资金。0 = 不送（注册即需充值）。
    signup_grant_credits: Decimal = Decimal("1000")

    # 对账任务周期（秒）：收编残留 pending + 毛利倒挂告警。0 = 关闭。
    reconcile_interval_seconds: int = 300

    # 1 积分折合多少 USD —— 毛利监控专用（billed 是 credit、upstream 是 USD，
    # 不可直接相减）。默认 1 即数值上等价于未换算；运营期按真实兑换比例调。
    credit_to_usd: Decimal = Decimal("1")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
