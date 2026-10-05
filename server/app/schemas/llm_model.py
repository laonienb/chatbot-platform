"""LLM 模型注册表的请求/响应结构。api_key 不回显，只返回 has_key。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class LlmModelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)  # LiteLLM 模型串
    api_base: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=512)
    is_default: bool = False
    sort: int = 0


class LlmModelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    api_base: str | None = Field(default=None, max_length=512)
    api_key: str | None = Field(default=None, max_length=512)
    enabled: bool | None = None
    is_default: bool | None = None
    sort: int | None = None


class LlmModelOut(BaseModel):
    id: UUID
    name: str
    model: str
    api_base: str | None
    has_key: bool
    enabled: bool
    is_default: bool
    sort: int
    created_at: datetime

    model_config = {"from_attributes": True}


def to_out(row) -> LlmModelOut:
    return LlmModelOut(
        id=row.id,
        name=row.name,
        model=row.model,
        api_base=row.api_base,
        has_key=bool(row.api_key),
        enabled=row.enabled,
        is_default=row.is_default,
        sort=row.sort,
        created_at=row.created_at,
    )
