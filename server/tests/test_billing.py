"""计费/计量测试（billing step 0-3）：计价兜底、账本双阶段、幂等、归因、费率。"""

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.billing.pricing import normalize_model_string, quote_upstream_cost
from app.billing.rating import RatingRule, compute_billed, rule_snapshot
from app.models import UsageLog


# ---------- step 0：计价永不抛异常 + provider 归一 ----------


def test_normalize_model_string():
    assert normalize_model_string("deepseek-chat") == "deepseek/deepseek-chat"
    assert normalize_model_string("gpt-4o-mini") == "openai/gpt-4o-mini"
    assert normalize_model_string("openai/gpt-4o") == "openai/gpt-4o"
    assert normalize_model_string("weird-unknown") is None  # 不猜 → needs_review
    assert normalize_model_string(None) is None


def test_quote_never_raises():
    """计价红线：任何输入都不抛异常（token 已消耗后定价失败不能拖挂请求）。"""
    for m in ["", None, "persona:unresolved", "unknown-vendor/model", "deepseek-chat"]:
        q = quote_upstream_cost(m, prompt_tokens=100, completion_tokens=50)
        assert isinstance(q.upstream, Decimal)
    # 负数/极端值也不抛
    q = quote_upstream_cost("gpt-4o-mini", prompt_tokens=-5, completion_tokens=10**9)
    assert isinstance(q.upstream, Decimal)


def test_quote_unknown_model_needs_review():
    q = quote_upstream_cost("totally-unknown-thing", prompt_tokens=10, completion_tokens=10)
    assert q.needs_review is True
    assert q.upstream == Decimal("0")


def test_quote_persona_prefix_short_circuits():
    """未解析的 persona:<slug> 直接标 needs_review，绝不送去问 litellm。

    litellm 会把 "persona" 当 provider 名抛 BadRequestError 并内部重试，实测每次
    约 1.9s —— 未解析就到计价层是调用方 bug，结论一样但要零成本。
    """
    import time

    t = time.perf_counter()
    q = quote_upstream_cost("persona:unresolved", prompt_tokens=100, completion_tokens=50)
    elapsed = time.perf_counter() - t
    assert q.needs_review is True
    assert q.upstream == Decimal("0")
    assert elapsed < 0.5, f"不应触发 litellm 查询，实耗 {elapsed:.2f}s"


def test_quote_unknown_provider_prefix_short_circuits():
    """带未知 provider 前缀的模型同样短路（前缀白名单取自 litellm 自身）。"""
    import time

    t = time.perf_counter()
    q = quote_upstream_cost("unknown-vendor/model", prompt_tokens=100, completion_tokens=50)
    elapsed = time.perf_counter() - t
    assert q.needs_review is True
    assert q.upstream == Decimal("0")
    assert elapsed < 0.5, f"不应触发 litellm 查询，实耗 {elapsed:.2f}s"


def test_quote_real_provider_prefix_still_priced():
    """白名单不能误杀真 provider：openai/gpt-4o 仍须算出非零价。"""
    q = quote_upstream_cost("openai/gpt-4o", prompt_tokens=1000, completion_tokens=500)
    assert q.needs_review is False
    assert q.upstream > 0


def test_quote_bare_deepseek_priceable():
    """实测修复点：裸 deepseek-chat 在 litellm 里会抛 BadRequestError —— 归一后必须能计价。"""
    q = quote_upstream_cost("deepseek-chat", prompt_tokens=1000, completion_tokens=500)
    assert q.needs_review is False
    assert q.upstream > 0


def test_quote_cache_discount_applied():
    """缓存读必须打折（人设 system prompt 每轮重复必然命中缓存）。"""
    full = quote_upstream_cost("deepseek-chat", prompt_tokens=1000, completion_tokens=0)
    cached = quote_upstream_cost("deepseek-chat", prompt_tokens=1000, completion_tokens=0, cache_read_tokens=1000)
    assert cached.upstream < full.upstream


