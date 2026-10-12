"""RB-1／RB-2／RB-5 的防线用例（协议 §28.1／§28.2／§28.3，工单 §9.5 批①）。

| 项 | 锁住什么 | 用例 |
|---|---|---|
| RB-1 | 首 token 计时由**上游任何 chunk** 复位；新增「首个正文 token」预算，且 `code` 与首 token 超时**区分归因** | `test_rb1_*` |
| RB-2 | 推理期对外**必须**出现 `:` 注释行（真 TCP 上可观测到时序）；心跳**不得进终态缓存** | `test_rb2_*` |
| RB-5 | 入窗**时长分类**（成功/永久拒绝长窗、"结果不明"短窗）＋ 回放带 `Idempotency-Replayed` | `test_rb5_*` |

流式**时序**类断言走真 uvicorn（`running_server`）：`httpx.ASGITransport` 会把 ASGI 响应体
整条缓冲后才返回，"心跳是否在正文之前到达"在它上面根本测不出来（协议 §28.2／§27.2 取证注）。
结果类断言（状态码、错误帧、入窗行为）用 ASGI 直连即可，快且不需要端口。
"""

import asyncio
import contextlib
import json
import os
import sys
import threading
import time
import types

import httpx
import pytest
import uvicorn
from pydantic import ValidationError

from msvc.config import Settings, get_settings
from msvc.errors import CODE_HTTP, ServiceError
from msvc.main import create_app
from msvc.provider import LiteLLMProvider, Scenario

# 用默认目录里的 id（目录校验是 §9.5 项3/A-1 的事，本文件不测它）：
# 冒烟那个 `mimo-v2.6-flash` 不在默认目录 ⇒ 会先撞 404 model_not_found，看不到本批防线。
MODEL = "deepseek-chat"
TOKEN = Settings().service_tokens.split(",")[0].strip()
AUTH = {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------- 按 env 建服务
@pytest.fixture
def ms_env():
    """设 `MS_*` env 并清配置缓存；teardown 还原。

    ⚠️ 必须在**建 app 之前**设好：`create_app()` 会用当时的 settings 构造幂等窗
    （`window.error_ttl_seconds` 就是那时定下来的），先建后改 env ⇒ 改的是空气。
    """
    saved = dict(os.environ)

    def _set(**env: str):
        for k, v in env.items():
            os.environ[k] = v
        get_settings.cache_clear()

    yield _set

    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def _new_app(**env):
    """按给定 env 建一个 app（调用期内保留 env，返回后还原并清缓存）。"""
    saved = dict(os.environ)
    for k, v in env.items():
        if v == "":
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()
    try:
        app = create_app()
    finally:
        os.environ.clear()
        os.environ.update(saved)
        # 不在此处清缓存：handler 请求期还会 get_settings()，env 已还原 ⇒ 由用例自己控制生命周期
    app.state.scenario = Scenario()
    app.state.window.clear()
    return app


@contextlib.contextmanager
def running_server(**env):
    """真 uvicorn 线程（端口 0），返回 (base_url, app)。env 在 with 期间保持生效。"""
    app = _new_app(**env)
    cfg = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    server = uvicorn.Server(cfg)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not server.started:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        raise AssertionError("真 uvicorn 没起来")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", app
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _body(**kw):
    b = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}
    b.update(kw)
    return b


async def _asgi_read(app, *, headers=None, body=None, key=None):
    """走完整 ASGI 链读一条流：返回 (status, 原文, 错误帧列表, 响应头)。"""
    h = dict(AUTH)
    h.update(headers or {})
    if key:
        h["Idempotency-Key"] = key
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ms", headers=h, timeout=30
    ) as c:
        async with c.stream("POST", "/v1/chat/completions", json=body or _body(stream=True)) as r:
            status = r.status_code
            text = (await r.aread()).decode("utf-8", "replace")
            hdrs = dict(r.headers)
    frames = [
        json.loads(line[6:])["error"]
        for line in text.splitlines()
        if line.startswith("data: {") and '"error"' in line
    ]
    return status, text, frames, hdrs


