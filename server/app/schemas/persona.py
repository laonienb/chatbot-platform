from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class PersonaCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    slug: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
    system_prompt: str = Field(min_length=1, max_length=20000)
    model: str | None = Field(default=None, max_length=128)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1, le=200000)
    opening_message: str | None = Field(default=None, max_length=4000)
    visibility: str = Field(default="private", pattern=r"^(private|public)$")
    tags: list[str] | None = None
    avatar_url: str | None = Field(default=None, max_length=512)
    memory_enabled: bool = True


class PersonaUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    system_prompt: str | None = Field(default=None, min_length=1, max_length=20000)
    model: str | None = Field(default=None, max_length=128)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1, le=200000)
    opening_message: str | None = Field(default=None, max_length=4000)
    visibility: str | None = Field(default=None, pattern=r"^(private|public)$")
    tags: list[str] | None = None
    avatar_url: str | None = Field(default=None, max_length=512)
    memory_enabled: bool | None = None


class PersonaOut(BaseModel):
    id: UUID
    owner_id: UUID
    slug: str
    name: str
    avatar_url: str | None
    system_prompt: str
    model: str | None
    temperature: float | None
    top_p: float | None
    max_tokens: int | None
    opening_message: str | None
    visibility: str
    status: str
    forked_from: UUID | None
    tags: list[str] | None
    memory_enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class PersonaMarketOut(PersonaOut):
    """市场卡片：附作者昵称。"""

    owner_name: str | None = None