def test_compat_idem_key_has_time_window(monkeypatch):
    """兼容层自动幂等键带 10 分钟窗：窗内同 body 相同（重发不双扣），
    窗外不同（合法重复请求照常计费，不给永久免单开口子）。"""
    import time as _time
    from uuid import uuid4

    from app.models import ApiKey
    from app.services.chat import _compat_idem_key

    key = ApiKey(id=uuid4(), user_id=uuid4(), name="k")
    msgs = [{"role": "user", "content": "hi"}]

    monkeypatch.setattr(_time, "time", lambda: 1_000_000.0)
    k1 = _compat_idem_key(key, "gpt-4o", msgs)
    k2 = _compat_idem_key(key, "gpt-4o", msgs)
    assert k1 == k2  # 同窗同 body → 同键

    monkeypatch.setattr(_time, "time", lambda: 1_000_000.0 + 601)  # 跨过一个窗
    k3 = _compat_idem_key(key, "gpt-4o", msgs)
    assert k3 != k1  # 窗外同 body → 新键（正常计费）


# ---------- step 3：费率规则 ----------


def _snap(**over):
    base = {
        "charge_dimension": "per_token",
        "unit": "credit",
        "base_amount": "0",
        "rate_input": "0.001",
        "rate_output": "0.002",
        "rate_reasoning": "0.002",
        "rate_cache_read": "0.0005",
        "credit_multiplier": "1",
        "base_credits_per_request": "0",
    }
    base.update(over)
    return base


def test_compute_billed_per_token():
    amount, unit = compute_billed(_snap(), prompt_tokens=1000, completion_tokens=500)
    assert unit == "credit"
    assert amount == Decimal("1000") * Decimal("0.001") + Decimal("500") * Decimal("0.002")


def test_compute_billed_multiplier():
    """credit_multiplier = 『不同模型按比例扣积分』。"""
    amount, _ = compute_billed(_snap(credit_multiplier="3"), prompt_tokens=1000, completion_tokens=500)
    plain, _ = compute_billed(_snap(), prompt_tokens=1000, completion_tokens=500)
    assert amount == plain * 3


def test_compute_billed_hybrid_base_plus_usage():
    amount, _ = compute_billed(
        _snap(charge_dimension="hybrid", base_amount="1", rate_input="0.001", rate_output="0.001"),
        prompt_tokens=1000,
        completion_tokens=1000,
    )
    # 1 底价 + 2 token 用量
    assert amount == Decimal("1") + Decimal("2")


def test_compute_billed_period_allowance_zero():
    """周期额度：正确扣减在 period_counters，账本 billed 只记 0（毛利看 upstream）。"""
    amount, _ = compute_billed(_snap(charge_dimension="period_allowance"), prompt_tokens=999, completion_tokens=999)
    assert amount == Decimal("0")


def test_compute_billed_no_snapshot_returns_none():
    assert compute_billed(None, prompt_tokens=1, completion_tokens=1) is None


# ---------- step 2：账本双阶段（端到端走 API） ----------


async def _send(client, auth_headers, persona, content="你好"):
    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    resp = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"content": content},
        headers=auth_headers,
    )
    return conv, resp