# ---------------------------------------------------------------- 假 litellm：可编程 reasoning 节奏
def _install_reasoning_litellm(monkeypatch, *, reasoning_chunks: int, interval: float,
                               content_after: bool = True, content_chunks: int = 3):
    """先吐 `reasoning_chunks` 个**只推理**的 chunk（逐个 `await asyncio.sleep(interval)`），
    再（可选）吐正文 chunk。被测对象始终是 `LiteLLMProvider` 本体。
    """
    mod = types.ModuleType("litellm")
    calls: list[dict] = []

    async def acompletion(**kw):
        calls.append(kw)
        if not kw.get("stream"):
            raise AssertionError("本文件的用例都走流式")

        async def gen():
            for _ in range(reasoning_chunks):
                await asyncio.sleep(interval)
                yield {"model": MODEL, "choices": [
                    {"index": 0, "delta": {"reasoning_content": "想一想"}, "finish_reason": None}
                ]}
            if content_after:
                for i in range(content_chunks):
                    yield {"model": MODEL, "choices": [
                        {"index": 0, "delta": {"content": f"正文{i}"}, "finish_reason": None}
                    ]}
                yield {"model": MODEL, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                yield {"model": MODEL, "choices": [], "usage": {
                    "prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33
                }}
        return gen()

    mod.acompletion = acompletion
    mod.cost_per_token = lambda **kw: (_ for _ in ()).throw(KeyError("no price"))
    mod.exceptions = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "litellm", mod)
    return mod, calls


# ================================================================ RB-1 首 token 计时复位
async def test_rb1_reasoning_before_content_does_not_504(monkeypatch, ms_env):
    """§28.1 正向：reasoning chunk 每个间隔 < 首 token 预算、**累计 > 首 token 预算** ⇒
    不得 504，正文必须收全。

    旧实现（只在 `delta.content` 为真时 yield）下整个推理期对外没有事件 ⇒ 首 token 计时
    不复位 ⇒ 0.5s 累计 > 0.2s 预算 ⇒ 504 `upstream_timeout`。这就是 mutation 要红的那条。
    """
    ms_env(MS_UPSTREAM_FIRST_TOKEN_TIMEOUT="0.2", MS_UPSTREAM_FIRST_CONTENT_TIMEOUT="60")
    _install_reasoning_litellm(monkeypatch, reasoning_chunks=10, interval=0.05)
    app = _new_app()
    app.state.upstream = LiteLLMProvider()
    status, text, frames, _ = await _asgi_read(app)
    assert status == 200, text[:400]
    assert not frames, f"推理期不该有错误帧：{frames}"
    assert "正文0" in text and "正文2" in text, "正文必须收全"
    assert text.rstrip().endswith("[DONE]")


async def test_rb1_reasoning_only_fails_with_distinct_code(monkeypatch, ms_env):
    """§28.1 反向：只推理、始终不出正文 ⇒ 超「首个正文 token」预算必须失败，且 `code`
    **不等于** `upstream_timeout` —— 区分归因是本项的全部意义。

    传输形态按 §6.7：心跳已写出 ⇒ 首字节已过 ⇒ 以**流内错误帧**收尾并仍发 `[DONE]`
    （状态码不可再改）。"504"那一半由 `CODE_HTTP` 映射用例锁死。
    """
    ms_env(MS_UPSTREAM_FIRST_TOKEN_TIMEOUT="0.2", MS_UPSTREAM_FIRST_CONTENT_TIMEOUT="0.5",
           MS_UPSTREAM_CHUNK_IDLE_TIMEOUT="30")
    _install_reasoning_litellm(monkeypatch, reasoning_chunks=400, interval=0.02, content_after=False)
    app = _new_app()
    app.state.upstream = LiteLLMProvider()
    t0 = time.monotonic()
    status, text, frames, _ = await _asgi_read(app)
    elapsed = time.monotonic() - t0
    assert status == 200, "首字节（心跳）已过，状态码不能再改"
    assert frames, f"只推理不出正文必须失败收尾，不得静默成功：{text[:300]}"
    err = frames[0]
    assert err["code"] == "upstream_reasoning_timeout", err
    assert err["code"] != "upstream_timeout", "与首 token 超时混成一条 ⇒ §28.1 的区分归因白做"
    assert err["retryable"] is False
    assert ": keepalive" in text, "推理期一条心跳都没有"
    assert text.rstrip().endswith("[DONE]"), "错误帧后必须仍发 [DONE]（G3-1）"
    assert elapsed < 5, f"不该挂到 chunk 空闲预算(30s)才失败，实测 {elapsed:.1f}s"


