"""用户身份域：平台账号、外部身份绑定、API Key。"""

from datetime import datetime
from decimal import Decimal
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
    """用量账本（billing step 2a）：计费与配额的唯一依据，追加式不可变。

    生命周期状态机（双阶段结算）：
      pending   → 请求进入、额度预扣、上游调用前先落库（崩溃可恢复）
      settled   → 上游返回，用实测 usage 结算（唯一可产生账单的终态）
      failed    → 上游异常/客户端中断，按已生成部分结算或标记失败
      abandoned → 对账任务收编的 pending 残留（进程被 kill，按预扣估算结清）
    不变量：任何消耗了上游 token 的请求，最终必须恰好有一行终态记录。

    兼容层调用 conversation_id 为空；原生会话调用 api_key_id 为空。
    """

    __tablename__ = "usage_logs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_usage_logs_idempotency_key"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    api_key_id: Mapped[UUID | None] = mapped_column(ForeignKey("api_keys.id"))
    conversation_id: Mapped[UUID | None] = mapped_column(ForeignKey("conversations.id"), index=True)

    # --- 归属与幂等 ---
    idempotency_key: Mapped[str | None] = mapped_column(String(128))  # 重复请求不双扣
    source: Mapped[str] = mapped_column(String(16), default="native")  # native | compat
    channel: Mapped[str | None] = mapped_column(String(16))  # web / qq / wechat / miniprogram
    source_uid: Mapped[str | None] = mapped_column(String(128))  # 兼容层 user 字段（qq:12345）

    # --- 上游身份（对账前提）---
    model: Mapped[str] = mapped_column(String(128))  # 调用方请求的 model 字段（persona:slug / 裸名）
    model_upstream: Mapped[str | None] = mapped_column(String(128))  # persona 解析后的底层模型
    provider: Mapped[str | None] = mapped_column(String(32))  # openai / deepseek / …
    persona_id: Mapped[UUID | None] = mapped_column(Uuid)

    # --- 计量（区分实测/估算，这是可审计性的核心）---
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer, default=0)
    metering_source: Mapped[str | None] = mapped_column(String(16))  # provider | tiktoken | estimated
    attribution: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # persona/记忆/历史/输入 拆分

    # --- 价格快照（账单可复现的依据；规则改了历史账单依然能复现）---
    rating_rule_id: Mapped[str | None] = mapped_column(String(40))
    rate_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    cost_upstream: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))  # 我们付的（USD）
    cost_billed: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))  # 用户付的（rate unit）
    currency: Mapped[str | None] = mapped_column(String(3))  # credit / usd / cny
    cost: Mapped[float | None] = mapped_column(Numeric(12, 6))  # 兼容旧字段：= cost_billed

    # --- 生命周期状态机 ---
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/settled/failed/abandoned
    reserved_tokens: Mapped[int | None] = mapped_column(Integer)  # 阶段A预扣量
    reserved_cost: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    finish_reason: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)  # 计量/计价不可信
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    # 兼容属性：model 列现在存「请求的 model 字段」，model_upstream 是底层模型。
    # 旧读取方（usage 汇总/兼容层响应）继续读 model 即可；新增读取方优先用 model_upstream。
    @property
    def billed_model(self) -> str:
        return self.model_upstream or self.model