async def test_ledger_settled_after_message(client, auth_headers, persona, db_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    conv, resp = await _send(client, auth_headers, persona)
    assert resp.status_code == 200

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        logs = (await s.execute(select(UsageLog))).scalars().all()
    assert len(logs) == 1  # 一次对话恰好一行账
    log = logs[0]
    assert log.status == "settled"
    assert log.prompt_tokens and log.prompt_tokens > 0
    assert log.completion_tokens and log.completion_tokens > 0
    assert log.settled_at is not None
    assert log.source == "native"
    assert log.conversation_id is not None
    # 归因拆分：人设 system prompt 与用户输入分别可见
    assert log.attribution is not None
    assert set(log.attribution) == {"persona_prompt", "memory", "history", "current_input"}
    assert log.attribution["persona_prompt"] > 0  # 人设注入占成本（可见即可优化）
    # 计价：mock 模式上游价有值 + 费率快照已播种
    assert log.cost_upstream is not None
    assert log.rate_snapshot is not None
    assert log.currency == "credit"
    assert log.cost_billed is not None
    # 幂等键已生成
    assert log.idempotency_key and log.idempotency_key.startswith("msg:")


async def test_ledger_stream_settled(client, auth_headers, persona, db_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    async with client.stream(
        "POST", f"/api/v1/conversations/{conv['id']}/messages",
        json={"content": "流式", "stream": True}, headers=auth_headers,
    ) as resp:
        assert resp.status_code == 200
        async for _ in resp.aiter_lines():
            pass

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        logs = (await s.execute(select(UsageLog))).scalars().all()
    assert len(logs) == 1
    assert logs[0].status == "settled"
    assert logs[0].completion_tokens > 0


async def test_ledger_pending_before_llm_and_settled_after(client, auth_headers, persona, db_engine, monkeypatch):
    """阶段A：pending 必须在调 LLM 之前落库（崩溃可恢复的前提）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    import app.services.chat as chat_service
    from app.llm.gateway import StreamDone

    seen_states: list[str] = []

    class SlowBackend:
        async def chat(self, messages, model, **kw):
            # LLM 被调用的时刻 —— 此刻账本行必须已是 pending
            async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
                logs = (await s.execute(select(UsageLog))).scalars().all()
                seen_states.extend(l.status for l in logs)
            from app.llm.gateway import LLMResult

            return LLMResult(content="回复", model=model, prompt_tokens=10, completion_tokens=5)

    monkeypatch.setattr(chat_service, "get_llm_backend", lambda: SlowBackend())

    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    resp = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "hi"}, headers=auth_headers
    )
    assert resp.status_code == 200
    # 阶段A：调 LLM 前已 pending
    assert seen_states == ["pending"], f"LLM 被调时账本状态应为 pending，实为 {seen_states}"

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        logs = (await s.execute(select(UsageLog))).scalars().all()
    assert logs[0].status == "settled"


async def test_ledger_upstream_error_settles_failed(client, auth_headers, persona, db_engine, monkeypatch):
    """上游报错：预扣行必须以 error_code 结算（上游可能已耗 token，不可免单）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    import app.services.chat as chat_service

    class BoomBackend:
        async def chat(self, *a, **k):
            raise RuntimeError("upstream exploded")

    monkeypatch.setattr(chat_service, "get_llm_backend", lambda: BoomBackend())

    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    resp = await client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "hi"}, headers=auth_headers
    )
    assert resp.status_code >= 500  # 路由层转 502

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        logs = (await s.execute(select(UsageLog))).scalars().all()
    assert len(logs) == 1
    assert logs[0].status == "failed"
    assert logs[0].error_code and "upstream_error" in logs[0].error_code
    # 阶段A已 commit 的用户消息仍在（请求确实到达了）
    from app.models import Message

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        msgs = (await s.execute(select(Message))).scalars().all()
    assert any(m.role == "user" for m in msgs)


