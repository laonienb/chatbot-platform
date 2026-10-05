"""安全组件：密码哈希（bcrypt）、JWT（access/refresh）、API Key 生成与校验。"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import bcrypt
import jwt

from app.config import get_settings

settings = get_settings()

# ---------- 密码 ----------


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


# ---------- JWT ----------


def _create_token(user_id: UUID, token_type: str, expires_delta: timedelta) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: UUID) -> str:
    return _create_token(user_id, "access", timedelta(minutes=settings.access_token_expire_minutes))


def create_refresh_token(user_id: UUID) -> str:
    return _create_token(user_id, "refresh", timedelta(days=settings.refresh_token_expire_days))


def decode_token(token: str, expected_type: str) -> UUID | None:
    """校验签名/过期/类型，失败返回 None（不抛异常，由调用方决定 401/403）。"""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
    if payload.get("type") != expected_type:
        return None
    try:
        return UUID(payload.get("sub", ""))
    except ValueError:
        return None


# ---------- API Key ----------

API_KEY_PREFIX = "sk-"


def generate_api_key() -> tuple[str, str, str]:
    """返回 (明文 key, sha256 哈希, 展示前缀)。明文只在创建时给一次。"""
    raw = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return raw, hash_api_key(raw), raw[:10] + "…"


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
