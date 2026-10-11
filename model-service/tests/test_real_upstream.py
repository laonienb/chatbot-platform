"""真实上游（LiteLLMProvider）与 SW-1/SW-2/SW-4/SW-5 的防线用例。

为什么单独一个文件、且**不用 FakeUpstream**：协议 §24.2 R4 与工作单 SW-4 指出，
既有 40 条契约测试全部跑 `FakeUpstream`（`provider.py` 曾整段 `# pragma: no cover`），
而 `_map_upstream_error` 只认假桩的 `_ScenarioError` ⇒ 真实 litellm 异常族**全部逃逸**，
基线却照样全绿（**双向皆盲**）。本文件的被测对象是 `LiteLLMProvider` 本体与 handler 的
catch-all：以注入的假 `litellm` 模块为边界（离线、确定性），不退回假桩。
"""

import asyncio
import json
import sys
import types

import pytest

from msvc.idempotency import IdempotencyWindow
from msvc.provider import LiteLLMProvider, Scenario, UpstreamResult
from msvc.errors import ServiceError, from_upstream_exception


# ---------------------------------------------------------------- 注入的假 litellm 边界
class _RateLimitErr(Exception):
    status_code = 429

    def __init__(self, message="upstream rate limit", retry_after=7):
        self.retry_after = retry_after
        super().__init__(message)


class _AuthErr(Exception):
    status_code = 401

    def __init__(self, message="invalid api key"):
        super().__init__(message)


class _ContextErr(Exception):
    status_code = 400

    def __init__(self, message="This model's maximum context length is 8192 tokens"):
        super().__init__(message)


def _install_fake_litellm(monkeypatch, *, chunks=None, raise_at_call=None, raise_midway=None,
                          cost_ok=True):
    """把假 litellm 注入 sys.modules：provider 内 `import litellm` 取到的就是它。

    `raise_midway` 是 `(第 N 个 chunk 之前, 异常实例)` 二元组。
    ⚠️ 曾经只传异常实例，而判断写作 `i >= raise_midway` ⇒ int 与异常比较当场 TypeError，
    错误在**首字节前**就发生 ⇒ 得到 502，测试验的其实不是"首字节后"这条路径
    （自查发现并修正——这类"自己测自己的桩"的坑正是 R4 双向盲区同款）。
    """
    mod = types.ModuleType("litellm")
    calls: list[dict] = []

    def _chunks():
        seq = chunks if chunks is not None else [
            {"model": "deepseek/deepseek-chat",
             "choices": [{"index": 0, "delta": {"content": "你好"}, "finish_reason": None}]},
            {"model": "deepseek/deepseek-chat",
             "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
            {"model": "deepseek/deepseek-chat", "choices": [],
             "usage": {"prompt_tokens": 30, "completion_tokens": 9, "total_tokens": 39,
                       "prompt_tokens_details": {"cached_tokens": 4}}},
        ]
        return seq

    async def acompletion(**kw):
        calls.append(kw)
        if raise_at_call is not None:
            raise raise_at_call
        if kw.get("stream"):
            async def gen():
                for i, c in enumerate(_chunks()):
                    if raise_midway is not None and i >= raise_midway[0]:
                        raise raise_midway[1]
                    yield c
            return gen()
        return {
            "id": "chatcmpl-1", "model": "deepseek/deepseek-chat",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "你好"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 9, "total_tokens": 39},
        }

    def cost_per_token(**kw):
        if not cost_ok:
            raise KeyError("no price entry")
        return (0.0000003, 0.0000012)

    mod.acompletion = acompletion
    mod.cost_per_token = cost_per_token
    mod.exceptions = types.SimpleNamespace(RateLimitError=_RateLimitErr, AuthenticationError=_AuthErr)
    monkeypatch.setitem(sys.modules, "litellm", mod)
    return mod, calls


def _body(**kw):
    b = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}
    b.update(kw)
    return b


async def _collect(agen):
    return [ev async for ev in agen]


