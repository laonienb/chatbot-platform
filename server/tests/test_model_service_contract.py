"""平台侧模型服务契约测试 —— 对应 `docs/model-service-protocol.md` §10 的 T1–T11。

验收对象：`app/llm/remote.py`（RemoteBackend §10.1）、`app/llm/model_service_ops.py`
（启动自检 §8.3 / 目录同步 §9）、以及 `app/billing/ledger.py` 的成本优先级链（§5.1）。

跑法：对 `tests/fake_model_service.py` 起的**真实 HTTP 桩**发请求（非 mock transport），
因为取消传播（红线6）只有在真实连接断开时才可验证。
"""

from decimal import Decimal
from uuid import uuid4

import contextlib
import httpx
import pytest
from sqlalchemy import select

from app.billing.ledger import reserve_usage, settle_usage
from app.llm.gateway import StreamDone
from app.llm.remote import ModelServiceError, RemoteBackend, breaker
from app.models import UsageLog, User


async def _drain_done(gen) -> tuple[list[str], StreamDone | None]:
    """消费 chat_stream 生成器：收集内容增量与终止 StreamDone。"""
    parts: list[str] = []
    done: StreamDone | None = None
    async for piece in gen:
        if isinstance(piece, StreamDone):
            done = piece
        else:
            parts.append(piece)
    return parts, done


async def _any_user_id(db_session) -> object:
    return (await db_session.execute(select(User.id))).scalars().first()


# ============================================================ T1 非流式基础


async def test_t1_nonstream_basic_fields(remote_mode):
    """T1：响应含实测 usage；cost_usd 为十进制字符串；model_used/provider 如实。"""
    control, _ = remote_mode
    result = await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")

    assert result.prompt_tokens == control.prompt_tokens == 120
    assert result.completion_tokens == control.completion_tokens == 80
    assert result.cache_read_tokens == control.cached_tokens == 64
    assert result.reasoning_tokens == control.reasoning_tokens == 0
    assert result.metering_source == "provider"
    assert result.needs_review is False
    # 红线3：如实返回实际模型串与供应商
    assert result.model_used == "deepseek/deepseek-chat"
    assert result.provider == "deepseek"
    assert result.model == "deepseek/deepseek-chat"
    # 成本是 Decimal（不是 float），且与桩给的一致
    assert isinstance(result.cost_usd, Decimal)
    assert result.cost_usd == Decimal("0.000123")
    assert result.cost_status == "exact"


async def test_t1_metering_falls_back_and_flags(remote_mode):
    """usage 缺失时回退 tiktoken/estimated，并置 needs_review（不假装是实测值）。"""
    control, _ = remote_mode
    control.omit_usage = True
    result = await RemoteBackend().chat(
        [{"role": "user", "content": "前缀" * 20}], "deepseek-chat"
    )
    assert result.metering_source in ("tiktoken", "estimated")
    assert result.needs_review is True


# ============================================================ T2 成本不可得


async def test_t2_unknown_cost_is_null_never_zero(remote_mode):
    """T2：cost_status=unknown 时 cost_usd 必须为 None，**绝不为 0**（红线2）。"""
    control, _ = remote_mode
    control.cost_mode = "unknown"
    result = await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")

    assert result.cost_usd is None
    assert result.cost_status == "unknown"
    # 明确排除"假免费"：0 与 "0" 都不允许
    assert result.cost_usd != Decimal("0")
    assert result.cost_status != "exact"


async def test_t2_absent_extension_block(remote_mode):
    """Proxy 过渡期形状：整块 x_model_service 缺失 → 字段为 None（不是 0）。"""
    control, _ = remote_mode
    control.cost_mode = "absent"
    control.emit_terminate_event = False
    result = await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")

    assert result.cost_usd is None
    assert result.cost_status is None
    assert result.provider is None
    # usage 仍是实测（Proxy 也给 usage）
    assert result.prompt_tokens == 120


# ============================================================ T3 persona 前缀


