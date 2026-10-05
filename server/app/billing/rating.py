"""费率表（步骤3）。

计价口径声明（cost_basis）：
- upstream：我们付的上游原价（USD），用于毛利监控
- retail：用户付的价（credits/usd/cny），用于实际扣减

同一模型同时可有 upstream 和 retail 两行；取「先 retail，后 upstream」的优先级。
"""

from decimal import Decimal

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Numeric, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, utcnow


class RatingRule(Base):
    """一条计费规则 = 一个计费口径。按优先级匹配，请求时快照进账本行。"""

    __tablename__ = "rating_rules"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # 稳定 slug，如 'platform-token'
    name: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(16), default="platform")  # platform | plan | user
    scope_ref_id: Mapped[str | None] = mapped_column(String(64))  # plan_id / user_id
    priority: Mapped[int] = mapped_column(default=100)

    charge_dimension: Mapped[str] = mapped_column(String(32))  # per_token | per_request | hybrid | period_allowance
    unit: Mapped[str] = mapped_column(String(16), default="credit")  # credit | usd | cny
    base_amount: Mapped[Decimal] = mapped_column(Numeric(20, 10), default=Decimal("0"))

    rate_input: Mapped[Decimal] = mapped_column(Numeric(20, 10), default=Decimal("0"))
    rate_output: Mapped[Decimal] = mapped_column(Numeric(20, 10), default=Decimal("0"))
    rate_reasoning: Mapped[Decimal] = mapped_column(Numeric(20, 10), default=Decimal("0"))
    rate_cache_read: Mapped[Decimal] = mapped_column(Numeric(20, 10), default=Decimal("0"))
    rate_cache_write: Mapped[Decimal] = mapped_column(Numeric(20, 10), default=Decimal("0"))

    cost_basis: Mapped[str] = mapped_column(String(16), default="retail")  # upstream | retail

    effective_from: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)
    effective_to: Mapped[object | None] = mapped_column(DateTime(timezone=True))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ModelRating(Base):
    """按模型的积分系数：credit_multiplier = 你想要的『不同模型按比例扣积分』。"""

    __tablename__ = "model_ratings"
    __table_args__ = (UniqueConstraint("rating_rule_id", "model", name="uq_rating_rule_model"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    rating_rule_id: Mapped[str] = mapped_column(ForeignKey("rating_rules.id"), index=True)
    model: Mapped[str] = mapped_column(String(128))  # 带 provider 前缀的 LiteLLM 模型串
    credit_multiplier: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal("1"))
    base_credits_per_request: Mapped[Decimal] = mapped_column(Numeric(20, 10), default=Decimal("0"))


async def resolve_rating_rule(db, *, user_id=None, model: str | None = None) -> RatingRule:
    """命中优先级：user 覆盖 > plan > platform。未命中返回 None（调用方走降级）。"""
    stmt = (
        select(RatingRule)
        .where(RatingRule.enabled == True)  # noqa: E712
        .order_by(RatingRule.priority.desc())
    )
    rows = (await db.execute(stmt)).scalars().all()
    for row in rows:
        if row.scope == "user" and str(row.scope_ref_id) != str(user_id):
            continue
        # scope=plan 的在订阅模块未接入时先按 platform 兜底；plan 匹配留 TODO
        if row.scope == "platform" or row.scope == "user" or row.scope == "plan":
            return row
    return None