async def test_idempotency_key_resends_do_not_double_charge(client, auth_headers, persona, db_engine):
    """同一 Idempotency-Key 重发 → 复用已有账行，不双扣。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    # 原生面：幂等键由 user_message.id 生成，每次新消息天然不同；
    # 兼容面：显式 Idempotency-Key header
    resp = await client.post("/api/v1/me/keys", json={"name": "k"}, headers=auth_headers)
    key = resp.json()["key"]
    hdr = {"Authorization": f"Bearer {key}", "Idempotency-Key": "fixed-key-1"}

    body = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    r1 = await client.post("/v1/chat/completions", json=body, headers=hdr)
    assert r1.status_code == 200
    r2 = await client.post("/v1/chat/completions", json=body, headers=hdr)
    assert r2.status_code == 200

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        logs = (await s.execute(select(UsageLog))).scalars().all()
    compat_logs = [l for l in logs if l.source == "compat"]
    assert len(compat_logs) == 1, f"同一 Idempotency-Key 应只产生 1 行账，实为 {len(compat_logs)}"


async def test_compat_usage_records_source_uid(client, auth_headers):
    """兼容层 user 字段（qq:12345）现在真正落库 —— 此前收了但丢弃。"""
    from app.models import UsageLog
    from sqlalchemy.ext.asyncio import async_sessionmaker

    resp = await client.post("/api/v1/me/keys", json={"name": "k"}, headers=auth_headers)
    key = resp.json()["key"]
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "user": "qq:12345", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 200
    # 用量接口按模型分组，至少证明记账发生
    usage = (await client.get("/api/v1/me/usage", headers=auth_headers)).json()
    assert usage["total_requests"] >= 1


async def test_rating_rule_seeded_and_snapshot_in_ledger(client, auth_headers, persona, db_engine):
    """首次结算自动播种平台费率规则，且规则被快照进账本行。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "hi"}, headers=auth_headers
    )

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        rules = (await s.execute(select(RatingRule))).scalars().all()
        logs = (await s.execute(select(UsageLog))).scalars().all()
    assert len(rules) >= 1  # 播种成功
    assert rules[0].charge_dimension == "per_token"
    snap = logs[0].rate_snapshot
    assert snap is not None
    assert snap["rule_id"] == rules[0].id
    assert snap["unit"] == "credit"


async def test_regenerate_single_ledger_row(client, auth_headers, persona, db_engine):
    """重新生成一次：首问 1 行 + regen 1 行，单次调用不产生重复预扣。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "第一问"}, headers=auth_headers
    )
    async with client.stream(
        "POST", f"/api/v1/conversations/{conv['id']}/regenerate", headers=auth_headers
    ) as resp:
        async for _ in resp.aiter_lines():
            pass

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        logs = (await s.execute(select(UsageLog))).scalars().all()
    assert len(logs) == 2
    assert all(l.status == "settled" for l in logs)
    regen_keys = [l.idempotency_key for l in logs if l.idempotency_key and l.idempotency_key.startswith("regen:")]
    assert len(regen_keys) == 1


async def test_regenerate_twice_bills_each_call(client, auth_headers, persona, db_engine):
    """回归（评审 critical）：同会话第二次 regenerate 必须新起一行账并扣款 ——
    旧的会话级幂等键会让第二次及以后的重新生成永久免单。"""
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.billing.charge import balance
    from app.billing.wallet import WalletEntry
    from app.config import get_settings

    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages", json={"content": "第一问"}, headers=auth_headers
    )

    for _ in range(2):
        async with client.stream(
            "POST", f"/api/v1/conversations/{conv['id']}/regenerate", headers=auth_headers
        ) as resp:
            async for _line in resp.aiter_lines():
                pass

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        logs = (await s.execute(select(UsageLog))).scalars().all()
        consumes = (
            (await s.execute(select(WalletEntry).where(WalletEntry.entry_type == "consume")))
            .scalars()
            .all()
        )
        # 首问 1 行 + 两次 regen 各 1 行
        assert len(logs) == 3, f"应有 3 行账，实为 {len(logs)}: {[l.idempotency_key for l in logs]}"
        assert all(l.status == "settled" for l in logs)
        regen_keys = [l.idempotency_key for l in logs if l.idempotency_key.startswith("regen:")]
        assert len(regen_keys) == 2 and len(set(regen_keys)) == 2  # 每次调用键唯一
        # 每行账恰好一笔扣款
        assert len(consumes) == 3
        total_cost = sum(Decimal(l.cost_billed) for l in logs)
        assert await balance(s, logs[0].user_id) == get_settings().signup_grant_credits - total_cost


# ---------- /me/usage 口径：只算终态账行 ----------


async def test_me_usage_excludes_pending_rows(client, auth_headers, db_engine):
    """用量接口只统计终态账行（settled/failed/abandoned），与配额口径一致。

    双阶段账本会在调 LLM 前先写 pending 行；若用量接口把它算进去，本请求尚未
    完成时用量就已虚增（并发下更明显）。abandoned 是进程被 kill 后对账收编的
    残留，按预扣估算计入（保守）。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models import User

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        uid = (await s.execute(select(User.id))).scalars().first()

        def row(status: str, tokens: int, key: str) -> UsageLog:
            return UsageLog(
                user_id=uid,
                model="gpt-4o-mini",
                prompt_tokens=tokens,
                completion_tokens=0,
                status=status,
                idempotency_key=key,
                source="native",
            )

        s.add_all(
            [
                row("pending", 999_999, "t-pending"),
                row("settled", 10, "t-settled"),
                row("abandoned", 20, "t-abandoned"),
                row("failed", 30, "t-failed"),
            ]
        )
        await s.commit()

    usage = (await client.get("/api/v1/me/usage", headers=auth_headers)).json()
    # 只算 settled + abandoned + failed = 3 条；pending 的 999999 必须被排除
    assert usage["total_requests"] == 3, usage
    assert usage["total_prompt_tokens"] == 60, usage


