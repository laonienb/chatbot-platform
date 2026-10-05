"""IM 路由策略（M4 启用）：平台群绑定表，供 AstrBot 插件查询用哪个 persona。"""

from uuid import UUID, uuid4

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import expression
from sqlalchemy import Uuid

from app.database import Base


class ImBinding(Base):
    __tablename__ = "im_bindings"
    __table_args__ = (UniqueConstraint("platform", "platform_chat_id", name="uq_im_platform_chat"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    platform: Mapped[str] = mapped_column(String(32))  # qq / wechat / …
    platform_chat_id: Mapped[str] = mapped_column(String(128))  # 群号 / 私聊身份
    persona_id: Mapped[UUID] = mapped_column(ForeignKey("personas.id"))
    trigger_mode: Mapped[str] = mapped_column(String(16), default="at")  # at / always / wake_word
    wake_word: Mapped[str | None] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=expression.true())