async def test_rb1_silent_upstream_still_reports_upstream_timeout(monkeypatch, ms_env):
    """归因的另一侧：上游**一个 chunk 都没吐** ⇒ 仍是 `upstream_timeout`（不是 reasoning 超时）——
    我们无从知道它在推理。两条形代码合起来才叫"区分归因"，只写一条就是一刀切。
    """
    mod = types.ModuleType("litellm")

    async def acompletion(**kw):
        async def gen():
            await asyncio.sleep(5)  # 永不产出事件
            yield {"model": MODEL, "choices": []}
        return gen()

    mod.acompletion = acompletion
    mod.cost_per_token = lambda **kw: (0, 0)
    mod.exceptions = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "litellm", mod)
    ms_env(MS_UPSTREAM_FIRST_TOKEN_TIMEOUT="0.2", MS_UPSTREAM_FIRST_CONTENT_TIMEOUT="0.5")
    app = _new_app()
    app.state.upstream = LiteLLMProvider()
    status, text, frames, _ = await _asgi_read(app)
    assert status == 504, f"首字节前的失败必须回到 HTTP 状态（§6.7）：{status} {text[:200]}"
    assert frames == [] and "upstream_timeout" in text


def test_rb1_new_budget_maps_to_504_and_defaults_ordered():
    """§7 表：`upstream_reasoning_timeout` 的 HTTP 状态是 504；§28.1-2 的先后关系由默认值满足。"""
    assert CODE_HTTP["upstream_reasoning_timeout"] == 504
    s = Settings()
    assert s.upstream_first_content_timeout == 60.0  # A 定的默认值（默认≠契约值）
    assert s.upstream_first_content_timeout >= s.upstream_first_token_timeout


def test_rb1_config_rejects_content_budget_smaller_than_first_token(monkeypatch):
    """§28.1-2：首个正文 token 是**外层**，配得比首 token 还小 ⇒ 永不可达 ⇒ 拒绝启动。"""
    monkeypatch.setenv("MS_UPSTREAM_FIRST_TOKEN_TIMEOUT", "15")
    monkeypatch.setenv("MS_UPSTREAM_FIRST_CONTENT_TIMEOUT", "3")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError) as ei:
            Settings()
        assert "§28.1" in str(ei.value)
    finally:
        get_settings.cache_clear()


# ================================================================ RB-2 心跳（真 TCP 时序）
async def test_rb2_comment_lines_arrive_before_any_content():
    """§28.2 判据：真 uvicorn + 真 TCP 上，上游只推理的那段时间里客户端必须收到 ≥1 行
    以 `:` 开头的注释行，且**那时还没有 `data:` 正文**。

    这是"真实服务会发"的正向锁：平台旧三条用例跑的是平台自家桩，只证"平台能吞"
    （§18.2-F1 第三次）。mutation＝删掉 `event_gen` 里心跳那条 yield ⇒ 本用例红。
    """
    with running_server() as (base, app):
        app.state.scenario = Scenario(reasoning_only_chunks=12, reasoning_chunk_interval=0.25)
        seen_comment_before_content = False
        comments = 0
        content_seen = False
        async with httpx.AsyncClient(timeout=30, headers=AUTH, trust_env=False) as c:
            async with c.stream("POST", f"{base}/v1/chat/completions", json=_body(stream=True)) as r:
                assert r.status_code == 200
                async for line in r.aiter_lines():
                    if line.startswith(":"):
                        comments += 1
                        if not content_seen:
                            seen_comment_before_content = True
                    elif _line_has_content(line):
                        content_seen = True
        assert seen_comment_before_content, (
            f"注释行={comments}，且没有一条出现在正文之前 ⇒ §6-4『推理期间必须发』未落地"
        )
        assert content_seen, "正文最终必须到达"


