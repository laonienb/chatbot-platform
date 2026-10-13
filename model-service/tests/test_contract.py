"""model-service 侧契约测试：对真实服务实现跑协议 §10 的 T1–T11（含 P0-3 入站校验）。

与平台侧 `server/tests/test_model_service_contract.py` 对同一契约从两端验证。
"""

import asyncio
import json

import httpx
import pytest

from tests.conftest import auth_headers  # noqa: F401  (fixture 复用)


def _body(**kw):
    b = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}]}
    b.update(kw)
    return b


# ---------------------------------------------------------------- T1 非流式基础
async def test_t1_nonstream_basic(client):
    r = await client.post("/v1/chat/completions", json=_body())
    assert r.status_code == 200
    d = r.json()
    assert d["object"] == "chat.completion"
    assert d["usage"]["prompt_tokens"] == 120 and d["usage"]["completion_tokens"] == 80
    xms = d["x_model_service"]
    assert xms["provider"] == "deepseek"
    assert xms["model_used"] == "deepseek/deepseek-chat"
    assert xms["cost_status"] == "exact"


# ---------------------------------------------------------------- T2 成本不可得
async def test_t2_unknown_cost_never_zero(client, scenario):
    scenario.cost_mode = "unknown"
    d = (await client.post("/v1/chat/completions", json=_body())).json()
    xms = d["x_model_service"]
    assert xms["cost_usd"] is None
    assert xms["cost_status"] == "unknown"
    assert xms["cost_usd"] != "0" and xms["cost_usd"] != 0  # 红线2


async def test_t2_absent_block_proxy_shape(client, scenario):
    scenario.cost_mode = "absent"
    d = (await client.post("/v1/chat/completions", json=_body())).json()
    assert "x_model_service" not in d  # Proxy 期整块缺失


# ---------------------------------------------------------------- T3 persona 前缀 400
async def test_t3_persona_prefix_rejected(client):
    r = await client.post("/v1/chat/completions", json=_body(model="persona:libai"))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


# ---------------------------------------------------------------- P0-3 红线1 禁字段入站校验
# 验收①列了四个字段（user_id / conversation_id / persona / trace）——`trace` 是
# §4.2 v1.5 收编为「v1 禁发」的字段，最易被漏，必须同样有用例（架构师核对遗留①）。
@pytest.mark.parametrize("field", ["user_id", "conversation_id", "persona", "trace"])
async def test_p0_3_forbidden_field_rejected_400(client, app, field):
    r = await client.post("/v1/chat/completions", json=_body(**{field: "x"}))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    # 关键：禁字段必须在任何上游调用前被拒（架构师核对遗留②：注释宣称的验收点必须落地成断言）。
    # 对齐平台侧 `test_audit_c_forbidden_fields_rejected_platform_side` 的强度（assert 请求数为 0），
    # 防后人把 guard_request 挪到上游调用之后。
    assert app.state.upstream.started == 0, "禁字段必须在触达上游前被拒（红线1）"


async def test_p0_3_message_level_forbidden_field(client, app):
    body = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi", "user_id": "leak"}]}
    r = await client.post("/v1/chat/completions", json=body)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert app.state.upstream.started == 0, "messages 夹带身份字段同样必须在触达上游前被拒"


async def test_p0_3_unknown_extension_not_rejected(client):
    # 宽松解析：未知扩展字段不得被拒（§4.2）
    r = await client.post("/v1/chat/completions", json=_body(custom_ext="whatever"))
    assert r.status_code == 200


# ---------------------------------------------------------------- T4 流式完整链
async def test_t4_stream_chunks_usage_terminate_done(client):
    async with client.stream("POST", "/v1/chat/completions", json=_body(stream=True)) as r:
        assert r.status_code == 200
        text = ""
        async for line in r.aiter_lines():
            text += line + "\n"
    # 内容 chunk → usage chunk → event: model_service → [DONE]
    assert "这是" in text and "[DONE]" in text
    assert '"usage"' in text
    assert "event: model_service" in text
    # 顺序：usage 早于 terminate 早于 [DONE]
    assert text.index('"usage"') < text.index("event: model_service") < text.index("[DONE]")

    # ---- P1-8 / §6.6：收尾 chunk 携带**非 null** finish_reason，且位于 usage chunk 之前 ----
    # 结构级断言（不用文本匹配：内容 chunk 里也有 `"finish_reason": null`，文本判断会假绿）。
    objs = []
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if payload != "[DONE]":
            objs.append(json.loads(payload))
    finish_at = next(
        (i for i, o in enumerate(objs) if any(c.get("finish_reason") for c in (o.get("choices") or []))),
        None,
    )
    usage_at = next((i for i, o in enumerate(objs) if isinstance(o.get("usage"), dict)), None)
    assert finish_at is not None, "§6.6：必须存在携带非 null finish_reason 的收尾 chunk（P1-8）"
    assert usage_at is not None, "§6.2：必须存在 usage chunk"
    assert finish_at < usage_at, "§6.6：收尾 chunk 必须位于 usage chunk **之前**"


