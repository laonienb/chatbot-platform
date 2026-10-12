"""流式主路径的幂等窗与错误语义（协议 §6.7 / §8.2 / §8.2.2；工作单 P1-10 / P1-9）。

P1-10 在此为**独立文件**：`test_contract.py` 覆盖 §10 的 T1–T11 happy/边界链，本文件
覆盖 v1.7 新增的「流式 × 幂等窗」与「流式错误分级」，便于按工作单项定位。
"""

import json

import httpx
import pytest

from tests.conftest import auth_headers


def _body(**kw):
    b = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}
    b.update(kw)
    return b


async def _collect(client, headers=None, **body_kw) -> tuple[int, dict]:
    """发一条流式请求，收集完整 SSE 文本并解析成结构化终态。"""
    text = ""
    async with client.stream(
        "POST", "/v1/chat/completions", json=_body(stream=True, **body_kw), headers=headers or {}
    ) as r:
        status = r.status_code
        async for line in r.aiter_lines():
            text += line + "\n"
    return status, _parse_sse(text)


def _parse_sse(text: str) -> dict:
    """解析 SSE：内容文本、usage、finish_reason、终止事件、错误帧、[DONE]。"""
    out: dict = {
        "content": "",
        "usage": None,
        "finish_reason": None,
        "xms": None,
        "error": None,
        "done": False,
        "raw": text,
    }
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if payload == "[DONE]":
            out["done"] = True
            continue
        obj = json.loads(payload)
        if isinstance(obj.get("error"), dict):
            out["error"] = obj["error"]
        if isinstance(obj.get("usage"), dict):
            out["usage"] = obj["usage"]
        if isinstance(obj.get("x_model_service"), dict):
            out["xms"] = obj["x_model_service"]
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") or {}
            out["content"] += delta.get("content") or ""
            if choice.get("finish_reason"):
                out["finish_reason"] = choice["finish_reason"]
    return out


# ============================================================ P1-10 验收① 成功流入窗 + 终态重放
async def test_p1_10_stream_success_cached_and_replayed(client, app):
    """P1-10 ①/§8.2.2：流式成功**必须入窗**；同 key 重发 → 终态重放，**不再调上游**。

    这是 G2 🔴（真实双烧）的防线：兼容面指纹键（`api_key+model+消息体` + 10 分钟桶）下
    AstrBot/QQ 重发会打到同一个 key，若流式不入窗，上游会被**真实重调、二次计费**。
    """
    app.state.upstream.reset()
    h = {"Idempotency-Key": "idem-stream-ok"}

    status1, first = await _collect(client, h)
    assert status1 == 200
    assert first["content"], "首次应产出内容"
    assert first["usage"], "首次应产出 usage chunk"
    assert first["done"] is True
    assert app.state.upstream.started == 1

    status2, replay = await _collect(client, h)
    assert status2 == 200
    assert app.state.upstream.started == 1, "同 key 重发不得再调上游（否则就是 G2 的双烧）"
    # §8.2.2-3：内容文本、usage、终止事件必须与首次一致（chunk 边界允许不同）
    assert replay["content"] == first["content"]
    assert replay["usage"] == first["usage"]
    assert replay["xms"] == first["xms"]
    assert replay["done"] is True
    assert replay["error"] is None


# ============================================================ P1-10 验收② 在飞位对流式同样适用
async def test_p1_10_same_key_concurrent_second_rejected_409(live_server, app):
    """P1-10 ②/§8.2.2-2：同 key **并发**两条流 → 第二条被在飞位拒（409），上游只加 1。

    若不拦，两条流会各自真实调用上游（并发双烧），而这正是 in-flight 位要防的。

    ⚠️ 必须用**真 uvicorn + 真 TCP**（`live_server`）：httpx 的 `ASGITransport` 会把
    ASGI 响应体**整条缓冲**后才返回（`client.stream` 拿不到增量），那样第一条流的生成器
    早已跑完、在飞位已释放，本用例会退化成"先跑完再重发"从而假绿。
    """
    app.state.upstream.reset()
    # 用**显式沉默**续住第一条流：首个内容 chunk 发出后进入 30s 沉默 ⇒ 在飞位在整个断言期间
    # 稳定持有，"并发"这件事不再依赖时间差运气。（`silence_*` 字段此前是声明但零使用的死字段，
    # RB-2③ 才接上。）旧写法 `chunk_delay=0.2` 让整条约 0.6s，共享机上跑全量会被调度挤掉：
    # 2026-10-12 在 HEAD(79cdace) 基线上复现过「全量 1 failed / 单文件 12 passed」，
    # 症状正是第二条拿到 200（第一条已跑完、在飞位已释放）——是时间预算太窄，不是用例逻辑错。
    sc = app.state.scenario
    sc.chunk_delay = 0.05
    sc.silence_after_chunks = 1
    sc.silence_seconds = 30.0
    url = f"{live_server}/v1/chat/completions"
    key = "idem-stream-race"

    async with httpx.AsyncClient(timeout=10, headers=auth_headers()) as c1:
        async with c1.stream("POST", url, json=_body(stream=True), headers={"Idempotency-Key": key}) as r1:
            assert r1.status_code == 200
            it = r1.aiter_lines().__aiter__()
            await it.__anext__()  # 首字节已发出，第一条流仍在飞
            async with httpx.AsyncClient(timeout=10, headers=auth_headers()) as c2:
                r2 = await c2.post(url, json=_body(stream=True), headers={"Idempotency-Key": key})
            assert r2.status_code == 409, "同 key 并发第二条必须被在飞位拒（409）"
            # 不 drain 第一条：它此刻正处在 30s 沉默里，退出上下文即取消（取消传播另有 T6 用例）

    assert app.state.upstream.started == 1, "并发第二条不得各自真实调用上游"


