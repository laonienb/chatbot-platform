"""会话域：有状态模式（第一方客户端）。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import Uuid

from app.database import Base, utcnow


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    persona_id: Mapped[UUID] = mapped_column(ForeignKey("personas.id"))
    channel: Mapped[str] = mapped_column(String(16), default="web")  # web / qq / wechat / miniprogram
    title: Mapped[str | None] = mapped_column(String(128))
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user / assistant / system
    content: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