async def test_p1_8_finish_reason_reflects_scenario(client, scenario):
    """P1-8：`finish_reason` 取**上游实际值**，不是硬编码 `stop`。

    平台据此区分「正常结束 vs 被 max_tokens 截断」（工作单 P1-1 的价值所在）。若实现把
    收尾 chunk 写成固定 `"stop"`，本用例必须变红。
    """
    scenario.finish_reason = "length"
    async with client.stream("POST", "/v1/chat/completions", json=_body(stream=True)) as r:
        text = ""
        async for line in r.aiter_lines():
            text += line + "\n"
    assert '"finish_reason": "length"' in text, "收尾 chunk 必须回显上游实际的终止原因"


# ---------------------------------------------------------------- T5 429 双语义
async def test_t5_rate_limited_has_retry_after(client, scenario):
    scenario.error = {"code": "rate_limited", "retry_after": 7, "retryable": True}
    r = await client.post("/v1/chat/completions", json=_body())
    assert r.status_code == 429
    assert r.headers.get("Retry-After") == "7"
    assert r.json()["error"]["code"] == "rate_limited"


async def test_t5_budget_exhausted_no_retry_after(client, scenario):
    scenario.error = {"code": "budget_exhausted", "retryable": False}
    r = await client.post("/v1/chat/completions", json=_body())
    assert r.status_code == 429
    assert "Retry-After" not in r.headers


# ---------------------------------------------------------------- T6 取消传播（真连接）
async def test_t6_client_disconnect_cancels_upstream(live_server, app):
    import time as _t

    app.state.scenario.chunk_delay = 0.5  # 续住连接，断连时服务端仍在第 2 个 chunk 前等待
    upstream = app.state.upstream
    upstream.reset()

    async with httpx.AsyncClient(timeout=10) as c:
        async with c.stream(
            "POST",
            f"{live_server}/v1/chat/completions",
            json=_body(stream=True),
            headers=auth_headers(),
        ) as r:
            it = r.aiter_lines().__aiter__()
            await it.__anext__()  # 拿到第一个 chunk
        # 离开 context → 断连
    # 轮询 ≤2s 内上游被取消（红线6）
    deadline = _t.monotonic() + 2.0
    while upstream.cancelled < 1 and _t.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert upstream.cancelled >= 1, "平台断连后上游须在 ≤2s 内被中止（红线6）"


# ---------------------------------------------------------------- T7 幂等窗口
async def test_t7_same_key_calls_upstream_once(client, app):
    app.state.upstream.reset()
    h = {"Idempotency-Key": "idem-abc"}
    r1 = await client.post("/v1/chat/completions", json=_body(), headers=h)
    r2 = await client.post("/v1/chat/completions", json=_body(), headers=h)
    assert r1.status_code == 200 and r2.status_code == 200
    assert app.state.upstream.started == 1, "同 Idempotency-Key 窗口内上游只应被调一次"


async def test_t7_transient_error_not_cached(client, app):
    """§8.2.1：上游 429（retryable）不入窗——同 key 重发应重新调上游（可成功）。"""
    app.state.upstream.reset()
    h = {"Idempotency-Key": "idem-429"}
    app.state.scenario.error = {"code": "rate_limited", "retry_after": 1, "retryable": True}
    first = await client.post("/v1/chat/completions", json=_body(), headers=h)
    assert first.status_code == 429
    app.state.scenario.error = None  # 上游恢复
    second = await client.post("/v1/chat/completions", json=_body(), headers=h)
    assert second.status_code == 200, "瞬时错误未入窗 → 重发重新走上游并成功"
    assert app.state.upstream.started == 2