# ---------- 断流结算（shield）与钱包扣费唯一约束 ----------


async def test_stream_disconnect_still_settles(
    client, auth_headers, persona, db_engine, monkeypatch
):
    """客户端中途断流：pending 行仍必须收敛为终态并扣费。

    断流时请求任务被取消，取消作用域下每个 await 都会再抛 CancelledError ——
    若结算不放在 CancelScope(shield=True) 里，结算会被打断，账行永远停在
    pending：上游跑了、钱花了，平台却收不到（且永久占额度）。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    import app.services.chat as chat_service
    from app.llm.gateway import StreamDone

    class MultiChunkBackend:
        async def chat_stream(self, messages, model, **kw):
            for i in range(6):
                yield f"块{i}"
            yield StreamDone(model=model, prompt_tokens=12, completion_tokens=6)

    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()

    monkeypatch.setattr(chat_service, "get_llm_backend", lambda: MultiChunkBackend())

    chunks = 0
    async with client.stream(
        "POST", f"/api/v1/conversations/{conv['id']}/messages",
        json={"content": "断流测试", "stream": True}, headers=auth_headers,
    ) as resp:
        assert resp.status_code == 200
        async for _line in resp.aiter_lines():
            chunks += 1
            if chunks >= 2:
                break  # 只读两行就断开，模拟用户关页面/网络断

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        logs = (await s.execute(select(UsageLog))).scalars().all()
    assert len(logs) == 1, f"应恰好一行账，实为 {len(logs)}"
    assert logs[0].status in ("settled", "failed"), f"断流后不得停在 {logs[0].status}"
    assert logs[0].settled_at is not None


async def test_wallet_entry_usage_log_id_is_unique(client, auth_headers, db_engine):
    """钱包扣费幂等靠**数据库唯一约束**，不能只靠应用层查询。

    并发下两个请求可能同时查不到已有流水而各扣一次 —— 只有 DB 约束能兜住。
    该测试直接打 DB，证明约束在模型与迁移里都真实存在。
    """
    from decimal import Decimal
    from uuid import UUID, uuid4

    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.billing.wallet import WalletEntry

    me = (await client.get("/api/v1/auth/me", headers=auth_headers)).json()
    uid = UUID(me["id"])
    log_id = uuid4()

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        s.add(
            WalletEntry(
                user_id=uid, entry_type="consume", amount=Decimal("-1"),
                balance_after=Decimal("999"), usage_log_id=log_id,
            )
        )
        await s.commit()

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        s.add(
            WalletEntry(
                user_id=uid, entry_type="consume", amount=Decimal("-1"),
                balance_after=Decimal("998"), usage_log_id=log_id,  # 同一账行再扣
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()

