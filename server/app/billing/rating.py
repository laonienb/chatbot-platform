"""费率表（billing step 3）。

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
    """按模型的积分系数：credit_multiplier = 『不同模型按比例扣积分』的可调旋钮。"""

    __tablename__ = "model_ratings"
    __table_args__ = (UniqueConstraint("rating_rule_id", "model", name="uq_rating_rule_model"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    rating_rule_id: Mapped[str] = mapped_column(ForeignKey("rating_rules.id"), index=True)
    model: Mapped[str] = mapped_column(String(128))  # 带 provider 前缀的 LiteLLM 模型串
    credit_multiplier: Mapped[Decimal] = mapped_column(Numeric(10, 4), default=Decimal("1"))
    base_credits_per_request: Mapped[Decimal] = mapped_column(Numeric(20, 10), default=Decimal("0"))


async def load_rating_rule(
    db, *, user_id=None, model: str | None = None
) -> tuple[RatingRule | None, ModelRating | None]:
    """命中优先级：user 覆盖 > plan > platform。无规则返回 (None, None)。

    首次请求（库里一条规则都没有）自动播种平台默认 per_token 规则 ——
    与 llm_models 的 _seed_if_empty 同一模式。播种失败不阻塞请求
    （计费走 upstream 兜底 + needs_review）。

    返回 (rule, model_rating) —— model_rating 是该模型在该规则下的积分系数，
    无专属系数时为 None（计费时按 credit_multiplier=1 兜底）。
    """
    rows = (
        (
            await db.execute(
                select(RatingRule)
                .where(RatingRule.enabled == True)  # noqa: E712
                .order_by(RatingRule.priority.desc())
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        await _seed_platform_rule(db)
        rows = (
            (
                await db.execute(
                    select(RatingRule)
                    .where(RatingRule.enabled == True)  # noqa: E712
                    .order_by(RatingRule.priority.desc())
                )
            )
            .scalars()
            .all()
        )
    rule = None
    for row in rows:
        if row.scope == "user" and str(row.scope_ref_id) != str(user_id):
            continue
        rule = row  # scope=platform / plan（plan 匹配留 step 5）
        break
    if rule is None or not model:
        return rule, None

    mr = (
        await db.execute(
            select(ModelRating).where(ModelRating.rating_rule_id == rule.id, ModelRating.model == model)
        )
    ).scalars().first()
    return rule, mr


async def _seed_platform_rule(db) -> None:
    """播种平台默认规则（per_token，credit 计价）。幂等：已有规则时不动。

    费率是「平台自定价」的起点（litellm 价格表只用于 cost_upstream 毛利
    核算，不决定用户价）。运营商按需在管理端调这些数字。
    """
    try:
        exists = (await db.execute(select(RatingRule.id).limit(1))).scalar_one_or_none()
        if exists is not None:
            return
        db.add(
            RatingRule(
                id="platform-token",
                name="平台默认（按 token 计积分）",
                scope="platform",
                priority=100,
                charge_dimension="per_token",
                unit="credit",
                # 1 credit ≈ 1000 prompt token 的等价刻度；毛利由
                # rate × upstream 的比值决定，运营期再调。
                rate_input=Decimal("0.001"),
                rate_output=Decimal("0.002"),
                rate_cache_read=Decimal("0.0005"),
                rate_reasoning=Decimal("0.002"),  # 推理 token 按 output 价
                cost_basis="retail",
                enabled=True,
            )
        )
        await db.commit()
    except Exception:
        # 并发播种/表未迁移时静默 —— 计费走 needs_review 兜底，不阻塞请求
        await db.rollback()


def rule_snapshot(rule: RatingRule | None, mr: ModelRating | None) -> dict | None:
    """把命中的费率规则序列化为 JSON 快照 —— 规则改了历史账单仍可复现。"""
    if rule is None:
        return None
    return {
        "rule_id": rule.id,
        "charge_dimension": rule.charge_dimension,
        "unit": rule.unit,
        "base_amount": str(rule.base_amount),
        "rate_input": str(rule.rate_input),
        "rate_output": str(rule.rate_output),
        "rate_reasoning": str(rule.rate_reasoning),
        "rate_cache_read": str(rule.rate_cache_read),
        "rate_cache_write": str(rule.rate_cache_write),
        "cost_basis": rule.cost_basis,
        "credit_multiplier": str(mr.credit_multiplier) if mr else "1",
        "base_credits_per_request": str(mr.base_credits_per_request) if mr else "0",
    }


def compute_billed(
    snap: dict | None,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cache_read_tokens: int = 0,
    reasoning_tokens: int = 0,
) -> tuple[Decimal, str] | None:
    """按费率快照算用户应付（billed）。无快照返回 None（调用方走 upstream 兜底）。

    四种 charge_dimension：
    - per_token：纯 token 用量计费
    - per_request：按次（含可选 token 超额）
    - hybrid：底价 + 用量（推荐给积分制；底价来自规则或模型专属 base_credits）
    - period_allowance：周期性额度 —— 正确扣减发生在 period_counters（step 5），
      这里 billed 记 0，只计 cost_upstream 做毛利核算。

    reasoning_tokens 已含在 completion_tokens 内（litellm 口径），因此只在
    rate_reasoning != rate_output 时补差额，避免双计。
    """
    if not snap:
        return None
    dim = snap.get("charge_dimension")
    unit = snap.get("unit", "credit")
    if dim == "period_allowance":
        return Decimal("0"), unit

    base = Decimal(snap.get("base_amount", "0"))
    r_in = Decimal(snap.get("rate_input", "0"))
    r_out = Decimal(snap.get("rate_output", "0"))
    r_reason = Decimal(snap.get("rate_reasoning", "0"))
    r_cache = Decimal(snap.get("rate_cache_read", "0"))
    mult = Decimal(snap.get("credit_multiplier", "1"))
    base_credits = Decimal(snap.get("base_credits_per_request", "0"))

    token_part = (
        Decimal(prompt_tokens) * r_in
        + Decimal(completion_tokens) * r_out
        + Decimal(cache_read_tokens) * r_cache
    )
    if dim == "per_request":
        amount = base + token_part
    elif dim == "hybrid":
        amount = base + base_credits + token_part
        if mult != 1:
            amount *= mult
    else:  # per_token（默认）
        amount = token_part * mult
        if r_reason and r_reason != r_out and reasoning_tokens:
            amount += Decimal(reasoning_tokens) * (r_reason - r_out)

    return round(amount, 10), unit