# ================================================================ SW-1 provider 本体
async def test_sw1_provider_stream_forwards_chunks_finish_usage_terminate(monkeypatch):
    """SW-1：真实 provider 必须逐块转发，并按 §6.6 顺序发 finish(非 null) → usage → 终止事件。"""
    _install_fake_litellm(monkeypatch)
    events = await _collect(LiteLLMProvider().stream(_body(), Scenario()))
    kinds = [e["kind"] for e in events]

    assert kinds == ["chunk", "finish", "usage", "terminate", "done"]
    assert events[0]["content"] == "你好"
    # 收尾 chunk 必须携带**非 null** finish_reason，且位于 usage **之前**（P1-8）
    assert events[1]["finish_reason"] == "stop"
    assert kinds.index("finish") < kinds.index("usage") < kinds.index("terminate")
    assert events[2]["usage"]["total_tokens"] == 39
    xms = events[3]["x_model_service"]
    assert xms["model_used"] == "deepseek/deepseek-chat"
    assert xms["cost_status"] == "exact"


async def test_sw1_provider_no_cost_never_zero(monkeypatch):
    """红线2：算不出成本 → null + unknown，**绝不**是 0（真实 provider 路径同样守得住）。"""
    _install_fake_litellm(monkeypatch, cost_ok=False)
    events = await _collect(LiteLLMProvider().stream(_body(), Scenario()))
    xms = [e for e in events if e["kind"] == "terminate"][0]["x_model_service"]
    assert xms["cost_usd"] is None
    assert xms["cost_status"] == "unknown"
    assert xms["cost_usd"] != 0 and xms["cost_usd"] != "0"


async def test_sw1_provider_complete_maps_real_error_not_bare(monkeypatch):
    """非流式：真实 litellm 异常必须带 §7 code（此前裸抛 → 500 无 code）。"""
    _install_fake_litellm(monkeypatch, raise_at_call=_AuthErr())
    with pytest.raises(ServiceError) as ei:
        await LiteLLMProvider().complete(_body(), Scenario())
    assert ei.value.code == "upstream_auth_failed"
    assert ei.value.http_status == 401


async def test_sw1_service_stream_end_to_end(client, app, monkeypatch):
    """经完整 ASGI 链跑真实 provider：SSE 含内容 / finish_reason chunk / usage / 终止事件 / [DONE]。"""
    _install_fake_litellm(monkeypatch)
    app.state.upstream = LiteLLMProvider()
    async with client.stream("POST", "/v1/chat/completions", json=_body(stream=True)) as r:
        assert r.status_code == 200
        text = ""
        async for line in r.aiter_lines():
            text += line + "\n"
    assert "你好" in text
    assert '"finish_reason": "stop"' in text
    assert '"usage"' in text
    assert "event: model_service" in text
    assert text.index('"usage"') < text.index("event: model_service") < text.index("[DONE]")


# ================================================================ SW-1 错误路径（§6.7 分界）
async def test_t12_pre_first_byte_error_returns_http_status(client, app, monkeypatch):
    """T12① / §6.7：首字节**前**的上游错误必须返回 HTTP 错误状态，不得是「200 + 流内错误帧」。

    命名对齐协议 §10 的 T 编号（P0-1 验收①「用例名可追溯到 T 编号」）——服务侧此前**没有**
    T12 用例（§10 要求两侧各配），本文件补上。
    """
    _install_fake_litellm(monkeypatch, raise_at_call=_AuthErr())
    app.state.upstream = LiteLLMProvider()
    async with client.stream("POST", "/v1/chat/completions", json=_body(stream=True)) as r:
        body = (await r.aread()).decode()
    assert r.status_code == 401, "首字节前失败必须以 HTTP 状态返回（否则平台退避/熔断/归因全失效）"
    assert json.loads(body)["error"]["code"] == "upstream_auth_failed"
    assert "[DONE]" not in body  # 不是流内收尾


async def test_t12_post_first_byte_error_frame_then_done(client, app, monkeypatch):
    """T12②③ / §6.7：首字节**后**的错误 → 流内 `data:{"error":…}` 帧，其后**仍须** `[DONE]`（G3-1）。"""
    _install_fake_litellm(monkeypatch, raise_midway=(1, _AuthErr()))  # 先给 1 个 chunk，再炸
    app.state.upstream = LiteLLMProvider()
    app.state.scenario = Scenario()
    async with client.stream("POST", "/v1/chat/completions", json=_body(stream=True)) as r:
        lines = [line async for line in r.aiter_lines()]
    text = "\n".join(lines)
    assert r.status_code == 200, "首字节已过，状态码不可再改"
    assert "你好" in text, "已产出的部分内容必须照常下发"
    assert '"error"' in text and "upstream_auth_failed" in text
    # G3-1：错误帧之后必须仍以 `[DONE]` 收尾，且**最后一个** data 帧就是 [DONE]
    data_frames = [l for l in lines if l.startswith("data: ")]
    assert data_frames[-1] == "data: [DONE]", f"错误帧后缺 [DONE]：{data_frames[-3:]}"
    assert text.index('"error"') < text.rindex("[DONE]")