async def test_t7_unknown_result_must_cache(client, app):
    """§8.2.1：已发起调用后的 5xx（结果不明，retryable=false）必须入窗，防双烧。"""
    app.state.upstream.reset()
    h = {"Idempotency-Key": "idem-500"}
    app.state.scenario.error = {"code": "internal_error", "retryable": False}
    r1 = await client.post("/v1/chat/completions", json=_body(), headers=h)
    assert r1.status_code == 500
    app.state.scenario.error = None
    r2 = await client.post("/v1/chat/completions", json=_body(), headers=h)
    assert r2.status_code == 500, "结果不明的失败已入窗 → 重发回放缓存而非重调上游（防双烧）"
    assert app.state.upstream.started == 1


async def test_t7_deterministic_reject_413_must_cache(client, app):
    """§8.2.1 三分表「确定性拒绝 → 入窗」：413 context_length_exceeded 入窗回放。

    与上一条 `test_t7_unknown_result_must_cache` **语义不同，不是重复用例**：
    - 那条测「结果不明」（已发起调用后的 5xx，**可能已计费**）→ 入窗理由是**防双烧**；
    - 本条测「确定性拒绝」（`413`，**同输入必同拒绝**）→ 入窗理由是**回放正确且省一次上游调用**。
    这正是 P1-7 于 v1.4 立项的理由：若按「4xx 一律不入窗」实现，`413` 会被排除掉。
    因此本用例必须能捕获「4xx 一刀切」这一类错误实现——否则这道防线无人看守。

    契约依据：§8.2.1 三分表第③行 + §1 红线 11（可重放性按副作用性质三分，不得按状态码段归类）；
    实现依据：`errors.py`（`context_length_exceeded`→413）、`idempotency.py` 的
    `should_cache`（**两布尔**：`retryable=False` 且 `reached_upstream=True` ⇒ 入窗，无状态码
    白名单——见 P1-11）、`main.py` 的 `_map_upstream_error` → `_finish_window`（传真实
    `retryable=False` 与 `reached_upstream=True`）。
    """
    app.state.upstream.reset()
    h = {"Idempotency-Key": "idem-413"}
    app.state.scenario.error = {"code": "context_length_exceeded", "retryable": False}
    first = await client.post("/v1/chat/completions", json=_body(), headers=h)
    assert first.status_code == 413
    assert first.json()["error"]["code"] == "context_length_exceeded"
    assert first.json()["error"]["retryable"] is False
    assert app.state.upstream.started == 1

    app.state.scenario.error = None  # 模拟"上游恢复"
    second = await client.post("/v1/chat/completions", json=_body(), headers=h)
    assert second.status_code == 413, "确定性拒绝必须入窗 → 同 key 重发回放缓存，而非重新调上游"
    assert second.json()["error"]["code"] == "context_length_exceeded"
    assert app.state.upstream.started == 1, "回放不得再打上游（否则 413 被当普通 4xx 排除 → 上游重复调用/双烧）"


@pytest.mark.parametrize(
    ("code", "http"),
    [
        pytest.param("upstream_auth_failed", 401, id="upstream-401-凭证失效"),
        pytest.param("compliance_denied", 403, id="upstream-403-内容策略"),
    ],
)
async def test_p1_11_upstream_deterministic_reject_must_cache(client, app, code, http):
    """P1-11：入窗判定是**两个布尔**，不是 `status in (413, 404)` 白名单。

    上游 401（provider 凭证失效）与上游 403（内容策略拒绝）和 413 同属「同输入必同拒绝」
    的**永久性拒绝**：已到达上游、重发必然同样失败 ⇒ 必须入窗回放。旧实现按状态码白名单
    判定时它们会被漏掉，于是窗口内每次重发都**白打一次上游**（§8.2.1 v1.6 澄清、§19.3-G6）。

    本用例看守的正是那类白名单：把 `should_cache` 改回 `status in (413, 404)` 必须变红。
    与 `test_t7_local_reject_not_cached` 的分界在 `started == 1` —— 错误由 `FakeUpstream`
    抛出（确实到了上游），本地拒绝则是 `started == 0` 且根本不进窗口。
    """
    app.state.upstream.reset()
    h = {"Idempotency-Key": f"idem-{code}"}
    app.state.scenario.error = {"code": code, "retryable": False}

    first = await client.post("/v1/chat/completions", json=_body(), headers=h)
    assert first.status_code == http
    assert first.json()["error"]["code"] == code
    assert app.state.upstream.started == 1, "错误须来自上游，否则测的是本地拒绝路径、看守不到本项"

    app.state.scenario.error = None  # 模拟"上游恢复"
    second = await client.post("/v1/chat/completions", json=_body(), headers=h)
    assert second.status_code == http, "上游永久性拒绝须入窗 → 同 key 重发回放缓存"
    assert second.json()["error"]["code"] == code
    assert app.state.upstream.started == 1, "回放不得重调上游"