# ============================================================ P1-10 验收③ 流内错误帧终止态入窗
async def test_p1_10_stream_error_terminal_cached_and_replayed(client, app, scenario):
    """P1-10 ③/§8.2.2-1：**已发起上游调用后**的错误帧终止 → 必须入窗；同 key 重发回放。

    与 ④ 方向相反：这类终止"结果不明/已计费"，重发即双烧 ⇒ 必须回放；
    首字节前的 HTTP 级瞬时失败则**不得**入窗（见下一条）。
    """
    app.state.upstream.reset()
    scenario.error = {"code": "internal_error", "retryable": False}
    scenario.error_after_chunks = 1  # 首字节之后才失败
    h = {"Idempotency-Key": "idem-stream-err"}

    status1, first = await _collect(client, h)
    assert status1 == 200  # SSE 已开始 → 无法改状态码，只能流内错误帧
    assert first["error"]["code"] == "internal_error"
    assert first["content"], "错误前已产出的部分内容必须保留"
    assert first["done"] is True, "§6.7 G3-1：错误帧之后仍须发 [DONE]"
    assert app.state.upstream.started == 1

    scenario.error = None
    scenario.error_after_chunks = None
    status2, replay = await _collect(client, h)
    assert status2 == 200
    assert replay["error"]["code"] == "internal_error", "错误终止态必须回放（同 key）"
    assert replay["content"] == first["content"]
    assert replay["done"] is True
    assert app.state.upstream.started == 1, "错误终止态已入窗 → 不得重调上游（防双烧）"


# ============================================================ P1-10 验收④ 首字节前失败不入窗
async def test_p1_10_pre_first_byte_failure_not_cached(client, app, scenario):
    """P1-10 ④：**首字节前**的瞬时失败（上游 429）**不入窗** —— 上游恢复后必须真实重调。

    本项与 ③ 是**方向相反**的两个用例，缺一即漏掉一类错误：只测 ③ 会写出"一切失败都
    入窗"（堵死客户端退避），只测本项会写出"一切失败都不入窗"（双烧）。
    """
    app.state.upstream.reset()
    scenario.error = {"code": "rate_limited", "retry_after": 3, "retryable": True}
    h = {"Idempotency-Key": "idem-stream-429"}

    r1 = await client.post("/v1/chat/completions", json=_body(stream=True), headers=h)
    assert r1.status_code == 429
    assert r1.headers.get("Retry-After") == "3"
    assert app.state.upstream.started == 1

    scenario.error = None  # 上游恢复
    status2, second = await _collect(client, h)
    assert status2 == 200
    assert second["content"], "瞬时拒绝未入窗 → 同 key 重发应真实重调上游并成功"
    assert app.state.upstream.started == 2


# ============================================================ P1-10 ⑤ 缓存有界 + 淘汰语义显式
async def test_p1_10_cache_bound_declares_miss_on_eviction(client, app):
    """P1-10 ⑤/§8.2.2-3：缓存条数上限生效；被淘汰的 key 后续按**未命中**处理。

    协议允许"按未命中处理"这一支，但要求**显式声明**（README 已写、淘汰处打 WARNING
    日志）——不允许淘汰后**静默**重调上游。本用例把该语义钉住，防后人把上限悄悄去掉。
    """
    app.state.upstream.reset()
    app.state.window.max_entries = 1  # 只留 1 条：第二条写入即淘汰第一条

    await _collect(client, {"Idempotency-Key": "idem-a"})
    assert app.state.upstream.started == 1
    await _collect(client, {"Idempotency-Key": "idem-b"})
    assert app.state.upstream.started == 2

    # A 已被淘汰 → 按未命中处理（真实重调上游），而不是命中一条不存在的缓存
    await _collect(client, {"Idempotency-Key": "idem-a"})
    assert app.state.upstream.started == 3