def _line_has_content(line: str) -> bool:
    if not line.startswith("data: "):
        return False
    try:
        obj = json.loads(line[6:])
    except Exception:
        return False
    for ch in obj.get("choices") or []:
        if (ch.get("delta") or {}).get("content"):
            return True
    return False


async def test_rb2_silence_heartbeat_fields_are_wired():
    """RB-2③：`keepalive_during_silence`／`silence_after_chunks` 此前是**声明但零使用**的死字段
    （§28.2 F2，A 抽验 grep 仅一行）。现在 fake 在沉默期真能产心跳。"""
    app = _new_app()
    app.state.scenario = Scenario(
        silence_after_chunks=1, silence_seconds=0.6,
        keepalive_during_silence=True, keepalive_interval_seconds=0.1,
    )
    status, text, frames, _ = await _asgi_read(app)
    assert status == 200 and not frames, text[:300]
    assert text.count(": keepalive") >= 2, f"沉默期心跳数不足：{text[:200]}"


async def test_rb2_keepalive_never_enters_terminal_cache():
    """§8.2.2-3／§28.2：心跳**不得进终态缓存**——否则回放会把过程字节一起缓存并重放，
    还污染 `MS_IDEMPOTENCY_CACHE_MAX_CHARS` 的体积核算。

    判据：首条流有注释行；同 key 回放时**一条注释行都不许有**，而内容与 `[DONE]` 照常。
    """
    with running_server() as (base, app):
        app.state.scenario = Scenario(reasoning_only_chunks=4, reasoning_chunk_interval=0.05)
        key = {"Idempotency-Key": "rb2-no-keepalive-replay"}

        async def one():
            async with httpx.AsyncClient(timeout=30, headers={**AUTH, **key}, trust_env=False) as c:
                async with c.stream("POST", f"{base}/v1/chat/completions", json=_body(stream=True)) as r:
                    return r.status_code, (await r.aread()).decode(), dict(r.headers)

        s1, t1, h1 = await one()
        s2, t2, h2 = await one()
        assert s1 == s2 == 200
        assert t1.count(": keepalive") >= 1, "首次流就该有注释行"
        assert ": keepalive" not in t2, f"回放里出现心跳 ⇒ 心跳被写进了终态缓存：{t2[:200]}"
        assert t2.rstrip().endswith("[DONE]")
        assert "idempotency-replayed" not in {k.lower() for k in h1}, "首次响应不该标回放"
        assert h2.get("idempotency-replayed") == "true", "回放必须带 Idempotency-Replayed（RB-5③）"


