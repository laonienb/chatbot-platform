"""钱包操作 + 毛利监控测试（billing step 4+6）。"""

from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from app.billing.charge import balance, consume, entries, grant, refund, topup
from app.billing.monitor import count_margin_violations, summarize_margin
from app.billing.wallet import WalletEntry


async def _user_id(client, auth_headers) -> str:
    me = (await client.get("/api/v1/auth/me", headers=auth_headers)).json()
    return me["id"]


async def test_wallet_topup_consume_refund(db_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.database import Base  # noqa: F401
    from app.models import User
    from uuid import uuid4 as _uuid4

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        uid = _uuid4()
        s.add(User(id=uid, email="w@t.dev", password_hash="x"))
        await s.commit()

        # 充值 100 → 消费 30 → 余额 70
        await topup(s, uid, Decimal("100"))
        await consume(s, uid, Decimal("30"))
        assert await balance(s, uid) == Decimal("70")

        # 余额不足 → 拒绝且零副作用
        result = await consume(s, uid, Decimal("1000"))
        assert result is None
        assert await balance(s, uid) == Decimal("70")  # 没被扣成负数

        # 退款 +80 → 150；赠送 +10 → 160
        await refund(s, uid, Decimal("80"), usage_log_id=None)
        await grant(s, uid, Decimal("10"))
        assert await balance(s, uid) == Decimal("160")

        # 流水完整且余额链一致（append-only 真源）
        es = await entries(s, uid)
        assert [e.entry_type for e in es] == ["topup", "consume", "refund", "grant"]
        assert es[-1].balance_after == Decimal("160")
        # 每一笔的 balance_after 都等于前一笔 + amount（可对账）
        running = Decimal("0")
        for e in es:
            running += e.amount
            assert e.balance_after == running


async def test_settle_auto_charges_wallet_and_charge_is_idempotent(client, auth_headers, persona, db_engine):
    """结算即扣钱包（settle_usage 内），且同一条账行重复扣只扣一次。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from uuid import UUID

    from app.billing.charge import charge_settled_log
    from app.config import get_settings
    from app.models import UsageLog

    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "hi"}, headers=auth_headers
    )

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        log = (await s.execute(select(UsageLog))).scalars().first()
        assert log is not None and log.status == "settled"
        uid = UUID(log.user_id) if isinstance(log.user_id, str) else log.user_id

        # 结算时已自动扣费：注册赠送 - cost_billed
        grant = get_settings().signup_grant_credits
        bal_after_msg = await balance(s, uid)
        assert bal_after_msg == grant - Decimal(log.cost_billed)

        # 流水里已有这笔扣费（usage_log_id 关联到账行）
        es = await entries(s, uid)
        consumes = [e for e in es if e.entry_type == "consume"]
        assert len(consumes) == 1 and consumes[0].usage_log_id == log.id

        # 手动重复扣 → 幂等复用，不产生第二笔
        e2 = await charge_settled_log(s, log)
        assert e2 is not None and e2.id == consumes[0].id
        assert await balance(s, uid) == bal_after_msg


async def test_register_grants_signup_credits(client, db_engine):
    """注册即发放注册赠送积分（settings.signup_grant_credits）—— 余额准入的启动资金。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from uuid import UUID

    from app.billing.charge import balance
    from app.config import get_settings

    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "grant@test.dev", "password": "password123"},
    )
    assert resp.status_code == 201
    uid = UUID(resp.json()["user"]["id"])

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        assert await balance(s, uid) == get_settings().signup_grant_credits


