"""限流 + 额度准入测试（billing step 5+6）。"""

from uuid import UUID

from sqlalchemy import update

from app.billing.quota import admit_request, reconcile_pending, tokens_used_in_period
from app.billing.ratelimit import limiter


async def _conv(client, auth_headers, persona):
    return (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()


async def test_rate_limit_429_with_retry_after(client, auth_headers, persona, monkeypatch):
    """超 RPM → 429 + Retry-After（OpenAI 生态工具按此退避）。"""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "rpm_default", 2)  # 每分钟最多 2 次

    conv = await _conv(client, auth_headers, persona)
    url = f"/api/v1/conversations/{conv['id']}/messages"

    r1 = await client.post(url, json={"content": "a"}, headers=auth_headers)
    r2 = await client.post(url, json={"content": "b"}, headers=auth_headers)
    assert r1.status_code == 200
    assert r2.status_code == 200

    r3 = await client.post(url, json={"content": "c"}, headers=auth_headers)
    assert r3.status_code == 429
    assert "Retry-After" in r3.headers
    assert int(r3.headers["Retry-After"]) >= 1
    # OpenAI 风格错误体（/v1 面）；原生面 detail 格式
    assert "rate limit" in str(r3.json()).lower()


async def test_rate_limit_resets_between_tests(client, auth_headers):
    """conftest 每测重置限流器 —— 上一测试的计数不泄漏。"""
    # 直接断言 limiter 是干净的（fixture 已 reset）
    allowed, _ = limiter.check("probe-key", 1, 60)
    assert allowed
    allowed2, retry = limiter.check("probe-key", 1, 60)
    assert not allowed2 and retry >= 1  # 同 key 第二次超限
    limiter.reset()  # 不影响后续


async def test_api_key_rpm_limit_respected(client, auth_headers, db_engine):
    """API Key 级 rpm_limit 覆盖全局默认。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models import ApiKey

    # 建 key 并设 rpm_limit=1
    resp = await client.post("/api/v1/me/keys", json={"name": "k"}, headers=auth_headers)
    key = resp.json()["key"]
    key_id = resp.json()["id"]

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        await s.execute(update(ApiKey).where(ApiKey.id == UUID(key_id)).values(rpm_limit=1))
        await s.commit()

    hdr = {"Authorization": f"Bearer {key}"}
    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    r1 = await client.post("/v1/chat/completions", json=body, headers=hdr)
    r2 = await client.post("/v1/chat/completions", json=body, headers=hdr)
    assert r1.status_code == 200
    assert r2.status_code == 429  # rpm_limit=1 生效


async def test_admit_request_blocks_over_quota(client, auth_headers, db_engine):
    """额度准入：已用 + 估算 > 额度 → 拒绝；未配额度 → 放行。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from uuid import UUID

    me = (await client.get("/api/v1/auth/me", headers=auth_headers)).json()
    uid = UUID(me["id"])

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        # 无额度限制 → 放行
        ok, used, allowance = await admit_request(s, uid, 100, allowance_tokens=None)
        assert ok is True

        # allowance=0 同样是「不限额」（settings 语义 None/0=不限，不能反向全量 429）
        ok, *_ = await admit_request(s, uid, 100, allowance_tokens=0)
        assert ok is True

        # 额度 50，已用 0，估算 100 → 拒绝（预检）
        ok, used, allowance = await admit_request(s, uid, 100, allowance_tokens=50)
        assert ok is False
        assert used == 0 and allowance == 50

        # 额度 1000，估算 100 → 放行
        ok, *_ = await admit_request(s, uid, 100, allowance_tokens=1000)
        assert ok is True

        # 额度 0 = 不限额（与 settings 的 None/0 语义一致）→ 必须放行。
        # 回归：若只判 `is None`，配 0 会变成 used + est > 0 恒真 —— 全量 429。
        for zero in (0, -1):
            ok, used, allowance = await admit_request(s, uid, 10**9, allowance_tokens=zero)
            assert ok is True, f"额度 {zero} 应视为不限额，实为拒绝"
            assert allowance == 0


