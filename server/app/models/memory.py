"""长期记忆（DESIGN.md §13.1）：persona 级事实记忆，蒸馏与对话提取共用。"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import Uuid

from app.database import Base, utcnow


class Memory(Base):
    __tablename__ = "memories"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    persona_id: Mapped[UUID] = mapped_column(ForeignKey("personas.id"), index=True)
    content: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(32), default="basic")  # basic/preference/relationship/event/opinion/style
    source: Mapped[str] = mapped_column(String(16), default="chat")  # chat(自动提取)/import(蒸馏导入)/manual(手动)
    source_ref: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # 原句摘录等来源定位（蒸馏/审计用）
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
