"""用户身份域：平台账号、外部身份绑定、API Key。"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import expression
from sqlalchemy import Uuid

from app.database import Base, utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16), default="user")  # user / admin
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Identity(Base):
    """外部身份绑定：qq / wechat / wecom / miniprogram 等挂到平台账号。"""

    __tablename__ = "identities"
    __table_args__ = (UniqueConstraint("provider", "provider_uid", name="uq_identity_provider_uid"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    provider_uid: Mapped[str] = mapped_column(String(128))
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiKey(Base):
    """机器客户端凭证（如 AstrBot）。明文只在创建时返回一次，库里只存哈希。"""

    __tablename__ = "api_keys"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    key_prefix: Mapped[str] = mapped_column(String(16))  # 展示用前缀，如 sk-ab12…
    model_whitelist: Mapped[list[str] | None] = mapped_column(JSON)  # None=不限
    rpm_limit: Mapped[int | None] = mapped_column(Integer)
    daily_token_limit: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, server_default=expression.false())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UsageLog(Base):
    """用量账本：计费与配额的唯一依据。兼容层调用 conversation_id 为空。"""

    __tablename__ = "usage_logs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    api_key_id: Mapped[UUID | None] = mapped_column(ForeignKey("api_keys.id"))
    conversation_id: Mapped[UUID | None] = mapped_column(ForeignKey("conversations.id"), index=True)
    model: Mapped[str] = mapped_column(String(128))
    persona_id: Mapped[UUID | None] = mapped_column(Uuid)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[float | None] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
