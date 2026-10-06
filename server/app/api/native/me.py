"""我的 API Key 管理（创建/列表/吊销）、用量统计与钱包余额。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession
from app.billing.wallet import Wallet
from app.core.security import generate_api_key
from app.models import ApiKey, UsageLog
from app.schemas.api_key import ApiKeyCreatedOut, ApiKeyCreate, ApiKeyOut
from app.schemas.usage import UsageByModel, UsageOut, WalletOut

router = APIRouter(prefix="/api/v1/me", tags=["me"])


@router.post("/keys", response_model=ApiKeyCreatedOut, status_code=status.HTTP_201_CREATED)
async def create_api_key(body: ApiKeyCreate, user: CurrentUser, db: DbSession):
    raw_key, key_hash, key_prefix = generate_api_key()
    expires_at = None
    if body.expires_at:
        try:
            expires_at = datetime.fromisoformat(body.expires_at)
        except ValueError:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "expires_at 需要 ISO 8601 格式")
        if expires_at.tzinfo is None:  # naive 时间按 UTC 处理，避免与 aware 比较报错
            expires_at = expires_at.replace(tzinfo=UTC)
    api_key = ApiKey(
        user_id=user.id,
        name=body.name,
        key_hash=key_hash,
        key_prefix=key_prefix,
        model_whitelist=body.model_whitelist,
        expires_at=expires_at,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)
    out = ApiKeyCreatedOut.model_validate(api_key)
    out.key = raw_key
    return out


@router.get("/keys", response_model=list[ApiKeyOut])
async def list_api_keys(user: CurrentUser, db: DbSession):
    stmt = (
        select(ApiKey)
        .where(ApiKey.user_id == user.id, ApiKey.revoked == False)  # noqa: E712
        .order_by(ApiKey.created_at.desc())
    )
    return (await db.execute(stmt)).scalars().all()


@router.delete("/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(key_id: UUID, user: CurrentUser, db: DbSession):
    api_key = await db.get(ApiKey, key_id)
    if api_key is None or api_key.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API Key 不存在")
    api_key.revoked = True
    await db.commit()


@router.get("/usage", response_model=UsageOut)
async def my_usage(user: CurrentUser, db: DbSession, days: int = 30):
    """我的 token 消耗（按模型分组），统计窗口 days 天（1-365，默认 30）。

    只统计**终态**账行（settled/failed/abandoned），与配额口径（`quota.py`）一致：
    pending 是「已预扣但尚未结算」的在飞行，把它算进来会让用量在本请求尚未完成时
    就虚增（并发下更明显）；abandoned 是进程被 kill 后由对账收编的残留，按预扣
    估算计入（保守）。
    """
    days = min(max(days, 1), 365)
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (
        await db.execute(
            select(
                UsageLog.model,
                func.count().label("requests"),
                func.coalesce(func.sum(UsageLog.prompt_tokens), 0).label("prompt_tokens"),
                func.coalesce(func.sum(UsageLog.completion_tokens), 0).label("completion_tokens"),
            )
            .where(
                UsageLog.user_id == user.id,
                UsageLog.created_at >= since,
                UsageLog.status.in_(("settled", "failed", "abandoned")),
            )
            .group_by(UsageLog.model)
            .order_by(func.sum(UsageLog.prompt_tokens + UsageLog.completion_tokens).desc())
        )
    ).all()
    by_model = [UsageByModel(model=r.model, requests=r.requests, prompt_tokens=r.prompt_tokens, completion_tokens=r.completion_tokens) for r in rows]
    return UsageOut(
        days=days,
        total_requests=sum(m.requests for m in by_model),
        total_prompt_tokens=sum(m.prompt_tokens for m in by_model),
        total_completion_tokens=sum(m.completion_tokens for m in by_model),
        by_model=by_model,
    )


@router.get("/wallet", response_model=WalletOut)
async def my_wallet(user: CurrentUser, db: DbSession):
    """我的积分余额（需求单：402 拒绝后前端据此引导充值）。

    结算即扣的真实余额，读 wallets 快写缓存（真源 wallet_entries 同事务维护）。
    从未充值/未发放（如 signup_grant_credits=0 且无流水）的用户视为 0。
    """
    w = await db.get(Wallet, user.id)
    return WalletOut(
        balance=float(w.balance) if w else 0.0,
        lifetime_topup=float(w.lifetime_topup) if w else 0.0,
    )
