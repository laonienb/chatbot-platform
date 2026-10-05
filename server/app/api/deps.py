"""共享依赖：JWT 用户鉴权（原生 API）与 API Key 鉴权（兼容层）。"""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token, hash_api_key
from app.database import get_db
from app.models import ApiKey, User

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少 Bearer 凭证", headers={"WWW-Authenticate": "Bearer"})
    user_id = decode_token(credentials.credentials, "access")
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token 无效或已过期", headers={"WWW-Authenticate": "Bearer"})
    user = await db.get(User, user_id)
    if user is None or user.status != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已被禁用")
    request.state.user = user  # 供限流依赖读取（不重复查库）
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


async def get_admin_user(user: CurrentUser) -> User:
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return user


AdminUser = Annotated[User, Depends(get_admin_user)]


async def get_api_key_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> tuple[ApiKey, User]:
    """兼容层鉴权：Bearer sk-xxx → (api_key, user)。校验吊销与过期。"""
    from datetime import UTC, datetime

    if credentials is None or not credentials.credentials.startswith("sk-"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少有效的 API Key")

    key_hash = hash_api_key(credentials.credentials)
    api_key = (
        await db.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
    ).scalar_one_or_none()
    if api_key is None or api_key.revoked:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API Key 无效或已吊销")
    if api_key.expires_at is not None:
        expires_at = api_key.expires_at
        if expires_at.tzinfo is None:  # SQLite 返回 naive datetime，统一补 UTC 再比较
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at < datetime.now(UTC):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API Key 已过期")

    user = await db.get(User, api_key.user_id)
    if user is None or user.status != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "API Key 所属用户不可用")

    api_key.last_used_at = datetime.now(UTC)
    await db.commit()
    request.state.principal = (api_key, user)  # 供限流依赖读取
    return api_key, user