@pytest.mark.parametrize("kind", ["chunk"])
async def test_p1_10_replay_chunk_boundary_may_differ(client, app, kind):
    """§8.2.2-3：回放**不要求逐事件原样**（chunk 边界可不同），但内容必须一致。

    首次是 3 个内容 chunk（"这是"/" 模型服务的"/"流式回复。"），回放按缓存终态**重新分块**
    —— 断言二者拼接后的文本相等，且回放内容块数量**不**被要求一致。
    """
    app.state.upstream.reset()
    h = {"Idempotency-Key": f"idem-boundary-{kind}"}
    _, first = await _collect(client, h)
    _, replay = await _collect(client, h)

    def _content_chunks(text: str) -> list[str]:
        out = []
        for line in text.splitlines():
            if not line.startswith("data:") or line.endswith("[DONE]"):
                continue
            obj = json.loads(line[len("data:") :].strip())
            for choice in obj.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content")
                if piece:
                    out.append(piece)
        return out

    assert "".join(_content_chunks(first["raw"])) == "".join(_content_chunks(replay["raw"]))
    assert first["content"] == replay["content"]


# ============================================================ P1-8 收尾 chunk 的 finish_reason
async def test_p1_8_success_stream_carries_finish_reason(client, scenario):
    """P1-8 / §6.6：成功流必须有一个携带**非 null** `finish_reason` 的收尾 chunk，且在 usage 前。

    服务侧此前从不发它（`provider.py` 不 yield 收尾事件、`main.py` 调 `_chunk` 不传 finish），
    而平台侧 `remote.py` 已在解析该字段并填进 `StreamDone.finish_reason`——**平台自家桩已发、
    真实服务不发**，所以平台的 P1-1 归因在生产路径静默失效。本用例把"服务侧真的发"钉住。
    """
    scenario.finish_reason = "length"  # 上游实际值：被 max_tokens 截断
    status, got = await _collect(client)
    assert status == 200
    assert got["finish_reason"] == "length", "收尾 chunk 的 finish_reason 必须是上游实际值"
    # 顺序：finish chunk 在 usage chunk 之前（§6.6）
    lines = [l for l in got["raw"].splitlines() if l.startswith("data:")]
    at_finish = next(i for i, l in enumerate(lines) if '"finish_reason": "length"' in l)
    at_usage = next(i for i, l in enumerate(lines) if '"usage"' in l)
    assert at_finish < at_usage


async def test_p1_8_replay_preserves_finish_reason(client, app, scenario):
    """§8.2.2-3：终态重放的一致性清单里含 `finish_reason` —— 回放必须与首次同值。

    若缓存只存内容/usage 而丢掉 finish_reason，重放的终态就与首次**不等价**（平台侧
    归因从 `length` 变成 None），违反本节"内容文本、usage、finish_reason、终止事件一致"。
    """
    app.state.upstream.reset()
    scenario.finish_reason = "length"
    h = {"Idempotency-Key": "idem-finish-replay"}
    _, first = await _collect(client, h)
    _, replay = await _collect(client, h)
    assert first["finish_reason"] == "length"
    assert replay["finish_reason"] == first["finish_reason"], "终态重放必须保持 finish_reason"
    assert replay["usage"] == first["usage"]
    assert app.state.upstream.started == 1


# ============================================================ P1-9 验收⑥：中途停顿不得被误杀（O1）
async def test_p1_9_mid_stream_stall_within_budget_is_not_killed(client, scenario):
    """P1-9 ⑥ / O1：真实上游偶发的 **>1s 停顿是正常的**，不得被 chunk 间隔预算误杀。

    旧实现把首字节后的间隔超时**硬编码为 1.0s**，且触发时文案写「上游首 token 超时」——
    真实上游（推理模型、网络抖动）中途停 2s 就会被判失败并误归因。默认预算 30s，
    故 2s 停顿必须完整跑完（内容 + usage + 收尾 + [DONE]）。
    """
    from msvc.config import get_settings

    assert get_settings().upstream_chunk_idle_timeout >= 5.0, (
        "中途空闲预算不得小于 5s（O1 定案值 30s，压回 1s 等于把误杀改回来）"
    )
    scenario.stall_after_chunks = 1
    scenario.stall_seconds = 2.0
    status, got = await _collect(client)
    assert status == 200
    assert got["error"] is None, "2s 停顿在预算内，不得产生流内错误帧"
    assert got["content"], "内容必须完整"
    assert got["finish_reason"], "P1-8：正常收尾必须带 finish_reason"
    assert got["done"] is True


