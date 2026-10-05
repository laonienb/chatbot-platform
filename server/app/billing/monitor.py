"""毛利监控（billing step 6）：按次/积分计费的定价错误逃生阀。

为什么必须有：credit_multiplier 或费率配错时，会**静默**按低于成本价卖 ——
账面一切正常，钱在无声地亏。cost_billed < cost_upstream 的行就是定价错误
的直接证据，必须能被查询/告警。这是积分制唯一的「配错了」检测手段，
成本极低但能救命。

币种口径：billed 是 credit、upstream 是 USD，不可直接相减 —— 统一经
settings.credit_to_usd（1 积分折合的 USD，默认 1）换算到 USD 再比。
"""

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import UsageLog


def _revenue_usd(billed) -> Decimal:
    """积分收入换算成 USD（credit_to_usd 为兑换率）。"""
    return Decimal(billed) * get_settings().credit_to_usd


async def count_margin_violations(db: AsyncSession) -> int:
    """已结算账行中「卖价低于上游成本」的条数（>0 即需要人工检查费率）。"""
    rate = get_settings().credit_to_usd
    stmt = select(func.count()).select_from(UsageLog).where(
        UsageLog.status == "settled",
        UsageLog.cost_billed.isnot(None),
        UsageLog.cost_upstream.isnot(None),
        UsageLog.currency == "credit",  # 只比同口径（usd 兜底行不参与）
        UsageLog.cost_billed * rate < UsageLog.cost_upstream,
    )
    return int((await db.execute(stmt)).scalar_one())


async def margin_violations(db: AsyncSession, limit: int = 50) -> list[UsageLog]:
    """违规明细（供管理端举报表 / 对账排查用）。"""
    rate = get_settings().credit_to_usd
    stmt = (
        select(UsageLog)
        .where(
            UsageLog.status == "settled",
            UsageLog.cost_billed.isnot(None),
            UsageLog.cost_upstream.isnot(None),
            UsageLog.currency == "credit",
            UsageLog.cost_billed * rate < UsageLog.cost_upstream,
        )
        .order_by(UsageLog.created_at.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())


async def summarize_margin(db: AsyncSession) -> dict:
    """汇总：收入(billed×credit_to_usd) − 成本(upstream) = 毛利（全 USD 口径）。

    margin_ratio 为负说明定价系统性错误。
    """
    stmt = select(
        func.coalesce(func.sum(UsageLog.cost_billed), 0),
        func.coalesce(func.sum(UsageLog.cost_upstream), 0),
        func.count(),
    ).where(UsageLog.status == "settled", UsageLog.currency == "credit")
    billed, upstream, n = (await db.execute(stmt)).one()
    billed, upstream = Decimal(billed), Decimal(upstream)
    revenue = _revenue_usd(billed)
    margin = revenue - upstream
    return {
        "requests": int(n),
        "billed_total": billed,  # 原始积分
        "revenue_usd": revenue,  # 换算后收入
        "upstream_total": upstream,
        "margin": margin,
        "margin_ratio": (margin / upstream) if upstream else None,
        "violations": await count_margin_violations(db),
    }
