from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
