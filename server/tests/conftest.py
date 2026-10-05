"""测试夹具：每个测试一个全新的内存 SQLite + mock LLM 后端。

通过覆写 get_db 依赖注入测试引擎，应用代码（服务层/路由）零改动。
"""

import os

# 必须在导入 app 之前设置：mock LLM 无需真实 Key，隔离的 JWT 密钥
os.environ.setdefault("LLM_BACKEND", "mock")
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite://")  # 仅占位，实际引擎在 fixture 里创建

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base, _json_serializer, get_db
from app.main import app as fastapi_app
import app.models  # noqa: F401


@pytest.fixture
async def db_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        json_serializer=_json_serializer,  # 与应用引擎一致：中文不转义，市场标签过滤才可用
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def client(db_engine):
    session_maker = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db():
        async with session_maker() as session:
            yield session

    fastapi_app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    fastapi_app.dependency_overrides.clear()


@pytest.fixture
async def user_tokens(client):
    """注册一个用户，返回 token 结构。"""
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "alice@test.dev", "password": "password123", "display_name": "Alice"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
def auth_headers(user_tokens):
    return {"Authorization": f"Bearer {user_tokens['access_token']}"}


@pytest.fixture
async def persona(client, auth_headers):
    """创建一个示例人设。"""
    resp = await client.post(
        "/api/v1/personas",
        json={
            "name": "李白",
            "slug": "libai",
            "system_prompt": "你是诗仙李白，说话豪放洒脱。",
            "opening_message": "君不见黄河之水天上来！有何吩咐？",
            "temperature": 0.8,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()