async def test_sw1_rate_limit_midstream_carries_retry_after_s(client, app, monkeypatch):
    """SW-2：流内 `rate_limited` 帧在**有上游值**时带 `retry_after_s`（流式唯一退避通道），
    且 `retryable=true`（§8.2.1 上游瞬时拒绝）。"""
    _install_fake_litellm(monkeypatch, raise_midway=(1, _RateLimitErr(retry_after=7)))
    app.state.upstream = LiteLLMProvider()
    app.state.scenario = Scenario()
    async with client.stream("POST", "/v1/chat/completions", json=_body(stream=True)) as r:
        lines = [line async for line in r.aiter_lines()]
    frame_line = [l for l in lines if l.startswith("data: {") and '"error"' in l][0]
    frame = json.loads(frame_line[len("data: ") :])
    assert frame["error"]["code"] == "rate_limited"
    assert frame["error"]["retryable"] is True, "上游瞬时拒绝必须 retryable=true（§8.2.1）"
    assert frame["error"]["retry_after_s"] == 7, "流内帧没有 header，退避只能走 retry_after_s"
    assert [l for l in lines if l.startswith("data: ")][-1] == "data: [DONE]"


# ================================================================ SW-4 在飞位必须释放
class _RaisingUpstream:
    """FakeUpstream **之外**的抛出型上游：模拟真实 provider 抛未映射异常（R4 现场）。"""

    name = "raising"

    def __init__(self, exc_factory):
        self._exc_factory = exc_factory
        self.started = 0

    async def complete(self, body, scenario):
        self.started += 1
        await asyncio.sleep(0)
        raise self._exc_factory()

    async def stream(self, body, scenario):
        self.started += 1
        await asyncio.sleep(0)
        raise self._exc_factory()
        yield {"kind": "done"}  # pragma: no cover


@pytest.mark.parametrize("stream", [False, True], ids=["nonstream", "stream"])
@pytest.mark.parametrize("factory,label", [
    (lambda: RuntimeError("boom"), "unexpected"),
    (lambda: _RateLimitErr(), "litellm-429"),
], ids=["unknown-exc", "litellm-rate-limit"])
async def test_sw4_unmapped_exception_releases_inflight_and_codes(app, client, stream, factory, label):
    """SW-4 核心：注入**非 `_ScenarioError`** 异常 ⇒
    ① 响应带 §7 错误体（不裸 500）；② 同 key 第二次**不是 409**；③ `_inflight` 为空。

    这正是 R4 的复现路径：此前异常在 `_finish_window` 之前逃出 ⇒ 在飞位永久泄漏 ⇒
    兼容面指纹键下同一条消息在进程重启前恒 409。
    """
    app.state.upstream = _RaisingUpstream(factory)
    window = app.state.window
    window.clear()
    h = {"Idempotency-Key": f"idem-{label}-{stream}"}

    r1 = await client.post("/v1/chat/completions", json=_body(stream=stream), headers=h)
    err = r1.json()["error"]
    assert "code" in err, "错误体必须带机器可判 code（§7），不得是裸 500 detail"
    assert err["code"] != "", err
    assert r1.status_code in (429, 502, 500, 504, 503), r1.status_code

    # ③ 在飞位已释放
    assert h["Idempotency-Key"] not in window._inflight, "在飞位必须在任何退出路径释放"
    # ② 第二次不得 409（要么回放缓存的终态，要么重新走上游）
    r2 = await client.post("/v1/chat/completions", json=_body(stream=stream), headers=h)
    assert r2.status_code != 409, f"同 key 第二次被在飞位堵成 409（R4 复现）：{r2.status_code}"


