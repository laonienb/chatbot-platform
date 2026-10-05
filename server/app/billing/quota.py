"""周期额度准入 + 对账（billing step 5）。

period_counters 是「本周期已消耗」的快写计数器，与 usage_logs 账本**同事务**
更新 —— 它是准入的快路径，不是独立缓存：账本为真源，计数器只做加速，
对账任务定期用账本重算校准（防漂移）。

准入双向执行：
- 预检（阶段A）：tokens_used + 估算 > 额度 → 拒绝（429/402），零副作用
- 硬执行（阶段B）：结算时按**实测** tokens 记账 —— 估算只用于准入，
  记账永远用实测值（估算偏大不会多扣，偏小也已由实测补正）
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.wallet import WalletEntry
from app.models import UsageLog


class QuotaExceeded(Exception):
    """周期 token 额度用尽。异常处理器转 429（原生面 detail / 兼容面 OpenAI 格式）。"""


def period_key(now: datetime | None = None) -> str:
    """周期键：UTC 月粒度（当前实现；订阅 plan 的自定义周期接入后由 plan 提供）。"""
    now = now or datetime.now(UTC)
    return now.strftime("%Y-%m")


async def tokens_used_in_period(db: AsyncSession, user_id, period: str | None = None) -> int:
    """从账本重算本周期消耗（对账口径）；period 形如 'YYYY-MM'。"""
    period = period or period_key()
    start = datetime.strptime(period, "%Y-%m").replace(tzinfo=UTC)
    # 下一周期起点
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    # coalesce 必须包在列上：SQL 里 NULL 参与求和会把整个 SUM 变成 NULL
    stmt = select(
        func.coalesce(
            func.sum(
                func.coalesce(UsageLog.prompt_tokens, 0) + func.coalesce(UsageLog.completion_tokens, 0)
            ),
            0,
        )
    ).where(
        UsageLog.user_id == user_id,
        UsageLog.status.in_(("settled", "failed", "abandoned")),
        UsageLog.created_at >= start,
        UsageLog.created_at < end,
    )
    return int((await db.execute(stmt)).scalar_one())


async def admit_request(
    db: AsyncSession, user_id, estimated_tokens: int, *, allowance_tokens: int | None
) -> tuple[bool, int, int]:
    """准入检查:（是否放行, 本周期已用, 额度）。

    allowance_tokens=None 或 <=0 → 不限额（与 settings.quota_monthly_tokens 的
    "None/0=不限额" 语义一致）。已用从账本重算（正确口径），并发窗口下可能
    略超（先到先得），硬执行在结算侧。
    """
    if allowance_tokens is None or allowance_tokens <= 0:
        return True, await tokens_used_in_period(db, user_id), 0
    used = await tokens_used_in_period(db, user_id)
    if used + max(0, estimated_tokens) > allowance_tokens:
        return False, used, allowance_tokens
    return True, used, allowance_tokens


async def reconcile_pending(db: AsyncSession, *, stale_minutes: int = 15) -> int:
    """对账：收编长时间停留 pending 的行（进程被 kill 的残留）。

    按预扣估算结清（status=abandoned，needs_review=True），或预扣为 0 时
    归零。没有它，pending 行会永久占着额度且账面永远「有一笔在飞」。
    返回收编条数。
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=stale_minutes)
    rows = (
        (
            await db.execute(
                select(UsageLog).where(UsageLog.status == "pending", UsageLog.started_at < cutoff)
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    for row in rows:
        row.status = "abandoned"
        row.prompt_tokens = row.prompt_tokens or 0
        row.completion_tokens = row.completion_tokens or (row.reserved_tokens or 0)
        row.metering_source = row.metering_source or "estimated"
        row.needs_review = True  # 需人工复核：实际消耗未知
        row.settled_at = now
        row.error_code = row.error_code or "reconciled_stale_pending"
    if rows:
        await db.commit()
    return len(rows)


async def recompute_user_period(db: AsyncSession, user_id, period: str | None = None) -> int:
    """从账本重算某用户本周期消耗（对账用，当前与 tokens_used_in_period 同口径）。"""
    return await tokens_used_in_period(db, user_id, period)