async def test_t3_persona_prefix_rejected_400(remote_mode):
    """T3 / 红线10：persona: 前缀必须被模型服务 400 拒绝（越界立即失败）。"""
    _, base_url = remote_mode
    async with httpx.AsyncClient() as c:
        resp = await c.post(
            f"{base_url}/v1/chat/completions",
            json={"model": "persona:libai", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": "Bearer test-service-token"},
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_request"


async def test_t3_remote_surfaces_invalid_request(remote_mode):
    """平台侧把 persona 前缀被拒翻译成 invalid_request（不重试、不吞）。"""
    control, _ = remote_mode
    control.error = {"code": "invalid_request", "http": 400}
    with pytest.raises(ModelServiceError) as ei:
        await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert ei.value.code == "invalid_request"
    assert ei.value.status == 400
    assert ei.value.retry_after is None


# ============================================================ T4 流式完整链


async def test_t4_stream_chunks_usage_and_terminate_event(remote_mode):
    """T4：chunk 序列 → usage chunk → event: model_service → [DONE] 全链被正确消费。"""
    control, _ = remote_mode
    control.emit_keepalive = True  # 同时验证注释行被忽略（§6.4）
    parts, done = await _drain_done(
        RemoteBackend().chat_stream([{"role": "user", "content": "hi"}], "deepseek-chat")
    )

    assert "".join(parts) == "这是 fake 模型服务的流式回复。"
    assert done is not None
    # usage 来自 usage chunk（实测）
    assert done.prompt_tokens == 120
    assert done.completion_tokens == 80
    assert done.cache_read_tokens == 64
    assert done.metering_source == "provider"
    # 扩展字段来自 event: model_service（不污染 chunk 结构）
    assert done.provider == "deepseek"
    assert done.model_used == "deepseek/deepseek-chat"
    assert done.cost_usd == Decimal("0.000123")
    assert done.cost_status == "exact"
    # 审计 D：finish_reason 必须从 chunk 解析（此前恒为 None，账本失去归因价值）
    assert done.finish_reason == "stop"


async def test_t4_keepalive_never_leaks_into_content(remote_mode):
    """keepalive 注释行与未知命名事件都不得变成正文（宽松解析）。"""
    control, _ = remote_mode
    control.emit_keepalive = True
    parts, _ = await _drain_done(
        RemoteBackend().chat_stream([{"role": "user", "content": "hi"}], "deepseek-chat")
    )
    joined = "".join(parts)
    assert "keepalive" not in joined
    assert "event:" not in joined


# ============================================================ T5 429 双语义


async def test_t5_rate_limited_carries_retry_after(remote_mode):
    """T5a：rate_limited → 可退避重试；Retry-After 透传给平台（Q5-B 加法演进）。"""
    control, _ = remote_mode
    control.error = {"code": "rate_limited", "http": 429, "retry_after": 7}
    with pytest.raises(ModelServiceError) as ei:
        await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert ei.value.code == "rate_limited"
    assert ei.value.status == 429
    assert ei.value.retry_after == 7


async def test_t5_budget_exhausted_has_no_retry_after(remote_mode):
    """T5b：budget_exhausted → 平台**不重试**，也没有 Retry-After。"""
    control, _ = remote_mode
    control.error = {"code": "budget_exhausted", "http": 429}
    with pytest.raises(ModelServiceError) as ei:
        await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert ei.value.code == "budget_exhausted"
    assert ei.value.status == 429
    assert ei.value.retry_after is None  # 与 rate_limited 的唯一区分点


async def test_t5_business_errors_do_not_trip_breaker(remote_mode):
    """§11：429/400 等业务性错误**不计入**熔断（避免上游限流误杀整条链路）。"""
    control, _ = remote_mode
    control.error = {"code": "rate_limited", "http": 429, "retry_after": 1}
    for _ in range(breaker.threshold + 3):
        with pytest.raises(ModelServiceError):
            await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert breaker.state == "closed", "业务错误不应打开熔断"


async def test_t5_server_errors_trip_breaker(remote_mode):
    """§11：连续 5xx/连接错达到阈值 → 熔断打开，后续快速失败（不再打模型服务）。"""
    control, _ = remote_mode
    control.error = {"code": "upstream_error", "http": 502}
    for _ in range(breaker.threshold):
        with pytest.raises(ModelServiceError):
            await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert breaker.state == "open"

    calls_before = len(control.calls)
    with pytest.raises(ModelServiceError) as ei:
        await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert ei.value.code == "service_unavailable"
    assert len(control.calls) == calls_before, "熔断打开后不应再发起调用"


# ============================================================ T6 取消传播（红线6）


async def test_t6_disconnect_aborts_upstream_within_2s(remote_mode):
    """T6：平台断开连接后，上游调用必须 ≤2s 内被中止（红线6）。

    只读第一个 chunk 就 aclose()，模拟用户点「停止」/关页面。桩在生成器被取消时
    记录 `upstream_cancelled` —— 若 RemoteBackend 不真正断连，该计数会是 0。
    桩的 chunk 间隔被调大，否则服务端会在客户端断开前就把整个流写完（那样测的是
    "自然结束"而不是"取消传播"）。
    """
    import asyncio
    import time

    control, _ = remote_mode
    control.stream_chunk_delay_seconds = 0.25

    gen = RemoteBackend().chat_stream([{"role": "user", "content": "hi"}], "deepseek-chat")
    try:
        first = await gen.__anext__()
        assert isinstance(first, str), "应先拿到第一个内容块"
        started = time.monotonic()
        await gen.aclose()  # 平台侧断开
        close_elapsed = time.monotonic() - started
    finally:
        # aclose() 幂等；断言失败时也确保生成器被关闭，否则 httpx 连接泄漏
        with contextlib.suppress(Exception):
            await gen.aclose()

    # 桩需要一点时间感知断连（服务端写下一个 chunk 时才发现）
    for _ in range(150):
        if control.upstream_cancelled:
            break
        await asyncio.sleep(0.02)

    assert control.upstream_started >= 1, "上游应已被调用"
    assert control.upstream_completed == 0, "不应让上游自然跑完（那就没验证到取消）"
    assert control.upstream_cancelled >= 1, "断开后上游必须被中止（红线6）"
    assert close_elapsed < 2.0
    assert min(control.cancelled_within) < 2.0


# ============================================================ T7 幂等窗口


async def test_t7_same_idempotency_key_burns_upstream_once(remote_mode):
    """T7 / §8.2：同 Idempotency-Key 重放，上游**只被处理一次**，重放被拒。

    协议 §4.1 允许模型服务"返回首次结果**或** 409"二选一。桩选 409，并带
    `code: invalid_request`（§7 表未单列 409/idempotency 冲突），平台按 code 映射为
    400 —— 因此本测试断言的是**语义**（第二次被拒且未二次烧上游），而非硬编码状态码。
    """
    control, _ = remote_mode
    backend = RemoteBackend()
    key = f"msg:{uuid4()}"

    await backend.chat([{"role": "user", "content": "hi"}], "deepseek-chat", idempotency_key=key)
    assert control.idempotency_seen[key] == 1

    with pytest.raises(ModelServiceError) as ei:
        await backend.chat([{"role": "user", "content": "hi"}], "deepseek-chat", idempotency_key=key)
    assert 400 <= ei.value.status < 500, "重放应被拒为客户端错误"
    assert ei.value.code == "invalid_request"
    # 桩共收到 2 次请求：第二次在幂等检查处被拦，**没有**再烧一次上游
    assert len(control.calls) == 2
    assert control.idempotency_seen[key] == 2


async def test_t7_idempotency_key_is_forwarded(remote_mode):
    """§8.2：平台生成的 key 必须**透传**给模型服务（否则平台重试＝上游双烧）。"""
    control, _ = remote_mode
    await RemoteBackend().chat(
        [{"role": "user", "content": "hi"}], "deepseek-chat", idempotency_key="k-abc"
    )
    assert control.calls[-1]["headers"].get("idempotency-key") == "k-abc"
    # 协议 §4.1 必备头
    assert control.calls[-1]["headers"].get("x-request-id")
    assert control.calls[-1]["headers"].get("x-model-service-version") == "1"
    assert control.calls[-1]["headers"].get("authorization") == "Bearer test-service-token"


# ============================================================ T8 合规否决（红线5）


async def test_t8_compliance_denied_not_bypassable(remote_mode):
    """T8 / 红线5：routing.allow_providers 含被禁 provider → 403，且调用方放不开。"""
    control, _ = remote_mode
    control.error = {"code": "compliance_denied", "http": 403}
    with pytest.raises(ModelServiceError) as ei:
        await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert ei.value.code == "compliance_denied"
    assert ei.value.status == 403


async def test_t8_fake_enforces_policy_on_allow_list(remote_mode):
    """桩本身也要证明"请求放宽不了合规策略"：allow_providers 里塞禁用的即 403。"""
    _, base_url = remote_mode
    async with httpx.AsyncClient() as c:
        resp = await c.post(
            f"{base_url}/v1/chat/completions",
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": "hi"}],
                "routing": {"allow_providers": ["deepseek", "openai"]},
            },
            headers={"Authorization": "Bearer test-service-token"},
        )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "compliance_denied"


# ============================================================ T9 版本自检（红线7）


async def test_t9_native_version_mismatch_refuses_startup(remote_mode):
    """T9a / 红线7：native 模式 capabilities 版本不符 → 抛错（平台拒绝启动）。"""
    from app.llm.model_service_ops import ProtocolVersionError, startup_self_check

    control, _ = remote_mode
    control.capabilities = {"protocol_version": 999, "features": {}}
    with pytest.raises(ProtocolVersionError):
        await startup_self_check()


async def test_t9_native_version_match_passes(remote_mode):
    """T9b：版本一致 → 自检通过（且确实调了 /v1/capabilities）。

    RB-3 起 capabilities 的**超时四项 + `stream_keepalive` 缺一即失败**，故本用例必须给
    全（此前只给 `features.cost_attribution` 也能过 —— 那正是"缺键即跳过"放行的空转）。
    """
    from app.llm.model_service_ops import startup_self_check

    control, _ = remote_mode
    control.capabilities = {
        "protocol_version": 1,
        "features": {
            "cost_attribution": True,
            "stream_keepalive": False,
            "timeouts": {
                "upstream_first_token_s": 15,
                "upstream_first_content_s": 60,
                "upstream_chunk_idle_s": 30,
                "upstream_total_s": 60,
            },
        },
    }
    await startup_self_check()  # 不抛即通过


async def test_t9_proxy_mode_skips_capabilities_but_checks_liveness(remote_mode, monkeypatch):
    """T9c / §8.3：proxy 过渡模式跳过 capabilities 自检，改探活 + 目录可达。"""
    from app.config import get_settings
    from app.llm.model_service_ops import startup_self_check

    control, _ = remote_mode
    # proxy 期没有 /v1/capabilities：桩上把它变成 404 也不该被调用
    monkeypatch.setattr(get_settings(), "model_service_mode", "proxy")
    control.capabilities = {"protocol_version": 999}  # 即使是错版本也不该校验
    await startup_self_check()  # 不抛即说明跳过了 capabilities


# ============================================================ T10 目录同步


async def test_t10_sync_adds_missing_without_credentials(remote_mode, db_engine):
    """T10 / §9：拉 /v1/models 补缺失模型；**不携带任何凭证**（凭证已下沉）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.llm.model_service_ops import sync_catalog
    from app.models import LlmModel

    control, _ = remote_mode
    control.models = ["deepseek-chat", "qwen-max"]

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        result = await sync_catalog(s)
        assert result["added"] == 2
        assert result["total_upstream"] == 2
        rows = (await s.execute(select(LlmModel))).scalars().all()
        for row in rows:
            assert row.api_base is None, "同步不得携带凭证（§9）"
            assert row.api_key is None
            assert row.enabled is False, "新模型默认不启用，由管理员显式开"

        # 再同步一次：幂等，不重复加
        again = await sync_catalog(s)
        assert again["added"] == 0


async def test_t10_sync_does_not_overwrite_business_fields(remote_mode, db_engine):
    """Q6-A：展示名/排序/启用是平台业务字段，同步**不得覆盖**。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.llm.model_service_ops import sync_catalog
    from app.models import LlmModel

    control, _ = remote_mode
    control.models = ["deepseek-chat"]

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        s.add(
            LlmModel(
                name="我的 DeepSeek", model="deepseek-chat", enabled=True, is_default=True, sort=5
            )
        )
        await s.commit()
        await sync_catalog(s)
        row = (await s.execute(select(LlmModel))).scalars().one()
    assert row.name == "我的 DeepSeek"
    assert row.sort == 5
    assert row.enabled is True
    assert row.is_default is True


async def test_t10_model_not_found_maps_to_404(remote_mode):
    """§7：model_not_found → 404（平台据此触发即时重同步，不重试）。"""
    control, _ = remote_mode
    control.error = {"code": "model_not_found", "http": 404}
    with pytest.raises(ModelServiceError) as ei:
        await RemoteBackend().chat([{"role": "user", "content": "hi"}], "nonexistent")
    assert ei.value.status == 404
    assert ei.value.code == "model_not_found"


# ============================================================ T11 超时分层（红线8）


async def test_t11_timeout_budget_is_layered(remote_mode):
    """T11 / 红线8：模型服务上游超时必须 < 平台总超时（配置层断言，留结算余量）。"""
    from app.config import get_settings

    s = get_settings()
    assert s.model_service_timeout_first_byte < s.model_service_timeout_total
    assert s.model_service_timeout_connect < s.model_service_timeout_total
    # 首字节超时是本层最紧的约束（无首 token 即 504）
    assert s.model_service_timeout_first_byte <= 60


async def test_t11_slow_upstream_raises_unavailable(remote_mode, monkeypatch):
    """T11：上游慢于首字节超时 → 平台侧连接错误 → 503（不吊死请求）。"""
    from app.config import get_settings

    control, _ = remote_mode
    monkeypatch.setattr(get_settings(), "model_service_timeout_first_byte", 0.3)
    monkeypatch.setattr(get_settings(), "model_service_timeout_total", 1.0)
    control.delay_seconds = 2.0  # 上游比首字节超时慢

    with pytest.raises(ModelServiceError) as ei:
        await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert ei.value.code == "service_unavailable"
    assert ei.value.status == 503


# ============================================================ §5.1 成本优先级链（账本口径）


async def test_cost_chain_service_exact_overrides_price_table(
    remote_mode, client, auth_headers, db_engine
):
    """§5.1：cost_status=exact → 采信 cost_usd 为 cost_upstream。

    同时锁死最关键的一条：**用户扣费仍走 rating_rules**，与 cost_usd 无关 ——
    若把 cost_usd 当扣费依据，用户就会被按上游成本价扣积分，零售与毛利模型全废。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    control, _ = remote_mode
    control.cost_usd = "0.5"

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        uid = await _any_user_id(s)
        rid = await reserve_usage(
            s, user_id=uid, model_requested="deepseek-chat", model_upstream="deepseek-chat"
        )
        result = await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
        await settle_usage(s, rid, done=result)
        log = (await s.execute(select(UsageLog))).scalars().one()

    assert log.cost_source == "service_exact"
    assert log.cost_upstream == Decimal("0.5")
    assert log.provider == "deepseek"
    assert log.model_upstream == "deepseek/deepseek-chat"
    assert log.metering_source == "provider"
    # 扣费走费率快照（platform-token 播种规则），**不等于**上游成本
    assert log.cost_billed is not None
    assert log.currency == "credit"
    assert log.cost_billed != log.cost_upstream, "扣费绝不能等于上游成本价"


async def test_cost_chain_unknown_falls_back_to_price_table(
    remote_mode, client, auth_headers, db_engine
):
    """§5.1：unknown → 回落平台价格表，且**不当作免费**（cost_upstream != 0）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    control, _ = remote_mode
    control.cost_mode = "unknown"

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        uid = await _any_user_id(s)
        rid = await reserve_usage(
            s, user_id=uid, model_requested="deepseek-chat", model_upstream="deepseek-chat"
        )
        result = await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
        await settle_usage(s, rid, done=result)
        log = (await s.execute(select(UsageLog))).scalars().one()

    assert log.cost_source == "price_table"
    assert log.cost_upstream is not None
    assert log.cost_upstream != Decimal("0"), "unknown 绝不能被当成免费（红线2）"
    assert log.needs_review is False  # 价格表可信，无需复核


async def test_cost_chain_untabulated_model_yields_none_and_review(
    remote_mode, client, auth_headers, db_engine
):
    """§5.1 末档：价格表也查不到 → cost_source=none、(0, needs_review=True) 进对账。

    必须让**服务不给成本**且**价格表查不到**两件事同时成立：若服务给了 cost_usd，
    走的是上一档（service_exact），根本轮不到价格表。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    control, _ = remote_mode
    control.cost_mode = "absent"  # 服务不给成本 → 触发价格表回落
    control.emit_terminate_event = False
    control.model_used = "deepseek-chat"

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        uid = await _any_user_id(s)
        # 预扣行就用一个价格表查不到的模型串（结算不会覆盖它，因服务未给 model_used）
        rid = await reserve_usage(
            s, user_id=uid, model_requested="mystery-model", model_upstream="mystery-model"
        )
        result = await RemoteBackend().chat([{"role": "user", "content": "hi"}], "mystery-model")
        await settle_usage(s, rid, done=result)
        log = (await s.execute(select(UsageLog))).scalars().one()

    assert log.cost_source == "none"
    assert log.cost_upstream == Decimal("0")
    assert log.needs_review is True, "查不到价必须进对账清单，不能静默当免费"


async def test_cost_chain_estimated_trusted_but_flagged(
    remote_mode, client, auth_headers, db_engine
):
    """§5.1：estimated → 采信 cost_usd，但账本必须 needs_review=True。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    control, _ = remote_mode
    control.cost_mode = "estimated"
    control.cost_usd = "0.25"

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        uid = await _any_user_id(s)
        rid = await reserve_usage(
            s, user_id=uid, model_requested="deepseek-chat", model_upstream="deepseek-chat"
        )
        result = await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
        await settle_usage(s, rid, done=result)
        log = (await s.execute(select(UsageLog))).scalars().one()

    assert log.cost_source == "service_estimated"
    assert log.cost_upstream == Decimal("0.25")
    assert log.needs_review is True


async def test_cost_chain_stream_path_also_records(
    remote_mode, client, auth_headers, db_engine
):
    """流式路径同样要走成本链（不能只有非流式记录 cost_source）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    control, _ = remote_mode
    control.cost_usd = "0.75"

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        uid = await _any_user_id(s)
        rid = await reserve_usage(
            s, user_id=uid, model_requested="deepseek-chat", model_upstream="deepseek-chat"
        )
        _, done = await _drain_done(
            RemoteBackend().chat_stream([{"role": "user", "content": "hi"}], "deepseek-chat")
        )
        assert done is not None
        await settle_usage(s, rid, done=done)
        log = (await s.execute(select(UsageLog))).scalars().one()

    assert log.cost_source == "service_exact"
    assert log.cost_upstream == Decimal("0.75")
    assert log.cache_read_tokens == 64


# ============================================================ 审计 §13 修复项的回归


async def test_audit_a_stream_failures_trip_breaker(remote_mode):
    """审计 A（阻断）/§11：**流式是主路径**，其失败必须计入熔断。

    修复前 `chat_stream` 的 except 只抛 `_unavailable` 不记失败 → 流式整条链路挂掉
    而熔断永不触发，保护形同虚设。
    """
    control, _ = remote_mode
    control.error = {"code": "upstream_error", "http": 502}

    for _ in range(breaker.threshold):
        with pytest.raises(ModelServiceError):
            await _drain_done(
                RemoteBackend().chat_stream([{"role": "user", "content": "hi"}], "deepseek-chat")
            )
    assert breaker.state == "open", "流式失败必须能打开熔断"


async def test_audit_a_stream_timeout_trips_breaker(remote_mode, monkeypatch):
    """审计 A / §11：超时（含首字节超时）也计入熔断，不能只算 5xx。"""
    from app.config import get_settings

    control, _ = remote_mode
    monkeypatch.setattr(get_settings(), "model_service_timeout_first_byte", 0.2)
    monkeypatch.setattr(get_settings(), "model_service_timeout_total", 0.6)
    control.delay_seconds = 1.5  # 上游慢于首字节超时

    for _ in range(breaker.threshold):
        with pytest.raises(ModelServiceError):
            await _drain_done(
                RemoteBackend().chat_stream([{"role": "user", "content": "hi"}], "deepseek-chat")
            )
    assert breaker.state == "open", "超时必须计入熔断"


async def test_audit_a_half_open_allows_only_one_probe(remote_mode):
    """§11 修订：half_open 每次只放行 **1 个** 探测请求（不得恢复全量流量）。

    否则熔断退化为固定冷却窗，失去保护意义。
    """
    control, _ = remote_mode
    control.error = {"code": "upstream_error", "http": 502}
    for _ in range(breaker.threshold):
        with pytest.raises(ModelServiceError):
            await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert breaker.state == "open"

    # 手动把冷却期拨到已过（不真等 30s）
    breaker._opened_at -= breaker.reset_seconds + 1
    assert breaker.state == "half_open"

    assert breaker.allow() is True, "首个探测应放行"
    assert breaker.allow() is False, "探测在飞时不得再放行"
    assert breaker.allow() is False, "half_open 不得放行全量流量"

    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.allow() is True


async def test_audit_c_forbidden_fields_rejected_platform_side(remote_mode):
    """审计 C（阻断）/§4.2 红线1：平台侧必须**主动**拦截禁字段，而不是只靠约定。

    用会注入禁字段的后端子类验证：必须在出网前 400 失败，且**一个 HTTP 请求都不发**。
    """
    control, _ = remote_mode

    class LeakyBackend(RemoteBackend):
        def _body(self, model, messages, **kw):
            body = super()._body(model, messages, **kw)
            body["user_id"] = "leaked-uuid"  # 越界：把平台业务身份下移
            return body

    with pytest.raises(ModelServiceError) as ei:
        await LeakyBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert ei.value.code == "invalid_request"
    assert ei.value.status == 400
    assert control.calls == [], "禁字段必须在出网前拦截，不得发给模型服务"


async def test_audit_c_persona_prefix_rejected_platform_side(remote_mode):
    """红线10 的平台侧对应：未解析的 persona: 不该被发出去。"""
    control, _ = remote_mode
    with pytest.raises(ModelServiceError) as ei:
        await RemoteBackend().chat([{"role": "user", "content": "hi"}], "persona:libai")
    assert ei.value.status == 400
    assert control.calls == []


async def test_audit_c_message_level_forbidden_field(remote_mode):
    """禁字段借 messages 夹带同样必须被拦（防"换个位置传身份"）。"""
    control, _ = remote_mode
    with pytest.raises(ModelServiceError) as ei:
        await RemoteBackend().chat(
            [{"role": "user", "content": "hi", "conversation_id": "c-1"}], "deepseek-chat"
        )
    assert ei.value.status == 400
    assert control.calls == []


async def test_p1_4_no_retry_after_any_http_response(remote_mode):
    """P1-4 / 协议 §7.1 硬规则：**收到任何 HTTP 响应后一律不重试**。

    本节最初按 v1 的 §7 表实现了「internal_error 重试 1 次」，v1.3 定案推翻了它：
    原因是 §8.2 的幂等窗口会让第二次请求直接命中首次结果 —— 重试毫无收益且徒增延迟，
    `internal_error` 应交由熔断 + 降级链（§8.4/§11）承担。
    """
    control, _ = remote_mode
    control.error_sequence = [{"code": "internal_error", "http": 500}]

    with pytest.raises(ModelServiceError) as ei:
        await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert ei.value.status == 502
    assert len(control.calls) == 1, "收到响应后不得重试（§7.1）"


async def test_p1_4_retryable_field_is_parsed_but_not_retried(remote_mode):
    """§7.1 的 `error.retryable` 必须被解析（诊断用），但不驱动重试。

    协议同时规定「依据该字段而非状态码**决定重试**」与「收到响应后一律不重试」；
    两者叠加的实际语义是：**该字段当前不产生重试行为**，只影响可观测性。
    本用例把这一"契约内部张力"钉住，避免后人各自解读。
    """
    control, _ = remote_mode
    control.error = {
        "code": "upstream_error",
        "http": 502,
        "retryable": True,
    }

    with pytest.raises(ModelServiceError) as ei:
        await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert ei.value.retryable is True, "字段应被解析出来"
    assert len(control.calls) == 1, "但收到响应后仍不重试（§7.1 硬规则）"


async def test_p1_4_retryable_defaults_false(remote_mode):
    """§7.1：字段缺失 → 视为 false（保守）。"""
    control, _ = remote_mode
    control.error = {"code": "upstream_error", "http": 502}
    with pytest.raises(ModelServiceError) as ei:
        await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert ei.value.retryable is False


async def test_p1_4_connection_failure_retried_once(remote_mode):
    """§7.1：**请求未送达**（建连失败）是唯一允许平台侧重试的情形，且只重试 1 次。

    通过指向一个必然拒绝连接的端口模拟"未送达"。
    """
    from app.config import get_settings

    control, _ = remote_mode
    # 127.0.0.1:1 通常立即 ECONNREFUSED（未送达），不是慢超时
    get_settings().model_service_base_url = "http://127.0.0.1:1"

    with pytest.raises(ModelServiceError) as ei:
        await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert ei.value.code == "service_unavailable"
    assert control.upstream_started == 0  # 真实桩从未被触达（说明确实没送达）


async def test_p0_2_fourxx_never_trips_breaker(remote_mode):
    """P0-2 验收③ / §11：4xx 业务错误连打 10 次**不**打开熔断。"""
    control, _ = remote_mode
    control.error = {"code": "invalid_request", "http": 400}
    for _ in range(10):
        with pytest.raises(ModelServiceError):
            await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert breaker.state == "closed"

    control.error = {"code": "budget_exhausted", "http": 429}
    for _ in range(10):
        with pytest.raises(ModelServiceError):
            await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert breaker.state == "closed", "429 属业务错误，不得计入熔断"


async def test_p0_2_half_open_allows_only_one_of_many_concurrent(remote_mode):
    """P0-2 验收② / §11：half_open 下并发 10 个请求，**只有 1 个**真正发往上游。"""
    import asyncio

    control, _ = remote_mode
    # 先打到熔断打开
    control.error = {"code": "upstream_error", "http": 502}
    for _ in range(breaker.threshold):
        with pytest.raises(ModelServiceError):
            await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert breaker.state == "open"

    # 冷却期拨到已过 → half_open；此刻上游已恢复正常
    breaker._opened_at -= breaker.reset_seconds + 1
    assert breaker.state == "half_open"
    control.error = None
    calls_before = len(control.calls)

    async def one():
        try:
            await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
            return "ok"
        except ModelServiceError:
            return "rejected"

    results = await asyncio.gather(*[one() for _ in range(10)])
    sent = len(control.calls) - calls_before
    assert sent == 1, f"half_open 只应放行 1 个探测，实际发往上游 {sent} 个"
    assert results.count("ok") == 1


# ============================================================ P1-2 流式空闲超时（缺口 K）


async def test_p1_2_idle_stream_aborted(remote_mode, monkeypatch):
    """P1-2 / §8.1 缺口 K：无任何字节超过空闲上限 → 断开并走降级（504）。

    把熔断阈值调成 1，使"失败被计入"成为**可判定**的事实（单次失败即打开）——
    阈值保持默认 5 时，单次失败后状态仍是 closed，断言没有鉴别力。
    """
    from app.config import get_settings

    control, _ = remote_mode
    settings = get_settings()
    monkeypatch.setattr(settings, "model_service_stream_idle_timeout", 0.5)
    monkeypatch.setattr(settings, "model_service_timeout_first_byte", 30.0)  # 让手动看门狗生效
    monkeypatch.setattr(breaker, "threshold", 1)
    control.silence_after_chunks = 2
    control.silence_seconds = 3.0

    gen = RemoteBackend().chat_stream([{"role": "user", "content": "hi"}], "deepseek-chat")
    got: list[str] = []
    with pytest.raises(ModelServiceError) as ei:
        async for piece in gen:
            if isinstance(piece, str):
                got.append(piece)
    assert ei.value.code == "upstream_timeout"
    assert ei.value.status == 504
    assert got, "沉默前应已收到若干 chunk"
    assert control.stream_finished == 0, "服务端未发完就沉默，客户端不应看到正常结束"
    assert breaker.state == "open", "空闲超时必须计入熔断（§11）"


async def test_p1_2_keepalive_resets_idle_timer(remote_mode, monkeypatch):
    """P1-2 / §8.1：心跳 `: keepalive` **可重置**空闲计时 → 看门狗不误杀。

    这是"沉默时长"限制而非"总时长"限制的直接体现。用极小的空闲上限（0.3s）+ 
    更密的心跳（0.05s）构造强区分度：若心跳不重置计时，看门狗必然先触发。
    """
    from app.config import get_settings

    control, _ = remote_mode
    settings = get_settings()
    monkeypatch.setattr(settings, "model_service_stream_idle_timeout", 0.3)
    monkeypatch.setattr(settings, "model_service_timeout_first_byte", 30.0)
    control.silence_after_chunks = 2
    control.silence_seconds = 0.8
    control.keepalive_during_silence = True

    got: list[str] = []
    async for piece in RemoteBackend().chat_stream(
        [{"role": "user", "content": "hi"}], "deepseek-chat"
    ):
        if isinstance(piece, str):
            got.append(piece)
    # 未抛 ModelServiceError 即说明看门狗未被心跳期触发；且首个 chunk 已送达。
    assert len(got) >= 2, f"应至少收到 2 个内容块，实为 {got}"


# ============================================================ P1-3 mock 生产守卫（红线9）


async def test_p1_3_prod_with_mock_refuses_startup():
    """P1-3 / 红线9：APP_ENV=prod + LLM_BACKEND=mock → **拒绝启动**。

    mock 会假装成功（返回假回复并污染账本），是生产最危险的"降级路径"。
    """
    from app.config import Settings
    from app.main import create_app

    app = create_app(Settings(app_env="prod", llm_backend="mock"))
    with pytest.raises(RuntimeError) as ei:
        async with app.router.lifespan_context(app):
            pass
    assert "mock" in str(ei.value)
    assert "红线 9" in str(ei.value)


async def test_p1_3_dev_with_mock_starts_fine():
    """P1-3：APP_ENV=dev + mock 是正常的本地开发路径，必须能启动。"""
    from app.config import Settings
    from app.main import create_app

    app = create_app(Settings(app_env="dev", llm_backend="mock"))
    async with app.router.lifespan_context(app):
        pass  # 不抛即通过


# ============================================================ P1-5 自检失败可读性


async def test_p1_5_unreachable_service_gives_actionable_error(remote_mode):
    """P1-5：连不上模型服务时，异常信息必须给操作指引（而非裸 httpx 异常）。"""
    from app.config import get_settings
    from app.llm.model_service_ops import ProtocolVersionError, startup_self_check

    _, _ = remote_mode
    get_settings().model_service_base_url = "http://127.0.0.1:1"

    with pytest.raises(ProtocolVersionError) as ei:
        await startup_self_check()
    msg = str(ei.value)
    assert "模型服务不可达" in msg
    assert "MODEL_SERVICE_BASE_URL" in msg, "必须指出该改哪个配置"


# ============================================================ P2-1 目录下线语义


async def test_p2_1_disappeared_model_disabled_never_deleted(remote_mode, db_engine):
    """P2-1 / 协议 §9.1：上游消失 → 行标 `enabled=False`，**永不删除**。

    行承载展示名/排序/白名单等运营配置，"临时下架又恢复"不应变成人工重建。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.llm.model_service_ops import sync_catalog
    from app.models import LlmModel

    control, _ = remote_mode
    control.models = ["deepseek-chat", "qwen-max"]

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        await sync_catalog(s)
        rows = (await s.execute(select(LlmModel))).scalars().all()
        qwen = next(r for r in rows if r.model == "qwen-max")
        qwen.enabled = True  # 管理员启用过
        await s.commit()

        # 上游把 qwen-max 下架
        control.models = ["deepseek-chat"]
        result = await sync_catalog(s)
        assert result["disabled"] == 1

        rows = (await s.execute(select(LlmModel))).scalars().all()
        qwen = next(r for r in rows if r.model == "qwen-max")
        assert qwen.enabled is False, "下线应停用"
        # 行仍在（未删除），运营配置得以保留
        assert len(rows) == 2


async def test_p2_1_disabled_default_falls_back(remote_mode, db_engine):
    """P2-1：被下线的行原是默认模型 → 清除默认标记并回退到其他启用行。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.llm.model_service_ops import sync_catalog
    from app.models import LlmModel

    control, _ = remote_mode
    control.models = ["deepseek-chat", "qwen-max"]

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        await sync_catalog(s)
        rows = (await s.execute(select(LlmModel))).scalars().all()
        for r in rows:
            r.enabled = True
        qwen = next(r for r in rows if r.model == "qwen-max")
        qwen.is_default = True
        await s.commit()

        control.models = ["deepseek-chat"]  # 默认模型被下架
        result = await sync_catalog(s)
        assert result["new_default"] == "deepseek-chat"

        rows = (await s.execute(select(LlmModel))).scalars().all()
        assert next(r for r in rows if r.model == "qwen-max").is_default is False
        assert next(r for r in rows if r.model == "deepseek-chat").is_default is True


async def test_p2_1_sync_never_enables_rows(remote_mode, db_engine):
    """P2-1 验收③：同步**不启用**任何行、不改展示名/排序（业务字段归平台）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.llm.model_service_ops import sync_catalog
    from app.models import LlmModel

    control, _ = remote_mode
    control.models = ["deepseek-chat", "qwen-max", "glm-4"]

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        await sync_catalog(s)
        rows = (await s.execute(select(LlmModel))).scalars().all()
    assert len(rows) == 3
    assert all(r.enabled is False for r in rows), "同步不得自动启用"


# ============================================================ P2-2 超时分层断言（T11 自动化）


async def test_p2_2_capabilities_timeout_layering_violation_refuses_startup(
    remote_mode, monkeypatch
):
    """P2-2 / 红线8：capabilities 声明的内层超时 ≥ 平台空闲上限 → 拒绝启动。"""
    from app.config import get_settings
    from app.llm.model_service_ops import ProtocolVersionError, startup_self_check

    control, _ = remote_mode
    monkeypatch.setattr(get_settings(), "model_service_stream_idle_timeout", 60.0)
    control.capabilities = {
        "protocol_version": 1,
        # RB-3：声明位与四个超时键都必须在场（缺一即失败），故此处逐个给全；
        # 本用例仍保留**顶层** `timeouts` 形状（兼容读那条臂的既有覆盖）。
        "features": {"stream_keepalive": False},
        # 内层 120s ≥ 外层 60s → 违反红线 8
        "timeouts": {
            "upstream_first_token_s": 15,
            "upstream_first_content_s": 30,
            "upstream_chunk_idle_s": 30,
            "upstream_total_s": 120,
        },
    }
    with pytest.raises(ProtocolVersionError) as ei:
        await startup_self_check()
    assert "红线 8" in str(ei.value) or "内层" in str(ei.value)


async def test_p2_2_capabilities_timeout_layering_ok(remote_mode, monkeypatch):
    """P2-2：正常分层（内层 60s < 外层 90s）→ 启动通过。"""
    from app.config import get_settings
    from app.llm.model_service_ops import startup_self_check

    control, _ = remote_mode
    monkeypatch.setattr(get_settings(), "model_service_stream_idle_timeout", 90.0)
    control.capabilities = {
        "protocol_version": 1,
        # RB-3：`stream_keepalive=False` ⇒ 保守侧，内层须 < 空闲上限 90s。
        "features": {"stream_keepalive": False},
        "timeouts": {
            "upstream_first_token_s": 15,
            "upstream_first_content_s": 60,
            "upstream_chunk_idle_s": 30,
            "upstream_total_s": 60,
        },
    }
    await startup_self_check()  # 不抛即通过


# ============================================================ P2-3 trace.tenant 约束


async def test_p2_3_no_identity_derived_trace(remote_mode):
    """P2-3 / 红线1：平台**不发送**由用户标识派生的 trace（派生即换名传身份）。

    当前实现不发 trace 字段；若将来要发，必须是部署级不透明常量（§4.2）。
    """
    control, _ = remote_mode
    await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    body = control.calls[-1]["body"]
    assert "trace" not in body, "当前不应发送 trace；要发须是部署级常量（协议 §4.2）"


# ============================================================ P0-1 验收④：负面用例必须真会红


async def test_p0_1_negative_cases_actually_fail(remote_mode, monkeypatch):
    """P0-1 验收④：确认测试有牙齿 —— **故意破坏**后断言必须失败。

    不修改源码，而是构造"被破坏的实现"与"正确的断言"对比，证明断言不是永真的空壳：
    - 破坏 1（红线2）：把 unknown 成本当作 0 → T2 的 `cost_usd is None` 必须不成立；
    - 破坏 2（T1）：usage 不实测而硬编码 999 → T1 的 token 断言必须不成立。
    """
    import app.llm.remote as remote_mod

    control, _ = remote_mode
    control.cost_mode = "unknown"

    # 破坏 1：cost_usd 解析成 0（正是红线 2 禁止的"假免费"）
    # 用局部 patch 而非 monkeypatch.undo()：后者会连夹具设的 base_url 一起还原。
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(remote_mod, "_parse_cost", lambda raw: Decimal("0"))
        broken = await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert broken.cost_usd == Decimal("0")
    # 这正是 T2 会捕获的失败：正确的实现必须返回 None
    assert broken.cost_usd is not None, "破坏后 T2 的断言应当失败（证明它有效）"

    # 破坏 2：token 计量不来自实测
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(remote_mod, "_usage_from", lambda usage: (999, 999, 0, 0))
        broken2 = await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert broken2.prompt_tokens == 999
    assert broken2.prompt_tokens != control.prompt_tokens, "破坏后 T1 的断言应当失败"


# ============================================================ 复核①② 熔断计数正确性
# 上一轮复核发现两个非直觉缺陷，现有绿灯用例都抓不到（它们只断言「会打开」/「只有
# 1 个到达上游」，对「一次失败计几次」「被拒请求是否改计数」不设防）。这两条专门锁定。


async def test_recheck_streaming_transport_failure_counts_once(remote_mode):
    """缺陷①：一次流式传输失败只能给熔断记 **1** 次。

    修复前 `chat_stream` 的 `except httpx.HTTPError` 记一次、其 raise 的 `_unavailable`
    又记一次 → 连接错/读超时按 +2 计，熔断在 ⌈阈值/2⌉ 就误开（阈值语义被悄悄减半）。
    """
    from app.config import get_settings

    breaker.reset()
    get_settings().model_service_base_url = "http://127.0.0.1:1"  # 必然建连失败（传输错误）

    with pytest.raises(ModelServiceError):
        await _drain_done(
            RemoteBackend().chat_stream([{"role": "user", "content": "hi"}], "deepseek-chat")
        )
    assert breaker._consecutive_failures == 1, (
        f"流式传输失败应恰好计 1 次，实际 {breaker._consecutive_failures}（双计回归）"
    )


async def test_recheck_breaker_rejection_not_counted(remote_mode):
    """缺陷②：被熔断主动拒绝的请求未出网，不得再计入连续失败。

    修复前 `_unavailable` 无条件 `record_failure`，连「因熔断打开而被拒」的请求也算失败，
    还在 half_open 下清掉在飞探测的 `_probe_in_flight` 位，破坏单探测不变量。
    """
    control, _ = remote_mode
    control.error = {"code": "upstream_error", "http": 502}
    for _ in range(breaker.threshold):
        with pytest.raises(ModelServiceError):
            await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert breaker.state == "open"
    failures_at_open = breaker._consecutive_failures

    # 熔断已开：这一发在 allow() 处被拒，根本没碰网络，计数不应再涨。
    with pytest.raises(ModelServiceError) as ei:
        await RemoteBackend().chat([{"role": "user", "content": "hi"}], "deepseek-chat")
    assert ei.value.code == "service_unavailable"
    assert breaker._consecutive_failures == failures_at_open, (
        "被拒请求不应计入连续失败（此前经 _unavailable 误记）"
    )


# ============================================================ P1-6 流式绝对上限（§8.1 / §8.1.1）


async def test_p1_6_absolute_cap_terminates_despite_keepalive(remote_mode, monkeypatch):
    """§8.1 缺口 M：心跳能重置空闲计时，但**绝对上限**（心跳不可重置）仍须按时切断流。

    桩发 2 个内容 chunk 后进入沉默期、每 0.1s 吐 `: keepalive`（持续 3s）。空闲上限被撑到
    30s（心跳永远喂不满它），绝对上限压到 0.5s。正确实现应在 ~0.5s **优雅收尾**（不 raise）、
    保留已收内容、`terminate_reason=stream_max_duration`、计入熔断一次（§8.1.1）。
    """
    import time as _t

    from app.config import get_settings

    control, _ = remote_mode
    control.silence_after_chunks = 2
    control.keepalive_during_silence = True
    control.silence_seconds = 3.0
    monkeypatch.setattr(get_settings(), "model_service_stream_idle_timeout", 30.0)
    monkeypatch.setattr(get_settings(), "model_service_stream_max_duration", 0.5)
    breaker.reset()

    t0 = _t.monotonic()
    parts, done = await _drain_done(
        RemoteBackend().chat_stream([{"role": "user", "content": "hi"}], "deepseek-chat")
    )
    elapsed = _t.monotonic() - t0

    assert elapsed < 2.0, f"绝对上限未在 ~0.5s 切断（实际 {elapsed:.2f}s，疑似空闲计时误当兜底）"
    assert done is not None and done.terminate_reason == "stream_max_duration"
    assert len(parts) >= 1, "绝对上限前已产生的部分内容必须保留"
    assert breaker._consecutive_failures == 1, "§8.1.1-4：绝对上限计入熔断一次"


async def test_p1_6_absolute_cap_settles_partial_not_failed(
    remote_mode, client, auth_headers, db_engine
):
    """§8.1.1-3/-5：绝对上限的账本按已收 token **结算为 settled（不是 failed）**，
    并记 `error_code=stream_max_duration`。直接喂一个带 terminate_reason 的 StreamDone 走结算。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.llm.gateway import StreamDone

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        uid = await _any_user_id(s)
        rid = await reserve_usage(
            s, user_id=uid, model_requested="deepseek-chat", model_upstream="deepseek-chat"
        )
        done = StreamDone(
            model="deepseek-chat",
            prompt_tokens=50,
            completion_tokens=20,
            metering_source="estimated",
            needs_review=True,
            terminate_reason="stream_max_duration",
        )
        # 上层（chat.py）会把 done.terminate_reason 作为 error_code 传入；此处模拟该契约
        await settle_usage(s, rid, done=done, error_code=done.terminate_reason)
        log = (await s.execute(select(UsageLog))).scalars().one()

    assert log.status == "settled", "§8.1.1-3：绝对上限部分内容结算为 settled，不是 failed"
    assert log.error_code == "stream_max_duration", "§8.1.1-5：记 error_code 便于归因"
    assert log.completion_tokens == 20, "按已收 token 结算"


# ============================================================ P1-9 / T12 流式错误语义（§6.7 + §11）


async def test_t12_pre_first_byte_error_keeps_http_semantics(remote_mode):
    """T12① / §6.7：首字节**前**的失败仍是一条 HTTP 错误响应，`code`/`Retry-After` 都在。

    这正是 S3 把首字节前的错误改回 HTTP 状态码的意义：若它发成「200 + 流内帧」，平台就读不到
    状态码与 header，限流退避、`error.code` 透传、熔断计数会全部失效。
    """
    control, _ = remote_mode
    control.error = {"code": "rate_limited", "retry_after": 5, "retryable": True}
    with pytest.raises(ModelServiceError) as ei:
        await _drain_done(
            RemoteBackend().chat_stream([{"role": "user", "content": "hi"}], "deepseek-chat")
        )
    assert ei.value.code == "rate_limited"
    assert ei.value.status == 429
    assert ei.value.retry_after == 5, "限流退避时长必须可读"
    assert breaker._consecutive_failures == 0, "§11：429 不计熔断"


async def test_p1_9_instream_rate_limited_frame_not_counted(remote_mode):
    """P1-9 验收③ **方向 A**：流内 `rate_limited` 帧**不得**计熔断（§11 负列表）。

    架构师 A 预裁决：是否计熔断由 **`error.code` 经 `_CODE_TO_PLATFORM`** 决定，不由
    "是不是流内帧"决定。若实现写成"流内帧一律计入"，上游重复限流会把整条链路熔断——
    正是 §11 要避免的误杀。

    预置 3 次连续失败再跑：错误实现要么变 4（计入）、要么归零（当成功），两个方向都能被抓。
    """
    control, _ = remote_mode
    for _ in range(3):
        breaker.record_failure()
    assert breaker._consecutive_failures == 3

    control.stream_error = {"code": "rate_limited", "retry_after_s": 30, "retryable": True}
    parts, done = await _drain_done(
        RemoteBackend().chat_stream([{"role": "user", "content": "hi"}], "deepseek-chat")
    )

    assert done is not None
    assert done.terminate_reason == "rate_limited", "§6.7：必须按 code 归因，不得当正常结束"
    assert done.error_message is not None, "对外要能给出错误帧，不能只留一个 code"
    assert done.retry_after_s == 30, "v1.7-澄清2：流内帧没有 header，退避只能走 retry_after_s"
    assert parts, "错误帧之前已产出的部分内容必须保留"
    assert breaker._consecutive_failures == 3, "限流帧既不记失败也不记成功（预审②）"


async def test_p1_9_instream_internal_error_frame_counted_exactly_once(remote_mode):
    """P1-9 验收②③ **方向 B**：流内 `internal_error` 帧 → 计熔断**恰好一次**。

    与上一条成对：只测一个方向就会写出另一个方向的一刀切。
    "恰好一次"同时看守预审③的防双计（帧计一次 + 关流再抛 httpx 错误又计一次）。
    """
    control, _ = remote_mode
    breaker.reset()
    control.stream_error = {"code": "internal_error"}
    parts, done = await _drain_done(
        RemoteBackend().chat_stream([{"role": "user", "content": "hi"}], "deepseek-chat")
    )
    assert done.terminate_reason == "internal_error"
    assert breaker._consecutive_failures == 1, (
        "§11：internal_error 计入熔断，且每请求至多记一次（双计会让熔断在 ⌈threshold/2⌉ 就打开）"
    )


async def test_p1_9_instream_error_usage_adopted_not_downgraded(remote_mode):
    """P1-9 验收⑤（G3-2）：错误帧带实测 `usage` 时平台**采信**，不得无故降档估算。

    错误终止时上游已产出的 token 是**已计费**的实测值，而服务侧明知其值；平台若丢掉它，
    账本就降档到 tiktoken/estimated + `needs_review`（毛利数字跟着失真）。
    """
    control, _ = remote_mode
    control.stream_error = {"code": "internal_error"}
    control.stream_error_usage = True
    _, done = await _drain_done(
        RemoteBackend().chat_stream([{"role": "user", "content": "hi"}], "deepseek-chat")
    )
    # ⚠️ 必须**同时**断言归因：只断言 usage 数字的话，本用例会变成永绿用例——帧里的
    # `usage` 会被下面那条通用的 usage 提取顺带捡到，即使"错误帧识别"整段被删掉也照样通过。
    assert done.terminate_reason == "internal_error", "错误帧本身必须被识别"
    assert done.prompt_tokens == control.prompt_tokens == 120
    assert done.completion_tokens == control.completion_tokens == 80
    assert done.metering_source == "provider", "帧内有实测值 → 不得降到 tiktoken/estimated"
    assert done.needs_review is False


async def test_p1_9_instream_error_settles_partial_as_settled(
    remote_mode, client, auth_headers, db_engine
):
    """P1-9 验收②/§6.7 + §8.1.1：流内错误终止 → 部分内容按**已收 token 结算为 settled**。

    不是 `failed`：上游确实产出并计费了。`error_code` = 帧内 code，归因与"整条请求失败"区分开
    （§7.1：两类入窗结论相同、归因不同）。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    control, _ = remote_mode
    control.stream_error = {"code": "internal_error"}
    control.stream_error_usage = True
    _, done = await _drain_done(
        RemoteBackend().chat_stream([{"role": "user", "content": "hi"}], "deepseek-chat")
    )

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        uid = await _any_user_id(s)
        rid = await reserve_usage(
            s, user_id=uid, model_requested="deepseek-chat", model_upstream="deepseek-chat"
        )
        # chat.py 的既有通路：terminate_reason → settle_usage(error_code=...)
        await settle_usage(s, rid, done=done, error_code=done.terminate_reason)
        log = (await s.execute(select(UsageLog))).scalars().one()

    assert log.status == "settled", "§8.1.1-3：已产出的部分内容结算为 settled，不是 failed"
    assert log.error_code == "internal_error", "按帧内 code 归因"
    assert log.completion_tokens == 80


async def test_p1_9_clean_stream_still_counts_as_success(remote_mode):
    """回归锁：正常流仍是成功 —— 修 F2 不能把 happy path 一起改成"永远计失败"。

    顺带把 P1-8 的平台侧消费钉住：服务侧（与桩同形）发的收尾 `finish_reason` 必须被解析进
    `StreamDone`，否则 P1-1 的归因在生产路径恒为 None。
    """
    control, _ = remote_mode
    for _ in range(2):
        breaker.record_failure()
    parts, done = await _drain_done(
        RemoteBackend().chat_stream([{"role": "user", "content": "hi"}], "deepseek-chat")
    )
    assert parts and done is not None
    assert done.terminate_reason is None and done.error_message is None
    assert done.finish_reason == control.finish_reason == "stop", "P1-8：平台须解析收尾 chunk"
    assert breaker._consecutive_failures == 0, "正常结束应清零连续失败数"


# ============================================================ H2 / 红线8：中途空闲预算也要断言


async def test_h2_chunk_idle_budget_violation_refuses_startup(remote_mode, monkeypatch):
    """H2（随 P1-9）：`upstream_chunk_idle_s` ≥ 平台流式空闲上限 → 拒绝启动。

    服务侧若把中途空闲预算配得比平台还大，平台会先按空闲超时断开，服务侧的
    `upstream_chunk_timeout` 归因**永远看不到**（红线 8「内层 < 外层」）。
    """
    from app.config import get_settings
    from app.llm.model_service_ops import ProtocolVersionError, startup_self_check

    control, _ = remote_mode
    monkeypatch.setattr(get_settings(), "model_service_stream_idle_timeout", 60.0)
    control.capabilities = {
        "protocol_version": 1,
        "features": {
            # 显式 `False` ⇒ 保守侧，外层 = 空闲上限 60s（外层是 180s 时 120 < 180 会合法通过，
            # 本用例要验的正是"不发心跳的服务把中途空闲配到 120 必须被拦"）。
            "stream_keepalive": False,
            "timeouts": {
                "upstream_first_token_s": 15,
                "upstream_first_content_s": 30,
                "upstream_total_s": 50,
                "upstream_chunk_idle_s": 120,  # ≥ 外层 60 → 违反
            },
        },
    }
    with pytest.raises(ProtocolVersionError) as ei:
        await startup_self_check()
    assert "中途空闲" in str(ei.value)


async def test_p2_2_timeouts_read_from_real_service_nesting(remote_mode, monkeypatch):
    """红线 8 断言必须读得到**真实服务**的形状：`features.timeouts`。

    真实服务（`msvc/main.py` 的 capabilities）把 timeouts 放在 `features` 里，而平台此前只读
    顶层 `caps["timeouts"]`；叠上"字段缺失即跳过"，红线 8 断言对真实服务**静默不执行**
    （P2-2 名义闭合、实则从未触发，与 P1-8 同一类"两端各跑自家桩"盲区）。
    本用例故意用嵌套形状注入违规 —— 顶层读法会当"没有该字段"直接放行。
    """
    from app.config import get_settings
    from app.llm.model_service_ops import ProtocolVersionError, startup_self_check

    control, _ = remote_mode
    monkeypatch.setattr(get_settings(), "model_service_stream_idle_timeout", 60.0)
    control.capabilities = {
        "protocol_version": 1,
        # RB-3：声明位与四项给全，**违规仍然只挂在嵌套这一侧** —— 若谁把读法改回只读顶层
        # 就会当"没有该字段"，而"缺键即失败"此时也不会救它（顶层读到的是空 dict）。
        "features": {
            "stream_keepalive": False,
            "timeouts": {
                "upstream_first_token_s": 15,
                "upstream_first_content_s": 30,
                "upstream_chunk_idle_s": 30,
                "upstream_total_s": 120,  # ≥ 外层 60 → 违反
            },
        },
    }
    with pytest.raises(ProtocolVersionError) as ei:
        await startup_self_check()
    assert "上游总超时" in str(ei.value), "必须是分层违规，不是缺键报错"


async def test_p2_2_default_stub_capabilities_pass(remote_mode, monkeypatch):
    """桩默认 capabilities 与真实服务同形（`features.timeouts`，15/30/60）→ 自检须通过。

    这一条保证"同形"没被写坏：如果谁把桩的形状改回顶层，上一条盲区就会重新出现。
    """
    from app.config import get_settings
    from app.llm.model_service_ops import startup_self_check

    control, _ = remote_mode
    assert "timeouts" not in control.capabilities, "桩必须与真实服务同形（timeouts 在 features 内）"
    monkeypatch.setattr(get_settings(), "model_service_stream_idle_timeout", 90.0)
    await startup_self_check()  # 不抛即通过


# ============================================ RB-3／RB-4 capabilities 声明位与对标对象
#
# 判据映射（工作单 W-1 §3）：
#   A3 → test_rb3_missing_*_refuses_startup（两条：循环路径 / 声明位路径）
#   B1 → test_rb4_keepalive_true_*（会发心跳 ⇒ 对标 180s）
#   B2 → test_rb4_keepalive_false_*（不发心跳 ⇒ 保守侧 90s）
#
# B1 与 B2 是**方向相反的一对**，缺一即漏一类错误：只有 B1 会写出"一律 180s"（漏掉不发
# 心跳的服务，真外层只有 90s 却被放行）；只有 B2 等于维持现状（"一律 90s"）。
# ⚠️ B2 单独**不能**证明新逻辑存在——它在改动前的旧代码下也是红的（120 ≥ 90）；
# 只有 B1 在旧代码下必红、在新代码下必绿，两条一起才锁得住"按 keepalive 分档"。

# 一份**完全合法**的 capabilities 底座：其余键全对，只让被测的那一项不同。
# A3 的用例必须建立在它之上 —— 若给一个整体崩溃的 payload（例如协议版本错），
# 失败会发生在版本检查上，"缺键即失败"与"缺键跳过"**都**会红 ⇒ 用例无牙齿。
def _valid_caps(**overrides) -> dict:
    features = {
        "cost_attribution": True,
        "stream_keepalive": False,
        "timeouts": {
            "upstream_first_token_s": 15,
            "upstream_first_content_s": 60,
            "upstream_chunk_idle_s": 30,
            "upstream_total_s": 60,
        },
    }
    for key in ("stream_keepalive",):
        if key in overrides:
            features[key] = overrides.pop(key)
    if "timeouts" in overrides:
        features["timeouts"] = overrides.pop("timeouts")
    elif "drop_timeout_key" in overrides:
        del features["timeouts"][overrides.pop("drop_timeout_key")]
    caps = {"protocol_version": 1, "features": features}
    caps.update(overrides)
    return caps


@pytest.mark.parametrize(
    "dropped",
    ["upstream_first_content_s", "upstream_first_token_s", "upstream_chunk_idle_s", "upstream_total_s"],
)
async def test_rb3_missing_timeout_key_refuses_startup(remote_mode, monkeypatch, dropped):
    """A3①（§8.1 硬约束 2，v1.7-澄清5）：caps 缺**任一**超时键 ⇒ **拒绝启动**，报错点名该键。

    这一条正对 P2-2 的旧病：旧写法是 `if inner is None: continue`，注释给的理由是
    "Proxy 过渡期无 capabilities 契约（§8.3）"——**那个理由是假的**（proxy 在本函数开头
    已 return，根本走不到循环）。它保护的不是 proxy，只是把 native 路径的缺键静默吞掉：
    整个 `timeouts` 缺失时每个键都是 `None`、全部 `continue` ⇒ 断言一条都不执行、**零日志
    零警告**。这就是"名义闭合、实则空转"的真相。

    mutation（本用例的牙齿）：把"缺键即失败"改回"缺键跳过"（`continue`）⇒ 必须红。
    ⚠️ 断言**缺的键名出现在报错里**（不能只断言 "红线 8"，太弱、无法定位到是哪一项）。
    """
    from app.config import get_settings
    from app.llm.model_service_ops import ProtocolVersionError, startup_self_check

    control, _ = remote_mode
    monkeypatch.setattr(get_settings(), "model_service_stream_idle_timeout", 90.0)
    monkeypatch.setattr(get_settings(), "model_service_stream_max_duration", 180.0)
    control.capabilities = _valid_caps(drop_timeout_key=dropped)

    with pytest.raises(ProtocolVersionError) as ei:
        await startup_self_check()
    msg = str(ei.value)
    assert dropped in msg, f"报错必须点名缺的是哪个键，实际：{msg}"
    assert "缺" in msg, f"必须是缺键类报错（不是分层违规）：{msg}"


async def test_rb3_missing_stream_keepalive_refuses_startup(remote_mode, monkeypatch):
    """A3②（§28.2）：caps 缺 `features.stream_keepalive` ⇒ **拒绝启动**，报错点名该键。

    与上一条**分属两条判定路径**（超时四项走循环、声明位走独立检查），必须各有一条用例，
    否则其中一条分支无人看守。声明位缺失时**不**静默回落保守侧：保守侧只适用于"显式声明
    了 false"，缺失是服务没履约（协议 §8.1 硬约束 2 把它列进"缺键即失败"清单）。
    """
    from app.config import get_settings
    from app.llm.model_service_ops import ProtocolVersionError, startup_self_check

    control, _ = remote_mode
    monkeypatch.setattr(get_settings(), "model_service_stream_idle_timeout", 90.0)
    monkeypatch.setattr(get_settings(), "model_service_stream_max_duration", 180.0)
    caps = _valid_caps()
    del caps["features"]["stream_keepalive"]
    control.capabilities = caps

    with pytest.raises(ProtocolVersionError) as ei:
        await startup_self_check()
    assert "stream_keepalive" in str(ei.value), (
        f"报错必须点名 stream_keepalive：{ei.value}"
    )


async def test_rb4_keepalive_true_benchmarks_absolute_cap(remote_mode, monkeypatch):
    """B1（§8.1 硬约束 2 的 A 裁定）：`stream_keepalive=true` + 内层 120s ⇒ **断言通过**。

    服务会发 `: keepalive` ⇒ 平台「流式空闲上限 90s」**永不触发**（心跳持续复位它），
    真正的外层边界是**绝对上限 180s** ⇒ 一个合规的 120s 内层预算必须被放行。
    旧代码拿它跟 90s 比 ⇒ 120 ≥ 90 ⇒ 误判违规、**平台拒绝启动**（协议 §8.1 原文点名的
    两种误判之一）。

    mutation（本用例的牙齿）：把对标值写死成 90s（即忽略 keepalive 分档）⇒ 必须红。
    """
    from app.config import get_settings
    from app.llm.model_service_ops import startup_self_check

    control, _ = remote_mode
    monkeypatch.setattr(get_settings(), "model_service_stream_idle_timeout", 90.0)
    monkeypatch.setattr(get_settings(), "model_service_stream_max_duration", 180.0)
    control.capabilities = _valid_caps(
        stream_keepalive=True,
        timeouts={
            "upstream_first_token_s": 15,
            "upstream_first_content_s": 120,  # 合法：< 180s 绝对上限
            "upstream_chunk_idle_s": 120,
            "upstream_total_s": 60,
        },
    )
    await startup_self_check()  # 不抛即通过

    # 边界：仍然必须**严格小于**绝对上限（180s 是硬天花板，不是"≤"）
    control.capabilities = _valid_caps(
        stream_keepalive=True,
        timeouts={
            "upstream_first_token_s": 15,
            "upstream_first_content_s": 120,
            "upstream_chunk_idle_s": 180,  # == 绝对上限 ⇒ 违反
            "upstream_total_s": 60,
        },
    )
    with pytest.raises(Exception) as ei:
        await startup_self_check()
    assert "绝对上限" in str(ei.value), (
        f"心跳服务的违规文案必须指向**绝对上限**（实际参与判定的那个）：{ei.value}"
    )


async def test_rb4_keepalive_false_benchmarks_idle_cap(remote_mode, monkeypatch):
    """B2（§8.1 硬约束 2 的保守侧）：`stream_keepalive=false` + 内层 120s ⇒ **断言失败**。

    服务**不发**心跳 ⇒ 空闲上限 90s 是真外层 ⇒ 120s 的内层预算必须先被拦下，否则平台会在
    90s 处断开、把一个本该由服务侧归因（`upstream_chunk_timeout`）的故障抢走
    （钱花了、回包丢了、归因没了）。

    mutation（本用例的牙齿）：去掉保守回退（一律对标 180s）⇒ 必须红。
    ⚠️ 与 B1 配对才有意义：本用例在旧代码（一律 90s）下**也是红的**，它单独不能证明
    新逻辑存在。
    """
    from app.config import get_settings
    from app.llm.model_service_ops import ProtocolVersionError, startup_self_check

    control, _ = remote_mode
    monkeypatch.setattr(get_settings(), "model_service_stream_idle_timeout", 90.0)
    monkeypatch.setattr(get_settings(), "model_service_stream_max_duration", 180.0)
    control.capabilities = _valid_caps(
        stream_keepalive=False,
        timeouts={
            "upstream_first_token_s": 15,
            "upstream_first_content_s": 120,  # < 180s 但 ≥ 90s ⇒ 保守侧下违规
            "upstream_chunk_idle_s": 30,
            "upstream_total_s": 60,
        },
    )
    with pytest.raises(ProtocolVersionError) as ei:
        await startup_self_check()
    msg = str(ei.value)
    # 文案必须**参数化**：指向实际选中的外层（空闲上限）与实际的配置名，
    # 而不是硬写"90s"（keepalive=true 时那条报错会指向一个没参与判定的配置）。
    assert "空闲上限" in msg, f"保守侧必须给出「空闲上限」这个原因：{msg}"
    assert "MODEL_SERVICE_STREAM_IDLE_TIMEOUT" in msg, f"必须指出该调哪个配置：{msg}"
    assert "stream_keepalive=False" in msg, f"必须说明判定依据是声明位：{msg}"


async def test_rb4_keepalive_true_still_bounds_nonstream_total(remote_mode, monkeypatch):
    """`upstream_total_s` **不随** keepalive 放宽（工作单 W-1 架构师裁定 §五）。

    协议 §8.1 表：该预算**仅约束非流式**，按平台读超时口径对标。若把它并进那条按
    keepalive 分档的循环，`keepalive=true` 时它会被放行到 180s —— 即**一条非流式预算
    被放过了头**（这正是"把它从循环里摘出来"的理由）。本用例锁住"摘出来"这个结构。
    """
    from app.config import get_settings
    from app.llm.model_service_ops import ProtocolVersionError, startup_self_check

    control, _ = remote_mode
    monkeypatch.setattr(get_settings(), "model_service_stream_idle_timeout", 90.0)
    monkeypatch.setattr(get_settings(), "model_service_stream_max_duration", 180.0)
    control.capabilities = _valid_caps(
        stream_keepalive=True,  # 流式侧可放宽到 180s
        timeouts={
            "upstream_first_token_s": 15,
            "upstream_first_content_s": 120,
            "upstream_chunk_idle_s": 120,
            "upstream_total_s": 120,  # 非流式侧仍须 < 90s ⇒ 违规
        },
    )
    with pytest.raises(ProtocolVersionError) as ei:
        await startup_self_check()
    assert "上游总超时" in str(ei.value), (
        f"非流式总超时必须仍按空闲上限 90s 对标、不得随 keepalive 放宽：{ei.value}"
    )


# ==================================================== P1-9 对外那一半：平台自己的 SSE 错误帧
#
# 上面那组测的是 RemoteBackend（对内）。这一组测**路由层**——前端与 AstrBot/QQ 实际看到的
# 那一面。协议用 §6.7/G3-1 约束模型服务，平台对外必须守同一套形态，否则"对内修好了、
# 错误照样吞"。


def _sse_payloads(text: str) -> list[str]:
    return [ln[len("data:") :].strip() for ln in text.splitlines() if ln.startswith("data:")]


def _error_frames(payloads: list[str]) -> list[dict]:
    import json

    out = []
    for p in payloads:
        if p == "[DONE]":
            continue
        obj = json.loads(p)
        if isinstance(obj.get("error"), dict):
            out.append(obj["error"])
    return out


async def _post_sse(client, url, body, headers) -> list[str]:
    text = ""
    async with client.stream("POST", url, json=body, headers=headers) as r:
        assert r.status_code == 200, r.text
        async for ln in r.aiter_lines():
            text += ln + "\n"
    return _sse_payloads(text)


async def test_p1_9_native_stream_surfaces_error_frame(remote_mode, client, auth_headers, persona):
    """§6.7 对外那一半（原生面）：模型服务的流内错误帧必须让**前端**看得见。

    此前原生面的 `event_stream` 里**一个 try/except 都没有**：Starlette 先 `send` 响应头
    再迭代 body（`starlette/responses.py:251`）⇒ 状态码早已不可改，异常只能把流截断，
    前端拿到的是"没有 `[DONE]` 的半截流"，既无 code 也无退避时长——上游限流/超时对用户
    完全不可见（§18.2-F2 的最后一跳）。
    """
    control, _ = remote_mode
    control.stream_error = {"code": "rate_limited", "retry_after_s": 30, "retryable": True}
    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()

    payloads = await _post_sse(
        client,
        f"/api/v1/conversations/{conv['id']}/messages",
        {"content": "hi", "stream": True},
        auth_headers,
    )

    assert payloads[-1] == "[DONE]", "G3-1：错误帧之后仍须 [DONE]"
    errs = _error_frames(payloads)
    assert errs, "原生面必须把模型服务的错误透出，不能再隐身"
    assert errs[-1]["code"] == "rate_limited"
    assert errs[-1]["retry_after_s"] == 30, "帧没有 header，退避只能走 retry_after_s"
    # 错误前的内容不得丢：至少有一个 delta chunk
    assert any("delta" in p for p in payloads), "已产出的部分内容必须照常下发"


async def test_p1_9_native_absolute_cap_gives_no_error_frame(remote_mode, client, auth_headers, persona, monkeypatch):
    """§8.1.1 末句：绝对上限对外只表现为「回复中断」，**不给**错误提示。

    这条专门看守判据：路由层用的是 `error_message` 有值，而不是 `terminate_reason` 有值。
    若有人图省事改成后者，上限切断的流会莫名多出一个错误帧（用户看到报错，协议说不能）。
    """
    from app.config import get_settings

    control, _ = remote_mode
    control.silence_after_chunks = 1
    control.keepalive_during_silence = True
    control.silence_seconds = 3.0
    monkeypatch.setattr(get_settings(), "model_service_stream_idle_timeout", 30.0)
    monkeypatch.setattr(get_settings(), "model_service_stream_max_duration", 0.5)
    conv = (
        await client.post(
            "/api/v1/conversations", json={"persona_id": persona["id"]}, headers=auth_headers
        )
    ).json()

    payloads = await _post_sse(
        client,
        f"/api/v1/conversations/{conv['id']}/messages",
        {"content": "hi", "stream": True},
        auth_headers,
    )
    assert not _error_frames(payloads), "§8.1.1：绝对上限不该给对外错误帧"
    assert payloads[-1] == "[DONE]"


async def test_p1_9_compat_stream_relayed_frame_followed_by_done(remote_mode, client, auth_headers):
    """G3-1 在平台**自己**的对外契约上同样成立（兼容面，转发模型服务的流内帧）。"""
    key = await _compat_key(client, auth_headers)
    control, _ = remote_mode
    control.stream_error = {"code": "internal_error"}

    payloads = await _post_sse(
        client,
        "/v1/chat/completions",
        {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        {"Authorization": f"Bearer {key}"},
    )
    errs = _error_frames(payloads)
    assert errs and errs[-1]["code"] == "internal_error"
    assert payloads[-1] == "[DONE]", "错误帧之后必须仍发 [DONE]"


async def test_p1_9_compat_pre_first_byte_frame_followed_by_done(remote_mode, client, auth_headers):
    """兼容面异常路径（首字节前的 HTTP 429 转成流内帧）此前 `return` 掉 `[DONE]` —— 现已补上。

    协议用 G3-1 约束模型服务，而平台此前自己转发错误时 `yield` 完帧就 `return`：**自己违反
    了自己拿去验收别人的条款**。OpenAI 兼容客户端（AstrBot 等）以 `[DONE]` 判正常收尾。
    """
    key = await _compat_key(client, auth_headers)
    control, _ = remote_mode
    control.error = {"code": "rate_limited", "retry_after": 9, "retryable": True}

    payloads = await _post_sse(
        client,
        "/v1/chat/completions",
        {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        {"Authorization": f"Bearer {key}"},
    )
    errs = _error_frames(payloads)
    assert errs and errs[-1]["code"] == "rate_limited"
    assert errs[-1]["retry_after_s"] == 9, "HTTP 层的 Retry-After 在流内帧里须转成 retry_after_s"
    assert payloads[-1] == "[DONE]"


async def _compat_key(client, auth_headers) -> str:
    resp = await client.post(
        "/api/v1/me/keys", json={"name": "astrbot"}, headers=auth_headers
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["key"]


# ==================================================================================
# RB 批① 跨侧对拍 —— 起**真模型服务**（`model-service` 的真 app + 真 uvicorn 子进程）
#
# 为什么这一段非要起真服务：协议 §28.2 里 A 对 F2+F3 的裁定指出，平台旧三条心跳用例
# （def 842／1130／1458）跑的是**平台自家桩**，只证明"平台能吞注释行"，**不证明"真实服务
# 会发"** ——§18.2-F1「两端各跑自家桩」的第三次。这里的两端都是真的：服务侧用 `msvc.main.create_app()`
# 起的真 app（场景经 `tests/serve_scenario.py` 注入），平台侧用真 `RemoteBackend` 走真 TCP。
# 时序类断言（心跳是否在正文之前到达）也只有真连接能测：`httpx.ASGITransport` 会攒完整个响应体。
# ==================================================================================
import json as _json  # noqa: E402  (本节局部导入，避免动文件头的既有 import 顺序)
import os as _os
import socket as _socket
import subprocess as _subprocess
import time as _time
import urllib.request as _urllib  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_MS_DIR = _Path(__file__).resolve().parents[2] / "model-service"
_SERVE = _MS_DIR / "tests" / "serve_scenario.py"
SERVICE_TOKEN = "rb-real-service-token"


def _free_port() -> int:
    with contextlib.closing(_socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def real_model_service(*, scenario=None, env=None):
    """起真 model-service（真 app + 真 uvicorn 子进程），返回 base_url。"""
    port = _free_port()
    child = dict(_os.environ)
    child.update({
        "MS_SERVICE_TOKENS": SERVICE_TOKEN,
        "MS_UPSTREAM": "fake",
        "MS_SERVE_PORT": str(port),
        "PYTHONPATH": str(_MS_DIR),
        "PYTHONIOENCODING": "utf-8",
    })
    if scenario:
        child["MS_SCENARIO_JSON"] = _json.dumps(scenario)
    child.update(env or {})
    proc = _subprocess.Popen([_sys_executable(), str(_SERVE)], env=child,
                             stdout=_subprocess.PIPE, stderr=_subprocess.STDOUT,
                             text=True, errors="replace")  # 控制台默认 GBK，服务日志里的中文会炸解码
    base = f"http://127.0.0.1:{port}"
    deadline = _time.monotonic() + 30
    try:
        while _time.monotonic() < deadline:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                raise AssertionError(f"真模型服务进程提前退出：{out[-900:]}")
            try:
                with _urllib.urlopen(f"{base}/health/liveness", timeout=1):  # noqa: S310
                    break
            except OSError:
                _time.sleep(0.1)
        else:
            raise AssertionError("真模型服务 30s 内没起来")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=6)
        except _subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


def _sys_executable() -> str:
    import sys

    return sys.executable


@pytest.fixture
def real_service_mode(monkeypatch):
    """把平台切到 remote/native，token 与真服务一致；不动 base_url（由用例指向真服务）。"""
    from app.config import get_settings
    from app.llm.remote import breaker

    s = get_settings()
    monkeypatch.setattr(s, "llm_backend", "remote")
    monkeypatch.setattr(s, "model_service_token", SERVICE_TOKEN)
    monkeypatch.setattr(s, "model_service_version", 1)
    monkeypatch.setattr(s, "model_service_mode", "native")
    monkeypatch.setattr(s, "model_service_fallback_direct", "")
    breaker.reset()
    yield s
    breaker.reset()


async def test_rb2_real_service_emits_keepalive_before_content_and_platform_swallows_it(
    real_service_mode,
):
    """RB-2④（§28.2 判据／"真实服务也发"的正向补位）：

    ① 直读 SSE：上游只推理的那段时间里，客户端必须先看到 ≥1 行 `:` 注释行，**那时还没有正文**；
    ② 平台 `RemoteBackend` 对着同一个真服务跑流式：注释行被吞、正文收全、无错误归因。

    mutation＝删掉服务侧 `event_gen` 里心跳那条 yield ⇒ **本用例红**；而平台旧三条（跑自家桩的）
    **仍在桩上全绿**——那正是 A 说的"只被自家桩看守"。
    """
    with real_model_service(
        scenario={"reasoning_only_chunks": 10, "reasoning_chunk_interval": 0.2}
    ) as base:
        # ① 真字节流：注释行必须在正文之前
        comments_before_content = 0
        content_seen = False
        async with httpx.AsyncClient(timeout=30, trust_env=False, headers={
            "Authorization": f"Bearer {SERVICE_TOKEN}"
        }) as c:
            async with c.stream(
                "POST", f"{base}/v1/chat/completions",
                json={"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}],
                      "stream": True},
            ) as r:
                assert r.status_code == 200, await r.aread()
                async for line in r.aiter_lines():
                    if line.startswith(":"):
                        if not content_seen:
                            comments_before_content += 1
                    elif _data_line_has_content(line):
                        content_seen = True
        assert comments_before_content >= 1, (
            "真实服务在推理期一条注释行都没发 ⇒ §6-4『推理期间必须发』只在桩里有、服务里没有"
        )
        assert content_seen, "正文最终必须到达"

        # ② 平台吞注释行并拿到完整正文（真服务、真 TCP，不是自家桩）
        real_service_mode.model_service_base_url = base
        parts, done = await _drain_done(
            RemoteBackend().chat_stream([{"role": "user", "content": "hi"}], "deepseek-chat")
        )
        assert "".join(parts) == "这是 模型服务的流式回复。", parts
        assert done is not None and done.terminate_reason is None, done


def _data_line_has_content(line: str) -> bool:
    if not line.startswith("data: "):
        return False
    try:
        obj = _json.loads(line[6:])
    except Exception:
        return False
    for ch in obj.get("choices") or []:
        if (ch.get("delta") or {}).get("content"):
            return True
    return False


async def test_rb1_platform_sees_reasoning_timeout_as_distinct_code_and_counts_once(real_service_mode):
    """RB-1 跨侧：服务侧因"只推理不出正文"发流内错误帧 ⇒ 平台按 §7 把它当成**独立 code**
    处理（504、计入熔断一次、内容按已收结算不 raise）。

    这一条同时防两件事：① 服务把归因写错（写回 `upstream_timeout` 就红）；
    ② 平台把未知 code 落进"未知码"分支翻成 502（§7 表要求 504）。
    """
    from app.llm.remote import _classify_error_code

    assert _classify_error_code("upstream_reasoning_timeout") == (504, False, True), (
        "§7 表新行未在平台映射表落地"
    )
    with real_model_service(
        scenario={"reasoning_only_chunks": 400, "reasoning_chunk_interval": 0.02},
        # 内容预算必须 ≥ 首 token 预算（§28.1-2，启动期断言会拒），否则服务根本起不来。
        env={"MS_UPSTREAM_FIRST_TOKEN_TIMEOUT": "0.3",
             "MS_UPSTREAM_FIRST_CONTENT_TIMEOUT": "0.6",
             "MS_UPSTREAM_CHUNK_IDLE_TIMEOUT": "30"},
    ) as base:
        real_service_mode.model_service_base_url = base
        parts, done = await _drain_done(
            RemoteBackend().chat_stream([{"role": "user", "content": "hi"}], "deepseek-chat")
        )
        assert done is not None
        assert done.terminate_reason == "upstream_reasoning_timeout", done
        assert done.error_retryable is False
        assert not parts, f"推理期不该有正文：{parts}"
        assert breaker._consecutive_failures == 1, (
            "§7 表：reasoning 超时计入熔断，且只计一次"
        )


async def test_rb5_replayed_failure_is_not_counted_again_by_platform(real_service_mode):
    """RB-5④（§8.2.1 连带项）：同一条消息重发命中幂等窗 ⇒ 服务标 `Idempotency-Replayed: true`
    ⇒ 平台**不重复计熔断**。不带标记时照旧计数（两条方向都要，防一刀切屏蔽计数）。

    真实场景：`cb_threshold=5`，一次上游故障后客户端抖动重发 5 次就会把整站打进降级——
    而实际发生过的上游故障只有一次。
    """
    from app.llm.remote import breaker

    with real_model_service(
        scenario={"error": {"code": "upstream_error", "retryable": False}},
        env={"MS_IDEMPOTENCY_ERROR_TTL_SECONDS": "300", "MS_IDEMPOTENCY_WINDOW_SECONDS": "600"},
    ) as base:
        real_service_mode.model_service_base_url = base
        be = RemoteBackend()
        key = "rb5-same-key"

        # ① 第一次：真实调上游并失败 ⇒ 计一次
        with pytest.raises(ModelServiceError):
            await be.chat([{"role": "user", "content": "hi"}], "deepseek-chat", idempotency_key=key)
        assert breaker._consecutive_failures == 1

        # ② 同 key 重发（在 error TTL 内）⇒ 服务回放、平台不计数
        with pytest.raises(ModelServiceError):
            await be.chat([{"role": "user", "content": "hi"}], "deepseek-chat", idempotency_key=key)
        assert breaker._consecutive_failures == 1, (
            "回放被再计一次熔断 = §8.2.1 连带项未解：同一条消息重发 cb_threshold 次即全站降级"
        )

        # ③ 换 key（真实新调用）⇒ 必须照常计数，证明 ② 不是"把计数整体关掉"
        with pytest.raises(ModelServiceError):
            await be.chat([{"role": "user", "content": "hi"}], "deepseek-chat",
                          idempotency_key="rb5-other-key")
        assert breaker._consecutive_failures == 2


async def test_rb5_service_marks_replay_header_on_http_error(real_service_mode):
    """回放标记的**服务端**证据：同一 Idempotency-Key 第二次拿到的响应带
    `Idempotency-Replayed: true`，第一次没有（RB-5③ 的最小加法）。"""
    with real_model_service(
        scenario={"error": {"code": "upstream_error", "retryable": False}},
        env={"MS_IDEMPOTENCY_ERROR_TTL_SECONDS": "300"},
    ) as base:
        h = {"Authorization": f"Bearer {SERVICE_TOKEN}", "Idempotency-Key": "rb5-header-check"}
        body = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}
        async with httpx.AsyncClient(timeout=20, trust_env=False) as c:
            r1 = await c.post(f"{base}/v1/chat/completions", json=body, headers=h)
            r2 = await c.post(f"{base}/v1/chat/completions", json=body, headers=h)
        assert r1.status_code == r2.status_code == 502, (r1.status_code, r2.status_code)
        assert "idempotency-replayed" not in {k.lower() for k in r1.headers}, r1.headers
        assert r2.headers.get("idempotency-replayed") == "true", r2.headers


async def test_rb5_frame_replay_flag_is_unit_lock(real_service_mode):
    """单元层再锁一次流内帧那条支路（§6.7 的帧与 HTTP 错误同等处理，但回放副本不计数）。"""
    from app.llm.remote import _error_from_frame, breaker

    breaker.reset()
    _error_from_frame({"code": "upstream_timeout", "message": "x", "retryable": False},
                      replayed=False)
    assert breaker._consecutive_failures == 1
    _error_from_frame({"code": "upstream_timeout", "message": "x", "retryable": False},
                      replayed=True)
    assert breaker._consecutive_failures == 1, "replayed=True 仍计数 ⇒ RB-5④ 没落地"
    _error_from_frame({"code": "rate_limited", "message": "x", "retryable": True}, replayed=False)
    assert breaker._consecutive_failures == 1, "§11：429 类不计数，加了 replayed 也不该变"