@pytest.mark.parametrize(
    "bad_body",
    [
        pytest.param(lambda: _body(user_id="u-leak"), id="forbidden-field-400"),
        pytest.param(lambda: _body(model="persona:libai"), id="persona-prefix-400"),
    ],
)
async def test_t7_local_reject_not_cached(client, app, bad_body):
    """§8.2.1 三分表末行「本地拒绝 → 不入窗」：本地 400 后同 key 仍可被正常处理。

    结构事实（也是本用例要钉住的东西）：`main.py:98` 的 `guard_request(body)` **先于**
    `:109` 读取 `Idempotency-Key` 与 `:120` 的 `window.begin()` ⇒ 本地拒绝根本不进窗口。
    若后人把校验挪到 `window.begin()` 之后，第一次拒绝会占住在飞位/写入缓存，同 key 重发
    将回放 400 或直接 409 —— 客户端退避重试永远失败（§14.2 的必需配套）。
    """
    app.state.upstream.reset()
    h = {"Idempotency-Key": "idem-local-400"}
    first = await client.post("/v1/chat/completions", json=bad_body(), headers=h)
    assert first.status_code == 400
    assert first.json()["error"]["code"] == "invalid_request"
    assert app.state.upstream.started == 0, "本地拒绝不得触达上游"

    second = await client.post("/v1/chat/completions", json=_body(), headers=h)
    assert second.status_code == 200, "本地拒绝未入窗 → 同 key 重发应被正常处理（不是回放 400，也不是 409）"
    assert app.state.upstream.started == 1