async def test_sw4_transient_429_not_cached_allows_retry(app, client):
    """§8.2.1：上游 429（retryable=true）不入窗 ⇒ 恢复后同 key 重发**真的重新调上游**。"""
    state = {"fail": True}

    class Flaky:
        name = "flaky"

        def __init__(self):
            self.started = 0

        async def complete(self, body, scenario):
            self.started += 1
            if state["fail"]:
                raise from_upstream_exception(_RateLimitErr())
            return UpstreamResult(content="ok", usage={"prompt_tokens": 1, "completion_tokens": 1,
                                                       "total_tokens": 2}, x_model_service={})

        async def stream(self, body, scenario):  # pragma: no cover
            raise AssertionError("unused")
            yield

    up = Flaky()
    app.state.upstream = up
    app.state.window.clear()
    h = {"Idempotency-Key": "idem-flaky"}
    r1 = await client.post("/v1/chat/completions", json=_body(), headers=h)
    assert r1.status_code == 429 and r1.json()["error"]["code"] == "rate_limited"
    state["fail"] = False
    r2 = await client.post("/v1/chat/completions", json=_body(), headers=h)
    assert r2.status_code == 200, "瞬时拒绝未入窗 → 退避后重发应成功（入窗会放大成窗口级故障）"
    assert up.started == 2


# ================================================================ SW-5 在飞位 TTL 兜底
def test_sw5_stale_inflight_is_released():
    w = IdempotencyWindow(600, inflight_ttl_seconds=30.0)
    assert w.begin("k") is True
    assert w.begin("k") is False, "新鲜在飞位必须拦住并发（§8.2.2-2）"
    # 时间拨到陈旧（等价于漏放 finally 的路径）
    w._inflight["k"] -= 31.0
    assert w.begin("k") is True, "陈旧在飞位不得永久阻断同 key（SW-5 兜底）"


def test_sw5_finish_releases_before_any_cache_work():
    """`finish()` 先清在飞位再做入窗记账：即便缓存逻辑抛异常也不会卡成永久 409。"""
    w = IdempotencyWindow(600, inflight_ttl_seconds=60.0)
    w.begin("k")
    import msvc.idempotency as idem_mod

    real_size = idem_mod._size

    def boom(_):
        raise ValueError("cache bookkeeping exploded")

    idem_mod._size = boom
    try:
        w.finish("k", idem_mod.CachedResponse(200, {}, {}, 0))
    except Exception:
        pass
    finally:
        idem_mod._size = real_size
    assert "k" not in w._inflight


# ================================================================ SW-2 错误体 retry_after_s
def test_sw2_retry_after_s_only_when_upstream_gave_value():
    """HTTP 层：有上游退避值才给 `retry_after_s` + `Retry-After`；缺省整键不出现（不得造 0）。"""
    with_val = ServiceError("rate_limited", "慢点", retry_after=9, retryable=True).to_response()
    payload = json.loads(with_val.body)["error"]
    assert payload["retry_after_s"] == 9
    assert with_val.headers["Retry-After"] == "9"

    without = ServiceError("internal_error", "炸了").to_response()
    payload2 = json.loads(without.body)["error"]
    assert "retry_after_s" not in payload2
    assert "Retry-After" not in without.headers


# ================================================================ §7 异常族映射矩阵
@pytest.mark.parametrize("exc,code,status,retryable", [
    (_RateLimitErr(), "rate_limited", 429, True),
    (_AuthErr(), "upstream_auth_failed", 401, False),
    (_ContextErr(), "context_length_exceeded", 413, False),
    (type("APITimeoutError", (Exception,), {"status_code": 504})("t"), "upstream_timeout", 504, False),
    (type("ServiceUnavailableError", (Exception,), {"status_code": 503})("s"), "service_unavailable", 503, True),
    (type("InternalServerError", (Exception,), {"status_code": 500})("i"), "internal_error", 500, False),
    (type("APIConnectionError", (Exception,), {})("c"), "upstream_error", 502, True),
    (RuntimeError("totally unknown"), "upstream_error", 502, False),
])
def test_sw4_mapping_matrix(exc, code, status, retryable):
    err = from_upstream_exception(exc)
    assert err.code == code
    assert err.http_status == status
    assert err.retryable == retryable


def test_sw4_existing_service_error_passes_through():
    original = ServiceError("compliance_denied", "合规否决", retryable=False)
    assert from_upstream_exception(original) is original
