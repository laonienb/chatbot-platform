"""模型服务配置（环境变量注入，§3/§8.1/§9）。无凭证入 git；生产由部署侧提供。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MS_", env_file=".env", extra="ignore")

    # 服务间鉴权（§3）：接受多个 token（新旧并行轮转窗口），逗号分隔。至少配一个。
    service_tokens: str = "dev-insecure-model-service-token"
    # 上游：fake（离线确定性，测试/本地）| litellm（真实 provider）
    upstream: str = "fake"
    litellm_api_base: str = ""
    litellm_api_key: str = ""
    litellm_default_model: str = "deepseek/deepseek-chat"

    # 合规硬策略（§4.3）：命中 deny 列表的 provider 一律 403，调用方不可放宽（红线5）。
    compliance_deny_providers: str = "openai,anthropic"

    # 目录（§9）：本服务对外声明的模型集合，逗号分隔（真实由上游/provider 注册表决定）。
    catalog_models: str = "deepseek-chat,qwen-plus"

    # 超时（§8.1，模型服务侧自持，红线8：内层 < 平台流式空闲上限 90s）。
    upstream_first_token_timeout: float = 15.0
    upstream_total_timeout: float = 60.0

    # 幂等窗口（§8.2.1）：≥10 分钟。窗口对**流式/非流式同等适用**（§8.2.2）。
    idempotency_window_seconds: int = 600
    # 窗口缓存的条数/体积上限（§8.2.2-3：缓存必须有界）。超限按插入序淘汰最旧条目，
    # 被淘汰的 key 后续重发按未命中处理（会真实调用上游）——语义已声明于 README。
    idempotency_cache_max_entries: int = 256
    idempotency_cache_max_chars: int = 1_000_000

    protocol_version: int = 1
    implementation_name: str = "model-service"
    implementation_version: str = "0.1.0"

    @property
    def token_set(self) -> set[str]:
        return {t.strip() for t in self.service_tokens.split(",") if t.strip()}

    @property
    def deny_providers(self) -> set[str]:
        return {p.strip().lower() for p in self.compliance_deny_providers.split(",") if p.strip()}

    @property
    def models(self) -> list[str]:
        return [m.strip() for m in self.catalog_models.split(",") if m.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
