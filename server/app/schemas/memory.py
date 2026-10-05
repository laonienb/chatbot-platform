"""长期记忆的请求/响应结构。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=500)
    category: str | None = Field(default=None, pattern=r"^(basic|preference|relationship|event|opinion|style)$")


class MemoryUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=500)


class MemoryOut(BaseModel):
    id: UUID
    persona_id: UUID
    content: str
    category: str
    source: str
    confidence: float
    created_at: datetime

    model_config = {"from_attributes": True}
