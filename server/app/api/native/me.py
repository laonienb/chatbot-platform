"""我的 API Key 管理：创建（明文只返回一次）/ 列表 / 吊销。"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.core.security import generate_api_key
from app.models import ApiKey
from app.schemas.api_key import ApiKeyCreatedOut, ApiKeyCreate, ApiKeyOut

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