# ================================================================ RB-5 入窗 TTL 分类 ＋ 回放标记
async def test_rb5_error_terminal_uses_short_ttl_then_retries_for_real():
    """§28.3 判据（双向都要）：TTL 内重发 ⇒ **回放**（`upstream.started` 不增）；
    TTL 外 ⇒ **真实重调**（+1）且上游恢复即放行。

    mutation＝把错误类 TTL 改回 `window_seconds` ⇒ "TTL 外那条"红：600s 的窗口会把一次慢
    推理放大成同一条消息的窗口级锁死（§16.2 同型）。
    """
    with running_server(MS_IDEMPOTENCY_ERROR_TTL_SECONDS="0.3",
                        MS_IDEMPOTENCY_WINDOW_SECONDS="600") as (base, app):
        url = f"{base}/v1/chat/completions"
        key = {"Idempotency-Key": "rb5-error-ttl"}
        app.state.scenario = Scenario(error={"code": "upstream_error", "retryable": False})

        async def one():
            async with httpx.AsyncClient(timeout=15, headers={**AUTH, **key}, trust_env=False) as c:
                r = await c.post(url, json=_body())
                return r.status_code, dict(r.headers)

        st1, h1 = await one()
        assert st1 == 502 and app.state.upstream.started == 1, (st1, app.state.upstream.started)
        assert "idempotency-replayed" not in {k.lower() for k in h1}

        st2, h2 = await one()  # TTL 内
        assert st2 == 502 and h2.get("idempotency-replayed") == "true"
        assert app.state.upstream.started == 1, "TTL 内必须回放，不该又调一次上游"

        time.sleep(0.45)  # 过 error TTL（0.3s），远未到 window（600s）
        app.state.scenario = Scenario()  # 上游"恢复"
        st3, h3 = await one()
        assert st3 == 200, f"TTL 外应真实重调并成功：{st3} {h3}"
        assert app.state.upstream.started == 2
        assert "idempotency-replayed" not in {k.lower() for k in h3}


async def test_rb5_success_terminal_keeps_long_window():
    """§28.3 的另一半：**成功**仍用长窗回放。error TTL 设 0.3s、window 600s，隔 0.45s 重发：
    若实现把短 TTL 用到成功条目上，这次会重新真实调用上游 ⇒ 红。
    """
    with running_server(MS_IDEMPOTENCY_ERROR_TTL_SECONDS="0.3",
                        MS_IDEMPOTENCY_WINDOW_SECONDS="600") as (base, app):
        url = f"{base}/v1/chat/completions"
        key = {"Idempotency-Key": "rb5-success-long"}

        async def one():
            async with httpx.AsyncClient(timeout=15, headers={**AUTH, **key}, trust_env=False) as c:
                r = await c.post(url, json=_body())
                return r.status_code, dict(r.headers)

        st1, h1 = await one()
        assert st1 == 200 and "idempotency-replayed" not in {k.lower() for k in h1}
        n1 = app.state.upstream.started
        time.sleep(0.45)
        st2, h2 = await one()
        assert st2 == 200 and h2.get("idempotency-replayed") == "true"
        assert app.state.upstream.started == n1, "成功条目必须留在长窗里回放"


async def test_rb5_permanent_4xx_denial_stays_in_long_window():
    """TTL 表的类目只给「结果不明」（超时／已发起调用后的 5xx）。4xx 永久性拒绝（413）
    同输入必同拒绝，是**稳定答案** ⇒ 留长窗，不该在 error TTL 之后被重调。"""

    class Denier:
        name = "denier"

        def __init__(self):
            self.started = 0

        async def complete(self, body, scenario):
            self.started += 1
            raise ServiceError("context_length_exceeded", "太长", param="messages", retryable=False)

        async def stream(self, body, scenario):  # pragma: no cover
            raise AssertionError("unused")
            yield

    with running_server(MS_IDEMPOTENCY_ERROR_TTL_SECONDS="0.3",
                        MS_IDEMPOTENCY_WINDOW_SECONDS="600") as (base, app):
        denier = Denier()
        app.state.upstream = denier
        url = f"{base}/v1/chat/completions"
        key = {"Idempotency-Key": "rb5-413-long"}

        async def one():
            async with httpx.AsyncClient(timeout=15, headers={**AUTH, **key}, trust_env=False) as c:
                r = await c.post(url, json=_body())
                return r.status_code, dict(r.headers)

        st1, h1 = await one()
        assert st1 == 413 and "idempotency-replayed" not in {k.lower() for k in h1}
        time.sleep(0.45)
        st2, h2 = await one()
        assert st2 == 413 and h2.get("idempotency-replayed") == "true", "永久拒绝应留长窗回放"
        assert denier.started == 1, "永久拒绝被短 TTL 提前重调 ⇒ 类目划错"


