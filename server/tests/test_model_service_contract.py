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
    """T9b：版本一致 → 自检通过（且确实调了 /v1/capabilities）。"""
    from app.llm.model_service_ops import startup_self_check

    control, _ = remote_mode
    control.capabilities = {"protocol_version": 1, "features": {"cost_attribution": True}}
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
        "features": {},
        # 内层 120s ≥ 外层 60s → 违反红线 8
        "timeouts": {"upstream_first_token_s": 15, "upstream_total_s": 120},
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
        "features": {},
        "timeouts": {"upstream_first_token_s": 15, "upstream_total_s": 60},
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

