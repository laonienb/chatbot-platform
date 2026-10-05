"""人设域：人格的唯一真源。slug 用于 model="persona:<slug>" 寻址。"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import Uuid

from app.database import Base, utcnow


class Persona(Base):
    __tablename__ = "personas"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    owner_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    avatar_url: Mapped[str | None] = mapped_column(String(512))
    system_prompt: Mapped[str] = mapped_column(String(20000))
    model: Mapped[str | None] = mapped_column(String(128))  # 底层模型偏好，空则用平台默认
    temperature: Mapped[float | None] = mapped_column(Float)
    top_p: Mapped[float | None] = mapped_column(Float)
    max_tokens: Mapped[int | None] = mapped_column(Integer)
    opening_message: Mapped[str | None] = mapped_column(String(4000))
    visibility: Mapped[str] = mapped_column(String(16), default="private")  # private / public
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / reviewing / banned
    forked_from: Mapped[UUID | None] = mapped_column(Uuid)
    tags: Mapped[list[str] | None] = mapped_column(JSON)
    memory_enabled: Mapped[bool] = mapped_column(Boolean, default=True)  # 长期记忆开关
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
