"""钱包操作（billing step 4）：充值/消费/退款/授权/过期。

会计口径（订阅模式的前提）：
- 充值(topup) = 预收账款（负债），不是收入
- 消费(consume) 时才转收入 —— 这是权责发生制的落地点
- 退款(refund) = 冲正（上游失败不可免单的对偶面：用户侧要退还）
- 过期(expire) = 负债释放（订阅额度过期作废是一笔真实账务处理）

不变量：wallet_entries 是唯一真源（append-only），wallets 表是快写缓存；
每一笔变动都在同一事务里写流水 + 更新余额（不允许只改其一）。
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.wallet import Wallet, WalletEntry
from app.database import utcnow

# 防止扣成负数：不足时拒绝（调用方转 402/429），不是静默扣成负
INSUFFICIENT = "insufficient_balance"


class InsufficientBalance(Exception):
    """积分余额不足。异常处理器转 402（原生面 detail / 兼容面 OpenAI 格式）。"""


async def _wallet(db: AsyncSession, user_id: UUID) -> Wallet:
    w = await db.get(Wallet, user_id)
    if w is None:
        w = Wallet(user_id=user_id, balance=Decimal("0"))
        db.add(w)
        await db.flush()
    return w


async def _apply(
    db: AsyncSession,
    user_id: UUID,
    entry_type: str,
    amount: Decimal,
    *,
    usage_log_id: UUID | None = None,
    note: str | None = None,
    expires_at: datetime | None = None,
    allow_negative: bool = False,
) -> WalletEntry | None:
    """写流水 + 更新余额（同事务）。amount 正=入账，负=扣减。

    扣减且余额不足 → 不写任何行、不改余额，返回 None（调用方拒绝请求）。
    """
    w = await _wallet(db, user_id)
    if amount < 0 and w.balance + amount < 0 and not allow_negative:
        return None  # 余额不足：调用方据此拒绝，零副作用
    w.balance += amount
    w.updated_at = utcnow()
    entry = WalletEntry(
        user_id=user_id,
        entry_type=entry_type,
        amount=amount,
        balance_after=w.balance,
        usage_log_id=usage_log_id,
        note=note,
        expires_at=expires_at,
    )
    db.add(entry)
    await db.flush()
    return entry


async def topup(db: AsyncSession, user_id: UUID, amount: Decimal, *, note: str | None = None) -> WalletEntry:
    """充值：负债增加（不是收入）。amount 必须为正。"""
    assert amount > 0, "topup amount must be positive"
    w = await _wallet(db, user_id)
    w.lifetime_topup += amount
    return await _apply(db, user_id, "topup", amount, note=note)


async def consume(
    db: AsyncSession, user_id: UUID, amount: Decimal, *, usage_log_id: UUID | None = None,
    note: str | None = None, allow_negative: bool = False,
) -> WalletEntry | None:
    """消费：负债转收入的时点。余额不足返回 None（调用方拒绝请求）。"""
    return await _apply(
        db, user_id, "consume", -amount, usage_log_id=usage_log_id, note=note,
        allow_negative=allow_negative,
    )


async def refund(
    db: AsyncSession, user_id: UUID, amount: Decimal, *, usage_log_id: UUID | None = None,
    note: str | None = None,
) -> WalletEntry:
    """退款：上游失败/争议时冲正 —— 不允许因平台故障扣用户钱。"""
    assert amount > 0, "refund amount must be positive"
    return await _apply(db, user_id, "refund", amount, usage_log_id=usage_log_id, note=note)


async def grant(db: AsyncSession, user_id: UUID, amount: Decimal, *, note: str | None = None) -> WalletEntry:
    """赠送/补偿（运营动作），正数入账。"""
    assert amount > 0, "grant amount must be positive"
    return await _apply(db, user_id, "grant", amount, note=note)


async def expire(db: AsyncSession, user_id: UUID, amount: Decimal, *, note: str | None = None) -> WalletEntry:
    """额度过期：负债释放。余额不足时允许为负的场景不存在（过期额 ≤ 余额），仍走普通扣减。"""
    assert amount > 0, "expire amount must be positive"
    return await _apply(db, user_id, "expire", -amount, note=note)


async def balance(db: AsyncSession, user_id: UUID) -> Decimal:
    w = await db.get(Wallet, user_id)
    return w.balance if w else Decimal("0")


async def entries(db: AsyncSession, user_id: UUID) -> list[WalletEntry]:
    rows = (
        await db.execute(
            select(WalletEntry).where(WalletEntry.user_id == user_id).order_by(WalletEntry.created_at)
        )
    ).scalars().all()
    return list(rows)


async def charge_settled_log(db: AsyncSession, entry) -> WalletEntry | None:
    """把一条已结算账行扣到钱包上（幂等：同 usage_log_id 只扣一次）。

    - 币种不匹配（规则未配 / 兜底 usd）→ 不扣返回 None：计费单位必须与钱包
      单位一致，宁可不扣交给对账，不可扣错单位。
    - 余额不足 → 尽力收回剩余全部（部分支付，余额归零），缺口把账行标
      needs_review 交对账。归零后下一请求在准入侧被 402 拦截，不会无限漏收。
    """
    if entry.cost_billed is None or entry.cost_billed <= 0:
        return None
    if entry.currency != "credit":
        return None
    # 幂等：该账行已扣过则跳过
    existing = (
        await db.execute(select(WalletEntry).where(WalletEntry.usage_log_id == entry.id))
    ).scalars().first()
    if existing is not None:
        return existing
    amount = Decimal(entry.cost_billed)
    insufficient = False
    try:
        # savepoint：唯一约束（uq_wallet_entries_usage_log）冲突只回滚扣款，
        # 不波及外层正在结算的账本行
        async with db.begin_nested():
            e = await consume(
                db, entry.user_id, amount, usage_log_id=entry.id,
                note=f"usage:{entry.model_upstream or entry.model}",
            )
            if e is None:
                # 余额不足：部分支付（扣光剩余），缺口记 needs_review
                insufficient = True
                remain = await balance(db, entry.user_id)
                if remain > 0:
                    e = await consume(
                        db, entry.user_id, remain, usage_log_id=entry.id,
                        note=f"usage:{entry.model_upstream or entry.model}(部分支付)",
                    )
    except IntegrityError:
        # 并发 settle：另一路已扣过 → 复用已有流水（唯一约束保证不双扣）
        existing = (
            await db.execute(select(WalletEntry).where(WalletEntry.usage_log_id == entry.id))
        ).scalars().first()
        if existing is None:
            raise
        return existing
    if insufficient:
        entry.needs_review = True
    return e