async def test_quota_zero_allows_message(client, auth_headers, persona, monkeypatch):
    """端到端：quota_monthly_tokens=0 时对话正常（不是全量 429）。"""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "quota_monthly_tokens", 0)

    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    resp = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"content": "quota 配 0 也该放行"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text


async def test_reconcile_pending_abandoned(client, auth_headers, persona, db_engine, monkeypatch):
    """对账任务：残留 pending 行被收编（needs_review，不永久占额度）。"""
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal
    from uuid import uuid4

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models import UsageLog, User

    me = (await client.get("/api/v1/auth/me", headers=auth_headers)).json()

    # 造一条 20 分钟前的残留 pending（模拟进程被 kill）
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        stale = UsageLog(
            user_id=UUID(me["id"]),
            model="gpt-4o-mini",
            status="pending",
            reserved_tokens=500,
            reserved_cost=Decimal("0.5"),
            started_at=datetime.now(UTC) - timedelta(minutes=20),
        )
        s.add(stale)
        # 再造一条刚创建的 pending（不应被收编）
        fresh = UsageLog(
            user_id=UUID(me["id"]),
            model="gpt-4o-mini",
            status="pending",
            reserved_tokens=100,
            started_at=datetime.now(UTC),
        )
        s.add(fresh)
        await s.commit()

        n = await reconcile_pending(s, stale_minutes=15)
        assert n == 1  # 只收编 stale

        from sqlalchemy import select

        rows = (await s.execute(select(UsageLog))).scalars().all()
        stale_row = next(r for r in rows if r.reserved_tokens == 500)
        fresh_row = next(r for r in rows if r.reserved_tokens == 100)
        assert stale_row.status == "abandoned"
        assert stale_row.needs_review is True  # 实际消耗未知，必须人工复核
        assert stale_row.error_code == "reconciled_stale_pending"
        assert stale_row.settled_at is not None
        assert fresh_row.status == "pending"  # 新 pending 不动


async def test_tokens_used_in_period_from_ledger(client, auth_headers, persona, db_engine):
    """周期消耗从账本重算（对账口径），不是独立缓存。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from uuid import UUID

    me = (await client.get("/api/v1/auth/me", headers=auth_headers)).json()
    uid = UUID(me["id"])

    conv = await _conv(client, auth_headers, persona)
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "消耗一点"}, headers=auth_headers
    )

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        used = await tokens_used_in_period(s, uid)
    assert used > 0  # 刚对话过，账本里有实测 tokens


async def test_monthly_quota_exceeded_429_native_and_compat(client, auth_headers, persona, db_engine, monkeypatch):
    """额度超限：原生面 429、兼容面 OpenAI 格式 429，且零副作用（不落预扣行）。"""
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.config import get_settings
    from app.models import UsageLog

    monkeypatch.setattr(get_settings(), "quota_monthly_tokens", 1)  # 估算必然 > 1

    conv = await _conv(client, auth_headers, persona)
    resp = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "hi"}, headers=auth_headers
    )
    assert resp.status_code == 429
    assert "额度" in str(resp.json())

    # 兼容面：OpenAI 错误格式
    key = (await client.post("/api/v1/me/keys", json={"name": "k"}, headers=auth_headers)).json()["key"]
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 429
    assert r.json()["error"]["type"] == "insufficient_quota"

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        n = (await s.execute(select(func.count()).select_from(UsageLog))).scalar_one()
    assert n == 0  # 准入零副作用


async def test_billing_watch_tick_reconciles_and_summarizes(client, auth_headers, db_engine):
    """对账循环一轮（billing_watch_tick）：收编残留 pending + 返回毛利摘要。"""
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal
    from uuid import UUID

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.main import billing_watch_tick
    from app.models import UsageLog

    me = (await client.get("/api/v1/auth/me", headers=auth_headers)).json()

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        s.add(
            UsageLog(
                user_id=UUID(me["id"]),
                model="gpt-4o-mini",
                status="pending",
                reserved_tokens=500,
                reserved_cost=Decimal("0.5"),
                started_at=datetime.now(UTC) - timedelta(minutes=20),
            )
        )
        await s.commit()

        result = await billing_watch_tick(s)

    assert result["abandoned"] == 1  # stale pending 被收编
    assert "violations" in result and "revenue_usd" in result  # 毛利摘要可用
