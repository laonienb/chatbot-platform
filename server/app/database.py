from collections.abc import AsyncGenerator
from datetime import UTC, datetime
import json

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


def utcnow() -> datetime:
    """微秒级 UTC 时间。

    SQLite 的 CURRENT_TIMESTAMP 仅秒级精度，同秒多条消息按时间排序会不稳定，
    因此时间戳统一用 Python 侧默认值而非 server_default。
    """
    return datetime.now(UTC)


def _json_serializer(value) -> str:
    # 默认 json.dumps 会把中文转义成 \uXXXX，导致 JSON 列无法用文本匹配（如市场标签筛选）
    return json.dumps(value, ensure_ascii=False)


class Base(DeclarativeBase):
    pass


settings = get_settings()

# SQLite 需要 check_same_thread=False 以配合测试内的连接复用；PostgreSQL 无此参数
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_async_engine(
    settings.database_url,
    echo=settings.debug,
    connect_args=connect_args,
    json_serializer=_json_serializer,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：每请求一个会话。"""
    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