# ---------------------------------------------------------------- T8 合规否决
async def test_t8_compliance_denied_not_bypassable(client):
    r = await client.post(
        "/v1/chat/completions",
        json=_body(routing={"allow_providers": ["openai"]}),
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "compliance_denied"


# ---------------------------------------------------------------- T9 版本自检
async def test_t9_capabilities_version_and_timeouts(client):
    d = (await client.get("/v1/capabilities")).json()
    assert d["protocol_version"] == 1
    to = d["features"]["timeouts"]
    # §8.1 红线8：模型服务上游总超时 < 平台流式空闲上限（90s）。
    # ⚠️ 措辞更正（v1.7-澄清5／W-3 §2-4）：90s 只是**流式三项**在"不发心跳/声明位缺失"
    # 那一档的对标（保守侧）；发心跳时流式三项标的对象是**绝对上限 180s**。本项
    # `upstream_total_s` **仅约束非流式**、按平台读超时口径，故固定对 90s——断言值本身
    # 仍然成立、不动（对标基准的修订见 W-2）。
    assert to["upstream_total_s"] < 90 and to["upstream_first_token_s"] <= to["upstream_total_s"]


# ------------------------------------------------- RB-3 capabilities 声明位（§8.1-2／§28.2）
async def test_rb3_capabilities_expose_first_content_budget(client):
    """RB-3-1（A1）：`features.timeouts` 必须含 `upstream_first_content_s`，且**值 == 实际配置**。

    平台启动断言据它对标红线 8（§8.1 硬约束 2，v1.7-澄清5 把它列进"缺键即失败"清单）。
    断言"存在"不够——把值写死成别的数（例如复制 first_token 的 15）平台就对标错了对象，
    故这里跟 `get_settings()` 的实际配置逐值比对。
    """
    from msvc.config import get_settings

    s = get_settings()
    to = (await client.get("/v1/capabilities")).json()["features"]["timeouts"]
    assert "upstream_first_content_s" in to, "首个正文 token 预算未暴露 ⇒ 平台缺键即失败（拒启动）"
    assert to["upstream_first_content_s"] == s.upstream_first_content_timeout
    # 与兄弟键同处、且四项齐全（平台侧按四项做"缺键即失败"）
    assert set(to) >= {
        "upstream_first_token_s",
        "upstream_first_content_s",
        "upstream_chunk_idle_s",
        "upstream_total_s",
    }


async def test_rb3_stream_keepalive_false_for_fake_upstream(client):
    """RB-3-2（A2 方向一）：`MS_UPSTREAM=fake` ⇒ **声明不发心跳**（`false`）。

    §28.2 的 A 裁定：平台据本键决定流式内层预算对标 90s（空闲上限）还是 180s（绝对上限）。
    出厂默认部署就是假上游（`deploy/docker-compose.yml` 的 `${MS_UPSTREAM:-fake}`），而假上游
    只在 Scenario 显式打开心跳开关时才发 `: keepalive`、生产建 app 用的是默认 `Scenario()`
    ⇒ 一行心跳都没有。**此时若声明 `true`，平台会按 180s 对标而真实保护只有 90s**
    （虚假的安全感，比不做这个功能更坏）。
    """
    from msvc.config import get_settings

    assert get_settings().upstream == "fake", "本用例的前提是进程跑在假上游上"
    feats = (await client.get("/v1/capabilities")).json()["features"]
    assert "stream_keepalive" in feats, "声明位缺失 ⇒ 平台无法判定，缺键即失败（拒启动）"
    assert feats["stream_keepalive"] is False, (
        "假上游默认一行心跳都不发，声明 true 就是假声明（平台按 180s 对标、真实只有 90s）"
    )


async def test_rb3_stream_keepalive_true_for_litellm_upstream(client, monkeypatch):
    """RB-3-2（A2 方向二）：`MS_UPSTREAM=litellm` ⇒ 声明**会发心跳**（`true`）。

    与上一条**方向相反**，缺一即漏一类错误：只有上面那条，实现就能写死 `false` 而全绿——
    那样真实 litellm 部署会被平台按保守侧 90s 对标，一个合规的 120s 内层预算被误判违规、
    平台启动被拒（协议 §8.1 硬约束 2 说的正是这种误判）。

    推导依据（不是拍脑袋）：`LiteLLMProvider.stream` 对**任何**不产出正文、也不带
    `finish_reason` 的上游 chunk（reasoning-only 最常见）产出 `keepalive` 事件，
    `_handle_stream` 再渲染成 `: keepalive` 注释行。该实现本身由
    `test_real_upstream.py::test_rb3_litellm_provider_emits_keepalive_for_reasoning_chunk` 看守。
    """
    from msvc.config import get_settings

    monkeypatch.setattr(get_settings(), "upstream", "litellm")
    feats = (await client.get("/v1/capabilities")).json()["features"]
    assert feats["stream_keepalive"] is True, (
        "真实上游会发推理期心跳，声明 false 会让平台按 90s 保守对标、误拦合法的 120s 内层预算"
    )


# ---------------------------------------------------------------- T10 目录同步
async def test_t10_models_catalog(client):
    d = (await client.get("/v1/models")).json()
    assert d["object"] == "list"
    assert "deepseek-chat" in [m["id"] for m in d["data"]]


# ---------------------------------------------------------------- T11 超时分层（首 token）
async def test_t11_first_token_timeout(client, scenario):
    """T11 / §6.7：上游慢于首 token 超时 → **首字节前**返回 HTTP 504 `upstream_timeout`。

    v1.7（工作单 P1-9）改判：首字节**之前**的失败必须是 **HTTP 错误状态**，不得发成
    「200 + 流内错误帧」—— 后者会让平台的限流退避、`Retry-After` 透传、熔断计数全部失效。
    本用例原断言"响应文本里含 upstream_timeout"，在 JSON 错误体下会**假绿**（不含状态码
    约束），故一并收紧为断言状态码 + code + retryable。
    """
    from msvc.config import get_settings

    scenario.first_token_seconds = get_settings().upstream_first_token_timeout + 0.5
    r = await client.post("/v1/chat/completions", json=_body(stream=True))
    assert r.status_code == 504, "首字节前的上游超时必须走 HTTP 错误状态（§6.7）"
    assert r.json()["error"]["code"] == "upstream_timeout"
    assert r.json()["error"]["retryable"] is False  # 结果不明 ⇒ 入窗（§8.2.1）


async def test_unauthorized_without_token(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ms") as c:
        r = await c.post("/v1/chat/completions", json=_body())  # 无 Authorization
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "service_auth_failed"