async def test_margin_monitor(client, auth_headers, persona, db_engine):
    """毛利监控：正常费率下 violations=0；配错费率（负毛利）必须被发现。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from uuid import UUID

    from app.models import UsageLog

    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "hi"}, headers=auth_headers
    )

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        # 正常情况：平台默认费率（0.001/token）应高于 mock 的 upstream 成本
        summary = await summarize_margin(s)
        assert summary["requests"] >= 1
        assert summary["billed_total"] > 0
        assert summary["violations"] == 0, "默认费率下不应出现负毛利"

        # 模拟费率配错：把 billed 改成低于 upstream → 必须被检出
        log = (await s.execute(select(UsageLog))).scalars().first()
        log.cost_billed = Decimal("0.0000001")  # 远低于 upstream
        await s.commit()
        assert await count_margin_violations(s) == 1
        summary2 = await summarize_margin(s)
        assert summary2["violations"] == 1
        assert summary2["margin"] < summary["margin"]  # 毛利下降可见


async def test_insufficient_balance_402_blocks_message(client, auth_headers, persona, db_engine):
    """余额扣光 → 准入 402，且零副作用（不落账、不产生消息）。"""
    from sqlalchemy import func
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from uuid import UUID

    from app.billing.charge import balance, consume
    from app.models import UsageLog

    me = (await client.get("/api/v1/auth/me", headers=auth_headers)).json()
    uid = UUID(me["id"])

    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()

    # 把注册赠送全部花光
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        bal = await balance(s, uid)
        assert bal > 0
        await consume(s, uid, bal)
        await s.commit()
        assert await balance(s, uid) == 0

    resp = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "hi"}, headers=auth_headers
    )
    assert resp.status_code == 402
    assert "余额" in str(resp.json())

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        n = (await s.execute(select(func.count()).select_from(UsageLog))).scalar_one()
    assert n == 0  # 准入零副作用：没有预扣行


async def test_admin_margin_endpoint(client, auth_headers, persona, db_engine):
    """毛利监控管理端：非管理员 403；管理员可查汇总与负毛利明细。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from uuid import UUID

    from app.models import User

    url = "/api/v1/admin/billing/margin"

    # 非管理员 → 403
    assert (await client.get(url, headers=auth_headers)).status_code == 403

    # 提权为管理员
    from sqlalchemy import update

    me = (await client.get("/api/v1/auth/me", headers=auth_headers)).json()
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        await s.execute(update(User).where(User.id == UUID(me["id"])).values(role="admin"))
        await s.commit()

    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "hi"}, headers=auth_headers
    )

    r = await client.get(url, headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["requests"] >= 1
    assert data["violations"] == 0  # 默认费率下无负毛利
    assert data["violations_detail"] == []

    # 手动对账入口可调用
    rc = await client.post("/api/v1/admin/billing/reconcile", headers=auth_headers)
    assert rc.status_code == 200 and rc.json()["abandoned"] == 0


async def test_margin_uses_credit_to_usd_rate(client, auth_headers, db_engine, monkeypatch):
    """毛利比较必须经 credit_to_usd 换算 —— 默认值 1 会掩盖这个 bug。

    billed 是 credit、upstream 是 USD，直接相减在兑换率非 1 时结论会反向。
    构造一行「按 USD 直比是亏、按真实汇率是赚」的账：监控必须按汇率判为正常。
    """
    from uuid import UUID

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.config import get_settings
    from app.models import UsageLog

    settings = get_settings()
    # 1 积分 = 0.5 USD（远低于默认 1，专门暴露「忘了换算」的实现）
    monkeypatch.setattr(settings, "credit_to_usd", Decimal("0.5"))

    me = (await client.get("/api/v1/auth/me", headers=auth_headers)).json()
    uid = UUID(me["id"])

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        s.add(
            UsageLog(
                user_id=uid,
                model="gpt-4o-mini",
                status="settled",
                currency="credit",
                cost_billed=Decimal("100"),  # 100 积分 × 0.5 = 50 USD 收入
                cost_upstream=Decimal("30"),  # 成本 30 USD → 实际赚 20
                prompt_tokens=10,
                completion_tokens=5,
                idempotency_key="margin-rate-test",
            )
        )
        await s.commit()

        # 按汇率：50 > 30，无违规。若实现忘了乘 rate（100 > 30 也成立，看不出）
        # 或漏乘后判断出错，这里会失败。
        summary = await summarize_margin(s)
        assert summary["billed_total"] == Decimal("100")  # 原始积分不改
        assert summary["revenue_usd"] == Decimal("50")  # 换算后收入
        assert summary["upstream_total"] == Decimal("30")
        assert summary["margin"] == Decimal("20"), summary
        assert summary["violations"] == 0, "换算后应无负毛利"
        assert await count_margin_violations(s) == 0

        # 反证：同一行在默认汇率（1.0）下确实会被判为违规 —— 证明
        # 上面的「无违规」是汇率生效的结果，而不是这行数据本来就无害。
        monkeypatch.setattr(settings, "credit_to_usd", Decimal("0.1"))  # 100 × 0.1 = 10 < 30
        assert await count_margin_violations(s) == 1, "低汇率下应收紧为违规"


async def test_charge_partial_payment_when_balance_runs_out(client, auth_headers, db_engine):
    """结算时余额不足：扣光剩余（部分支付）+ 账行标 needs_review，余额不为负。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from uuid import UUID, uuid4

    from app.billing.charge import charge_settled_log, consume, topup
    from app.models import UsageLog, User

    me = (await client.get("/api/v1/auth/me", headers=auth_headers)).json()
    uid = UUID(me["id"])

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        # 清空注册赠送，只留 1 积分
        bal = await balance(s, uid)
        await consume(s, uid, bal)
        await topup(s, uid, Decimal("1"))

        log = UsageLog(
            user_id=uid,
            model="gpt-4o-mini",
            status="settled",
            currency="credit",
            cost_billed=Decimal("5"),  # 应扣 5，只有 1
            prompt_tokens=100,
            completion_tokens=50,
            idempotency_key=str(uuid4()),
        )
        s.add(log)
        await s.commit()

        e = await charge_settled_log(s, log)
        assert e is not None
        assert e.amount == Decimal("-1")  # 扣光剩余，不是 5
        assert await balance(s, uid) == 0  # 归零，绝不为负
        assert log.needs_review is True  # 缺口交对账

        # 幂等：再调不重复扣
        e2 = await charge_settled_log(s, log)
        assert e2 is not None and e2.id == e.id
        assert await balance(s, uid) == 0


async def test_me_wallet_endpoint(client, auth_headers, persona, db_engine):
    """需求单验收：登录用户可查余额；注册赠送可见；结算即扣后下降；未登录 401。"""
    from app.config import get_settings

    grant = float(get_settings().signup_grant_credits)

    # 未登录 → 401（沿用 HTTPBearer 鉴权，无新鉴权行为）
    assert (await client.get("/api/v1/me/wallet")).status_code == 401

    r = await client.get("/api/v1/me/wallet", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert set(data) == {"balance", "lifetime_topup"}
    assert data["balance"] == grant
    # lifetime_topup 只统计充值(topup)，注册赠送(grant)按会计口径不算充值
    assert data["lifetime_topup"] == 0.0

    # 充值后 lifetime_topup 增加、balance 同步
    from decimal import Decimal
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.billing.charge import topup
    from app.models import User
    from uuid import UUID

    uid = UUID((await client.get("/api/v1/auth/me", headers=auth_headers)).json()["id"])
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        await topup(s, uid, Decimal("50"))
        await s.commit()
    topped = (await client.get("/api/v1/me/wallet", headers=auth_headers)).json()
    assert topped["lifetime_topup"] == 50.0
    assert topped["balance"] == grant + 50.0

    # 对话一次（结算即扣）→ 余额下降、累计入账不变
    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    resp = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "hi"}, headers=auth_headers
    )
    assert resp.status_code == 200
    after = (await client.get("/api/v1/me/wallet", headers=auth_headers)).json()
    assert after["balance"] < grant + 50.0  # 结算即扣
    assert after["lifetime_topup"] == 50.0  # 消费不减少累计充值