async def test_rb5_replay_header_on_stream_replay_too():
    """RB-5③：流式回放的**响应头**也必须有标记——流式的头只在开流前发一次，
    那是唯一能标"这条流是副本"的位置（不加 body 字段、不改 chunk 形状）。"""
    with running_server() as (base, app):
        url = f"{base}/v1/chat/completions"
        key = {"Idempotency-Key": "rb5-stream-replay-header"}

        async def one():
            async with httpx.AsyncClient(timeout=20, headers={**AUTH, **key}, trust_env=False) as c:
                async with c.stream("POST", url, json=_body(stream=True)) as r:
                    await r.aread()
                    return r.status_code, dict(r.headers)

        s1, h1 = await one()
        s2, h2 = await one()
        assert s1 == s2 == 200
        assert "idempotency-replayed" not in {k.lower() for k in h1}
        assert h2.get("idempotency-replayed") == "true", h2


async def test_rb5_replayed_error_body_is_byte_identical(monkeypatch, ms_env):
    """RB-5 只动**时长**与**响应头**，不改语义：回放的 §7 错误体必须与首次逐字一致。"""
    ms_env(MS_UPSTREAM_FIRST_TOKEN_TIMEOUT="0.1", MS_IDEMPOTENCY_ERROR_TTL_SECONDS="30")
    app = _new_app()
    app.state.scenario = Scenario(first_token_seconds=5.0)
    key = {"Idempotency-Key": "rb5-same-body"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ms", headers=AUTH, timeout=20
    ) as c:
        r1 = await c.post("/v1/chat/completions", json=_body(stream=True), headers=key)
        r2 = await c.post("/v1/chat/completions", json=_body(stream=True), headers=key)
    assert r1.status_code == r2.status_code == 504
    assert r1.json() == r2.json(), "回放必须逐字还原错误体（retryable/code 语义不变）"
    assert r2.headers.get("idempotency-replayed") == "true"
    assert "idempotency-replayed" not in {k.lower() for k in r1.headers}


# ================================================================ 配置绑定（新名字必须真的绑上）
def test_rb_new_env_names_bind(monkeypatch):
    """新 env 的名字→字段绑定实测：`extra="ignore"` 会静默吞掉拼错的名字（cdda16a 踩过一次）。"""
    for name, field, val in (
        ("MS_UPSTREAM_FIRST_CONTENT_TIMEOUT", "upstream_first_content_timeout", "33.5"),
        ("MS_IDEMPOTENCY_ERROR_TTL_SECONDS", "idempotency_error_ttl_seconds", "12.5"),
    ):
        monkeypatch.setenv(name, val)
        get_settings.cache_clear()
        try:
            assert getattr(Settings(), field) == float(val), name
        finally:
            get_settings.cache_clear()


def test_rb5_config_rejects_error_ttl_not_shorter_than_window(monkeypatch):
    """§28.3-1：`error_ttl < window` 才叫分类，否则"结果不明"与成功同长、分而无效。"""
    monkeypatch.setenv("MS_IDEMPOTENCY_WINDOW_SECONDS", "600")
    monkeypatch.setenv("MS_IDEMPOTENCY_ERROR_TTL_SECONDS", "600")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValidationError) as ei:
            Settings()
        assert "§8.2.1" in str(ei.value)
    finally:
        get_settings.cache_clear()


def test_rb5_window_takes_error_ttl_from_settings():
    """`IdempotencyWindow` 默认值不该与 Settings 脱钩：直接验构造参数被用上。"""
    from msvc.idempotency import IdempotencyWindow

    w = IdempotencyWindow(600, error_ttl_seconds=7.5)
    assert w.error_ttl_seconds == 7.5 and w.window_seconds == 600