# ============================================================ P1-9 验收⑦：超预算归因须独立成码
async def test_p1_9_chunk_budget_exceeded_has_its_own_code(client, scenario, monkeypatch):
    """P1-9 ⑦：中途停顿超预算 → `upstream_chunk_timeout`，**不得**复用 `upstream_timeout`。

    §7.1：两类失败「入窗结论相同、归因不同」，日志与 `error_code` 必须可区分。首字节已过
    ⇒ 状态码改不了，只能以流内错误帧收尾（§6.7），其后仍须 `[DONE]`（G3-1）。
    """
    from msvc.config import get_settings

    monkeypatch.setattr(get_settings(), "upstream_chunk_idle_timeout", 0.3)
    scenario.stall_after_chunks = 1
    scenario.stall_seconds = 3.0
    status, got = await _collect(client)
    assert status == 200, "首字节已过 → 只能流内错误帧，不能改状态码"
    assert got["error"] is not None
    assert got["error"]["code"] == "upstream_chunk_timeout", "归因必须区别于首 token 超时"
    assert got["error"]["retryable"] is False, "结果不明/可能已计费 ⇒ 入窗（§8.2.1）"
    assert got["content"], "已产出的部分内容必须保留"
    assert got["done"] is True, "§6.7 G3-1：错误帧之后仍须发 [DONE]"


async def test_p1_9_capabilities_exposes_chunk_idle_budget(client):
    """§8.1-2 / 架构师B H2：`capabilities.timeouts` 必须暴露中途空闲预算。

    平台启动自检据此断言「内层 < 外层」。缺这个字段，服务侧把预算配成 > 平台流式空闲
    上限（90s）时无人拦截——平台会先断开并把归因抢走。
    """
    d = (await client.get("/v1/capabilities")).json()
    timeouts = d["features"]["timeouts"]
    assert "upstream_chunk_idle_s" in timeouts, "中途空闲预算须可被平台观测"
    assert timeouts["upstream_chunk_idle_s"] > 1.0, (
        "预算不得回退到旧的硬编码 1s（O1：真实上游正常停顿即被误杀）"
    )


# ============================================================ H4：首字节前超时的入窗**正例**
async def test_h4_pre_first_byte_timeout_must_be_cached(client, app, scenario, monkeypatch):
    """H4 / §8.2.1 双向原则：首字节**前**的超时属「结果不明」⇒ **必须入窗**。

    `test_p1_10_pre_first_byte_failure_not_cached` 钉的是反方向（上游 429 瞬时拒绝**不**入窗）。
    只有那一条，实现就能写成"首字节前的失败一律不入窗"而**全绿** —— 而那会让同 key 重发
    再打一次上游（超时已经发起调用、可能已计费 ⇒ 双烧）。两条方向相反，缺一即漏一类。

    判据链：首 token 超时 → `ServiceError(upstream_timeout, retryable=False)` → HTTP 504 →
    `_finish_window(..., reached_upstream=True)` → `should_cache` 两布尔 ⇒ 入窗。
    """
    from msvc.config import get_settings

    app.state.upstream.reset()
    monkeypatch.setattr(get_settings(), "upstream_first_token_timeout", 0.3)
    scenario.first_token_seconds = 1.0
    h = {"Idempotency-Key": "idem-pre-timeout"}

    r1 = await client.post("/v1/chat/completions", json=_body(stream=True), headers=h)
    assert r1.status_code == 504, "§6.7：首字节前的失败须走 HTTP 错误状态"
    assert r1.json()["error"]["code"] == "upstream_timeout"
    assert r1.json()["error"]["retryable"] is False, "结果不明（可能已计费）⇒ retryable=false"
    assert app.state.upstream.started == 1

    scenario.first_token_seconds = 0.0  # 上游恢复
    r2 = await client.post("/v1/chat/completions", json=_body(stream=True), headers=h)
    assert r2.status_code == 504, "结果不明已入窗 ⇒ 同 key 重发回放，不能再打上游"
    assert app.state.upstream.started == 1, "重发真实重调上游就是双烧（幂等窗存在的理由）"
