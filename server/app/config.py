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
    llm_backend: str = "mock"  # mock | litellm | remote
    llm_default_model: str = "gpt-4o-mini"
    llm_api_key: str = ""  # LiteLLM 后端使用的上游 Key（或由各模型 env 提供）
    llm_base_url: str = ""  # 可选：统一指向自建 LiteLLM 代理

    # 模型服务（S3/S4，契约见 docs/model-service-protocol.md v1）。仅 LLM_BACKEND=remote 时生效。
    model_service_mode: str = "native"  # proxy | native（协议 §8.3）。默认 native：版本不符即拒启动（红线7）
    model_service_base_url: str = ""  # 集群内网地址（不暴露公网），如 http://model-service:8000
    model_service_token: str = ""  # 服务间静态 Bearer（§3）；env 注入，不落库、不进 git
    model_service_version: int = 1  # X-Model-Service-Version 客户端声明值
    model_service_timeout_total: float = 120.0  # 平台总超时（§8.1，必须 > 模型服务上游超时）
    model_service_timeout_connect: float = 5.0  # 建连超时
    model_service_timeout_first_byte: float = 15.0  # 平台读超时：任一 chunk 间隔（§8.1）
    # §8.1 流式空闲上限：无**任何**字节（含心跳）超过此时长 → 断开并按 §8.4 降级。
    # httpx 的 total 超时对流式不适用，故必须显式实现空闲计时（审计缺口 K）。
    # 必须 > 模型服务上游总超时（60s），满足红线 8「内层 < 外层」。
    model_service_stream_idle_timeout: float = 90.0
    # 生产环境守卫（§8.4 尾句 / 红线 9）：APP_ENV=prod 且 LLM_BACKEND=mock → 拒绝启动
    app_env: str = "dev"  # dev | prod
    model_service_fallback_direct: str = ""  # §8.4 可选直连降级模型；默认空 = 不降级，直接 503
    model_service_catalog_sync_seconds: int = 300  # §9 目录同步周期（秒），0 = 关闭定期同步
    model_service_cb_threshold: int = 5  # §11 熔断：滑动窗口连续 N 次 5xx/连接错 → 打开
    model_service_cb_reset_seconds: float = 30.0  # 熔断打开后进入半开探测的冷却秒数

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
