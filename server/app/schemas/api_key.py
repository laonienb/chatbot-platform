from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    model_whitelist: list[str] | None = None
    expires_at: str | None = None  # ISO 8601，可选


class ApiKeyOut(BaseModel):
    """列表项：不含明文（明文只在创建时返回一次）。"""

    id: UUID
    name: str
    key_prefix: str
    model_whitelist: list[str] | None
    revoked: bool
    last_used_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ApiKeyCreatedOut(ApiKeyOut):
    key: str = ""  # 明文，仅创建响应中由路由层填充
