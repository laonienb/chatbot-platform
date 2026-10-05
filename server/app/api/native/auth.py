"""认证：注册 / 登录 / 刷新 / 当前用户。"""

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models import User
from app.schemas.auth import LoginIn, RefreshIn, RegisterIn, TokenOut, UserOut

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _tokens_for(user: User) -> TokenOut:
    return TokenOut(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
        user=UserOut.model_validate(user),
    )


@router.post("/register", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterIn, db: DbSession):
    existing = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "该邮箱已注册")
    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        display_name=body.display_name,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return _tokens_for(user)


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, db: DbSession):
    user = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "邮箱或密码错误")
    if user.status != "active":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已被禁用")
    return _tokens_for(user)


@router.post("/refresh", response_model=TokenOut)
async def refresh(body: RefreshIn, db: DbSession):
    user_id = decode_token(body.refresh_token, "refresh")
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "refresh_token 无效或已过期")
    user = await db.get(User, user_id)
    if user is None or user.status != "active":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已被禁用")
    return _tokens_for(user)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser):
    return user
