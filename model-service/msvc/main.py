"""模型服务 ASGI app（Starlette）——协议 §2 端点总览的服务端实现。

无状态（红线4）：不持会话/账本；只认请求，不认用户（红线1，见 guards）。
"""

import asyncio
import contextlib
import json
import time
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from msvc.auth import require_auth
from msvc.config import get_settings
from msvc.errors import CODE_HTTP, ServiceError, from_upstream_exception
from msvc.guards import guard_request
from msvc.idempotency import CachedResponse, IdempotencyWindow, StreamTerminal
from msvc.provider import FakeUpstream, LiteLLMProvider, Scenario, UpstreamResult, _ScenarioError

SSE_HEADERS = {"Cache-Control": "no-cache"}


def _make_upstream():
    return LiteLLMProvider() if get_settings().upstream == "litellm" else FakeUpstream()


def _map_upstream_error(exc: BaseException) -> ServiceError:
    """把上游异常映射为 ServiceError。

    - `_ScenarioError`：假桩的声明式错误（保留 code/retryable/retry_after 语义）；
    - `ServiceError`：真实 provider 已映射过，原样透传；
    - **其余一切异常**：走 `from_upstream_exception`（SW-4）——真实 litellm 异常族此前
      无人认领，直接逃逸成裸 500，并让幂等在飞位永久泄漏。
    """
    if isinstance(exc, ServiceError):
        return exc
    if isinstance(exc, _ScenarioError):
        payload: dict[str, Any] = exc.payload
        return ServiceError(
            payload.get("code", "upstream_error"),
            payload.get("message", f"上游错误 {payload.get('code', 'upstream_error')}"),
            err_type=payload.get("type", "api_error"),
            param=payload.get("param"),
            retry_after=payload.get("retry_after"),
            retryable=bool(payload.get("retryable", False)),
        )
    return from_upstream_exception(exc)


def _build_completion(res: UpstreamResult, requested_model: str) -> dict:
    body: dict[str, Any] = {
        "id": "chatcmpl-ms",
        "object": "chat.completion",
        "model": res.x_model_service.get("model_used") or requested_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": res.content},
                # §6.6：真实上游给什么就记什么（`stop`/`length`），不再硬编码 `stop`。
                "finish_reason": getattr(res, "finish_reason", None) or "stop",
            }
        ],
    }
    if res.usage:
        body["usage"] = res.usage
    if res.x_model_service:  # absent → 整块不给（Proxy 形状）
        body["x_model_service"] = res.x_model_service
    return body


def _sse(obj: Any, event: str | None = None) -> str:
    prefix = f"event: {event}\n" if event else ""
    payload = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return f"{prefix}data: {payload}\n\n"


def _chunk(content: str | None, model: str, finish: str | None = None) -> dict:
    return {
        "id": "chatcmpl-ms",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {"content": content} if content else {}, "finish_reason": finish}],
    }


def _usage_chunk(usage: dict, model: str) -> dict:
    return {
        "id": "chatcmpl-ms",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [],
        "usage": usage,
    }


def _error_payload(
    code: str,
    message: str,
    *,
    err_type: str = "api_error",
    param: str | None = None,
    retryable: bool = False,
    retry_after_s: int | None = None,
) -> dict:
    """§7 错误体（流内错误帧的 `error` 值）。

    `retry_after_s`（v1.7-澄清2 加法字段，SW-2）：**流内错误帧没有 HTTP header 可带**，
    这是流式路径传递退避时长的唯一手段。**只有上游真给了值才出现该键**——缺省不给，
    不得凭空造一个 0 或默认值（否则平台会把"没有退避信息"误读成"立刻可重试"）。
    """
    payload: dict[str, Any] = {
        "message": message,
        "type": err_type,
        "code": code,
        "param": param,
        "retryable": retryable,
    }
    if retry_after_s is not None:
        payload["retry_after_s"] = int(retry_after_s)
    return payload


def _error_event(err: dict, usage: dict | None = None) -> dict:
    """流内错误帧（§6.7：无 `event:` 名，与 OpenAI 一致）。

    G3-2：错误帧**可**携带实测部分 `usage`（加法字段）——错误终止时上游已产出部分
    token 且**已计费**，服务侧明知实测值，就不该让平台降档估算。
    """
    frame: dict[str, Any] = {"error": err}
    if usage:
        frame["usage"] = usage
    return frame


def _check_compliance(body: dict) -> None:
    deny = get_settings().deny_providers
    routing = body.get("routing") or {}
    allow = [str(p).lower() for p in (routing.get("allow_providers") or [])]
    model = str(body.get("model", ""))
    model_provider = model.split("/")[0].lower() if "/" in model else ""
    hit = [p for p in allow if p in deny] or ([model_provider] if model_provider in deny else [])
    if hit:
        raise ServiceError(
            "compliance_denied",
            f"合规策略否决：provider {hit} 被禁（调用方不可放宽，红线5）",
            err_type="compliance_error",
            retryable=False,
        )


async def chat_completions(request: Request) -> Response:
    app_state = request.app.state
    settings = get_settings()
    try:
        try:
            body = await request.json()
        except Exception as exc:
            raise ServiceError(
                "invalid_request", f"请求体不是合法 JSON：{exc}",
                err_type="invalid_request_error",
            ) from exc
        if not isinstance(body, dict):
            raise ServiceError(
                "invalid_request", "请求体必须是 JSON object", err_type="invalid_request_error"
            )
        require_auth(request.headers.get("Authorization"))
        guard_request(body)  # 红线1/10：P0-3 入站禁字段校验（先于任何上游调用）
        _check_compliance(body)

        model = body.get("model", "")
        if model and model not in settings.models and "/" not in model:
            raise ServiceError(
                "model_not_found", f"模型 {model} 不在本服务目录", err_type="invalid_request_error", param="model"
            )
    except ServiceError as e:
        return e.to_response()
    except Exception as exc:
        # SW-4：入口段的任何意外异常也要产出 §7 的 code，**不得**留裸 500 给 ASGI 默认处理器。
        # 此处尚未 begin()，没有在飞位需要释放。
        return _map_upstream_error(exc).to_response()

    idem = request.headers.get("Idempotency-Key")
    window: IdempotencyWindow = app_state.window
    scenario: Scenario = getattr(app_state, "scenario", Scenario())

    is_stream = bool(body.get("stream"))

    # 幂等窗对「流式 / 非流式」同等适用（协议 §8.2 告示 + §8.2.2，v1.7 定案）。
    # 流式**成功**终止、以及**已发起上游调用后**的流内错误帧终止，同样必然已调用上游
    # （成功必已计费），按 §8.2.1 三分表属「必须入窗」；实现**不得**以"流式回放不便"
    # 为由把流式排除在窗口之外（那是未写明的范围收缩，见 §19.2/§20.1 的真实双烧证据）。
    # 命中缓存 → 终态重放（流式）/ 回放 JSON；同 key 并发 → 在飞位拒 409。
    # 顺序与非流式一致：**先 get 再 begin**（否则"命中缓存"会被误判成"并发在飞"）。
    if idem:
        cached = window.get(idem)
        if cached is not None:
            return _replay_cached(cached, model)
        if not window.begin(idem):
            return JSONResponse(
                {"error": {"message": "同 Idempotency-Key 正在处理", "type": "invalid_request_error", "code": "invalid_request", "param": None, "retryable": True}},
                status_code=409,
            )

    if not is_stream:
        return await _handle_nonstream(request, body, model, scenario, idem)
    return await _handle_stream(request, body, model, scenario, idem)


def _replay_cached(cached: CachedResponse, model: str) -> Response:
    """命中幂等窗：流式 → **终态重放**（§8.2.2-3）；非流式/首字节前错误 → 回放原 JSON。

    RB-5④（§8.2.1 连带项）：回放在平台侧原本**不可识别** ⇒ 每次回放 504 都让平台再计一次
    熔断（`cb_threshold=5` ⇒ 同一条消息重发 5 次就打开熔断、全站降级）。故回放响应一律带
    `Idempotency-Replayed: true`（**流式与非流式都在响应头**——流式的头在开流前就已发出，
    这是唯一能标记"这条流是副本"的位置）。不加 body 字段、不改 chunk 形状、不升线协议版本。
    """
    replayed = {"Idempotency-Replayed": "true"}
    if cached.stream is None:
        return JSONResponse(
            cached.body, status_code=cached.status, headers={**cached.headers, **replayed}
        )
    return StreamingResponse(
        _replay_stream(cached.stream, model),
        media_type="text/event-stream",
        headers={**SSE_HEADERS, **replayed},
    )


async def _replay_stream(t: StreamTerminal, model: str):
    """按缓存终态**重新生成**一条 SSE 流（不缓存事件序列，故 chunk 边界可与首次不同）。

    §8.2.2-3 要求一致的部分：**内容文本、usage、finish_reason、终止事件、`[DONE]`**。
    """
    if t.content:
        yield _sse(_chunk(t.content, model))
    if t.error is not None:
        frame: dict[str, Any] = {"error": t.error}
        if t.usage:  # §6.7 G3-2：错误帧可携带实测部分 usage，回放时保持携带
            frame["usage"] = t.usage
        yield _sse(frame)
    else:
        if t.finish_reason:
            yield _sse(_chunk(None, model, t.finish_reason))
        if t.usage:
            yield _sse(_usage_chunk(t.usage, model))
        if t.x_model_service:
            yield _sse({"x_model_service": t.x_model_service}, event="model_service")
    yield _sse("[DONE]")


def _cache_stream_terminal(window: IdempotencyWindow, idem: str, terminal: StreamTerminal) -> None:
    """首字节已过并到达终态 ⇒ 上游已产出（已计费）⇒ **必须入窗**（§8.2.2-1）。

    此处**不走** `should_cache` 三元判定：§8.2.1 把「上游瞬时拒绝」排除在入窗之外的
    **理由**是"未处理未计费、重发可能成功"，而首字节之后该理由不成立（token 已消耗、
    已计费）。§8.2.2-1 的硬条款把"已发起上游调用后的终止"一律归入必须入窗。
    """
    window.finish(
        idem,
        CachedResponse(
            200,
            {},
            {},
            time.monotonic()
            # RB-5（§8.2.1 TTL 表）：错误帧终止的流属「结果不明／已发起调用后的失败」⇒ 短窗；
            # 成功终止才用长窗回放答案。
            + (window.error_ttl_seconds if terminal.error is not None else window.window_seconds),
            stream=terminal,
        ),
    )


async def _aclose(agen) -> None:
    """首字节前失败时收掉上游生成器（红线6：不让上游调用悬空）。"""
    with contextlib.suppress(Exception):
        await agen.aclose()


async def _handle_nonstream(request, body, model, scenario, idem) -> Response:
    settings = get_settings()
    window = request.app.state.window
    upstream = request.app.state.upstream
    reached_upstream = False
    try:
        reached_upstream = True  # 已进入上游调用（超时/5xx 属"结果不明"）
        res = await asyncio.wait_for(upstream.complete(body, scenario), timeout=settings.upstream_total_timeout)
    except asyncio.TimeoutError:
        err = ServiceError("upstream_timeout", "上游总超时（自持，§8.1）", retryable=False)
    except Exception as exc:  # SW-4 catch-all：ServiceError 直透、其余按 §7 映射，**绝不裸抛**
        err = _map_upstream_error(exc)
    else:
        resp = JSONResponse(_build_completion(res, model))
        _finish_window(window, idem, 200, False, reached_upstream, resp, cache_body=True)
        return resp
    _finish_window(window, idem, err.http_status, err.retryable, reached_upstream, err.to_response())
    return err.to_response()


def _finish_window(window, idem, status, retryable, reached_upstream, response, *, cache_body=False):
    """终结幂等窗：**任何情况下都必须释放在飞位**（SW-4 硬条款）。

    入窗只是"顺带缓存"；在飞位漏放会让同 key 之后恒 409（兼容面指纹键 ⇒ 用户的同一条
    消息在进程重启前永久发不出）。故本函数**自带兜底**：入窗逻辑若抛异常，退化为
    `finish(idem, None)`（不入窗但清在飞），绝不让异常把在飞位留在集合里。
    """
    if not idem:
        return
    try:
        should = window.should_cache(status, retryable, reached_upstream)
        if should:
            body = response_body(response)
            headers = _response_headers(response)
            # RB-5（§8.2.1 TTL 表／§28.3）：**入窗时长分类**。类目"结果不明"= 超时类与
            # 已发起调用后的 5xx，一律用短 TTL；4xx 永久性拒绝（413／上游 404）同输入必同
            # 拒绝，是"稳定答案"，留长窗。判据只用 status 的这一段——**入不入窗**仍由
            # `should_cache` 的两个布尔决定（§8.2.1 禁止用状态码白名单判入窗，这里没有判入窗）。
            ttl = window.window_seconds if status < 500 else window.error_ttl_seconds
            window.finish(
                idem,
                CachedResponse(status, body, headers, time.monotonic() + ttl),
            )
            return
    except Exception:  # 记账失败不能连带泄漏在飞位
        pass
    window.finish(idem, None)  # 不入窗 / 兜底：仅清除在飞标记


def response_body(response: JSONResponse) -> dict:
    import json as _json

    return _json.loads(response.body.decode())


def _response_headers(response: JSONResponse) -> dict[str, str]:
    return {k: v for k, v in response.headers.items() if k.lower() == "retry-after"}


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """SW-4 兜底：任何逃逸到 ASGI 层的异常都渲染成 §7 错误体，绝不裸 500。"""
    return _map_upstream_error(exc).to_response()


async def _handle_stream(request, body, model, scenario, idem) -> Response:
    upstream = request.app.state.upstream
    window: IdempotencyWindow = request.app.state.window
    settings = get_settings()
    model_used = scenario.model_used or model

    # **首字节是否已发出**是流式错误语义的分界线（§6.7）：首字节之前（尚未向客户端写出
    # 任何事件）必须返回 **HTTP 错误状态**（同 §7 表），不得用「200 + 流内错误帧」代替
    # —— 否则平台的限流退避、`Retry-After` 透传、熔断计数全部失效，且入窗分类也会失真
    # （瞬时拒绝本该不入窗）。故先向上游取**第一个事件**，成功后才构造 StreamingResponse。
    #
    # RB-1（协议 §28.1）两条预算在此分工：
    #  · **首 token**：上游**任何** chunk 都复位它（provider 把 reasoning-only chunk 转成
    #    `keepalive` 事件，§28.1 明确"定义不改、改实现"）⇒ 它测的是"上游到底有没有在吐字"；
    #  · **首个正文 token**：从请求开始的**绝对**截止（含全部推理时长）⇒ 它测的是
    #    "一直推理、始终不出正文"。
    # 首字节之前没有任何事件 ⇒ 归因只能是 `upstream_timeout`（我们**不知道**它在推理）；
    # `upstream_reasoning_timeout` 在"有事件（含心跳）但无正文"时触发，那时首字节已过，
    # 按 §6.7 只能以流内错误帧收尾（见 `event_gen`）。
    content_deadline = time.monotonic() + settings.upstream_first_content_timeout
    agen = upstream.stream(body, scenario).__aiter__()
    try:
        first_ev = await asyncio.wait_for(
            agen.__anext__(),
            timeout=min(settings.upstream_first_token_timeout,
                        max(content_deadline - time.monotonic(), 0.001)),
        )
    except StopAsyncIteration:
        first_ev = None
    except asyncio.TimeoutError:
        await _aclose(agen)
        err = ServiceError("upstream_timeout", "上游首 token 超时（自持，§8.1）", retryable=False)
        # 首字节前的失败 → 走 §8.2.1 三分表（结果不明 ⇒ 入窗；瞬时拒绝 ⇒ 不入窗）
        _finish_window(window, idem, err.http_status, err.retryable, True, err.to_response())
        return err.to_response()
    except Exception as exc:
        # SW-4 catch-all（首字节**前**）：真实 litellm 异常族此前从这里逃逸成裸 500，
        # 且因为没走 `_finish_window` 而把幂等在飞位永久留在集合里（同 key 恒 409）。
        await _aclose(agen)
        err = _map_upstream_error(exc)
        _finish_window(window, idem, err.http_status, err.retryable, True, err.to_response())
        return err.to_response()

    async def event_gen():
        # 流式**终态**（§8.2.2-3）：成功终止与「已发起上游调用后」的错误帧终止都要入窗，
        # 故这里累积的是**终态**（内容文本 + 一份 usage + finish_reason + 终止事件 +
        # 错误帧），**不是**事件序列 —— 后者会随生成长度无界增长。
        terminal = StreamTerminal()
        reached_terminal = False
        # RB-2（§6-4「推理期间必须发」／§8.2.2-3）：心跳是**过程字节**，不是终态的一部分
        # ⇒ 绝不写进 `terminal`（否则被终态重放缓存收下并重放，还会污染
        # `MS_IDEMPOTENCY_CACHE_MAX_CHARS` 的体积核算）。
        content_seen = bool(first_ev and first_ev.get("kind") == "chunk" and first_ev.get("content"))
        try:
            ev = first_ev
            while ev is not None:
                kind = ev.get("kind")
                if kind == "keepalive":
                    # `_sse()` 只会产 `event:`/`data:` 两种行，注释行必须**绕开**它直接写。
                    yield ": keepalive\n\n"
                elif kind == "chunk":
                    terminal.content += ev["content"]
                    content_seen = True
                    yield _sse(_chunk(ev["content"], model_used))
                elif kind == "finish":
                    # §6.6（工作单 P1-8）：收尾 chunk 必须携带**非 null** finish_reason，且位于
                    # usage chunk **之前**。此前服务侧从不发它 ⇒ 平台的 StreamDone.finish_reason
                    # 恒 None、P1-1 归因在生产路径静默失效（平台自家桩已发，故其测试全绿 = 桩分歧）。
                    terminal.finish_reason = ev["finish_reason"]
                    yield _sse(_chunk(None, model_used, ev["finish_reason"]))
                elif kind == "usage":
                    terminal.usage = ev["usage"]
                    yield _sse(_usage_chunk(ev["usage"], model_used))
                elif kind == "terminate":
                    terminal.x_model_service = ev["x_model_service"]
                    yield _sse({"x_model_service": ev["x_model_service"]}, event="model_service")
                elif kind == "done":
                    break
                # 取下一个事件：**首字节之后**的错误一律以流内错误帧收尾（状态码已无法改）。
                # O1（v1.7）：中途停顿用**独立可配**预算 `upstream_chunk_idle_timeout`
                # （默认 30s），**不与首 token 超时共用硬编码值**——旧实现硬编码 1.0s，
                # 真实上游中途正常停顿 >1s 即被误杀，且归因文案错写成"首 token 超时"。
                # RB-1：正文尚未出现时，还要同时盯着「首个正文 token」的**绝对**截止。
                # ⚠️ 归因**不能**在超时之后再读一次时钟来判断"哪个预算到了"：asyncio.wait_for
                # 用的是事件循环每轮**缓存**的时钟，可能比 time.monotonic() 早触发几毫秒，于是
                # 内容预算明明先到却被判成 upstream_timeout（本机实测：26 条心跳、最大间隔 32ms、
                # 全程没有 0.3s 沉默，却误报首 token 超时）。正确做法：等待**之前**就记下这次由
                # 哪个预算封顶——remote.py 的 P1-6 归因用的也是同一招（别二次读时钟）。
                wait_for_budget = (
                    # 正文出现前：**上游任何 chunk（含 reasoning-only → keepalive 事件）都复位**
                    # 首 token 预算（§28.1「定义不改、改实现」），所以这里的"沉默多久算失败"
                    # 用的是 first_token 而非 chunk_idle。
                    settings.upstream_first_token_timeout
                    if not content_seen
                    # 正文之后：中途停顿预算 O1（默认 30s，独立可配）。
                    else settings.upstream_chunk_idle_timeout
                )
                bound_by_content = False
                if not content_seen:
                    remaining_content = content_deadline - time.monotonic()
                    if remaining_content <= 0:
                        terminal.error = _error_payload(
                            "upstream_reasoning_timeout",
                            f"上游在 {settings.upstream_first_content_timeout}s 内只推理未产出正文"
                            "（§8.1 首个正文 token 预算，含全部推理时长；与首 token 超时区分归因）",
                            retryable=False,
                        )
                        await _aclose(agen)  # 红线6
                        break
                    bound_by_content = remaining_content <= wait_for_budget
                    wait_for_budget = min(wait_for_budget, remaining_content)
                try:
                    ev = await asyncio.wait_for(agen.__anext__(), timeout=wait_for_budget)
                except StopAsyncIteration:
                    ev = None
                except asyncio.TimeoutError:
                    # RB-1 归因（§28.1／§7 表）：三种沉默是三件不同的事，压成一条就白分了——
                    #  · 正文始终没出现**且**外层内容预算到了 ⇒ `upstream_reasoning_timeout`
                    #    （上游在推理、预算不够 ⇒ 处置是放大 first_content）；
                    #  · 正文没出现、内容预算也还没到，只是上游连着 >first_token 没吐 chunk
                    #    ⇒ 仍是首 token 超时 `upstream_timeout`（上游连不上/不吐字，该早失败）；
                    #  · 正文已出现后的停顿 ⇒ `upstream_chunk_timeout`（O1）。
                    if bound_by_content:
                        terminal.error = _error_payload(
                            "upstream_reasoning_timeout",
                            f"上游在 {settings.upstream_first_content_timeout}s 内只推理未产出正文"
                            "（§8.1 首个正文 token 预算，含全部推理时长；与首 token 超时区分归因）",
                            retryable=False,
                        )
                    elif not content_seen:
                        terminal.error = _error_payload(
                            "upstream_timeout",
                            f"上游首 token 超时（自持，§8.1；预算 {settings.upstream_first_token_timeout}s），"
                            "正文出现前上游连一个 chunk 都没吐",
                            retryable=False,
                        )
                    else:
                        terminal.error = _error_payload(
                            "upstream_chunk_timeout",
                            f"上游中途停顿超过 {settings.upstream_chunk_idle_timeout}s 未产出下一个事件"
                            "（§8.1 中途空闲预算，与首 token 超时区分归因）",
                            retryable=False,
                        )
                    await _aclose(agen)  # 红线6：别让上游生成器悬空
                    break
                except Exception as exc:
                    # SW-4：假桩的 `_ScenarioError`、真实 provider 的 `ServiceError`、
                    # 以及任何未映射异常，都在此收敛成 §7 错误帧（此前只认 `_ScenarioError`，
                    # 真实上游中途报错会**逃出** event_gen ⇒ 既无错误帧也无 `[DONE]`）。
                    err = _map_upstream_error(exc)
                    terminal.error = err.to_payload()  # 含 retry_after_s（§6.7 流式唯一退避通道）
                    # §6.7 G3-2：错误帧**可**携带实测部分 usage（加法字段）
                    usage_extra = getattr(exc, "payload", {})
                    if isinstance(usage_extra, dict) and isinstance(usage_extra.get("usage"), dict):
                        terminal.usage = usage_extra["usage"]
                    await _aclose(agen)
                    break
            if terminal.error is not None:
                yield _sse(_error_event(terminal.error, terminal.usage))
            # §6.7 G3-1：错误帧之后**必须仍发** `[DONE]` —— 平台循环以 `[DONE]` 为唯一
            # 正常终止信号（否则客户端解析器无法区分"错误收尾"与"连接异常断开"）；
            # 同时这是「终态重放」一致性的前提（回放必须与首次形态相同）。
            yield _sse("[DONE]")
            reached_terminal = True
        except asyncio.CancelledError:
            # 红线6：平台断连 → 取消传播到上游（FakeUpstream 已记 cancelled）
            raise
        finally:
            if idem:
                if reached_terminal:
                    # 首字节已过并到达终态 ⇒ 上游已产出（已计费）⇒ 必须入窗（§8.2.2-1）
                    _cache_stream_terminal(window, idem, terminal)
                else:
                    # 客户端断连/异常：终态未知 → 不入窗，仅清在飞位（§6.5 由平台 abandoned 收编）
                    window.finish(idem, None)

    return StreamingResponse(event_gen(), media_type="text/event-stream", headers=SSE_HEADERS)


async def capabilities(request: Request) -> Response:
    """§8.1-2 / §28.2：向平台自述**实际**超时值与「是否真会发心跳」。

    `stream_keepalive` 是**推导值、不是写死的常量**（RB-3，工作单 W-1 架构师裁定）：
    平台据它决定流式内层预算对标 90s（空闲上限）还是 180s（绝对上限），
    写死 `true` 就是一份**假声明** ⇒ 平台按 180s 对标、真实保护只有 90s，
    比不做这个功能更坏（虚假的安全感）。故本键必须与"本进程是否真会发心跳"一致：

    - `MS_UPSTREAM=litellm` ⇒ **会发**。`LiteLLMProvider.stream` 对**任何**不产出正文、
      也不带 `finish_reason` 的上游 chunk（reasoning-only 最常见）产出 `keepalive` 事件，
      `_handle_stream` 再渲染成 `: keepalive` 注释行（§28.1／§28.2）；
    - `MS_UPSTREAM=fake` ⇒ **不发**。假上游只在 `Scenario` 显式打开
      `reasoning_only_chunks` / `keepalive_during_silence` 时产心跳，而生产建 app 用的是
      默认 `Scenario()`（`create_app`）⇒ 一行心跳都没有。**出厂默认部署就是 fake**
      （`deploy/docker-compose.yml` 的 `${MS_UPSTREAM:-fake}`、`deploy/.env.example`）,
      所以键的默认值是 `false`。

    该推导的**另一半防线**在用例里：`tests/test_contract.py` 锁"声明位 == 实际 provider"，
    `tests/test_real_upstream.py` 锁"litellm provider 对 reasoning-only chunk 真的产心跳"
    ——两条合起来才排除"声明说有、实现没有"。
    """
    s = get_settings()
    return JSONResponse(
        {
            "protocol_version": s.protocol_version,
            "features": {
                "stream_terminate_event": True,
                "cost_attribution": True,
                "routing_hints": True,
                "compliance_policies": ["cn_only_providers"],
                "idempotency_window_seconds": s.idempotency_window_seconds,
                # §28.2：声明"推理静默期会发 `: keepalive`"（推导值，见 docstring）。
                "stream_keepalive": s.upstream == "litellm",
                # §8.1-2：暴露实际超时值供平台启动断言「内层 < 外层」。
                # 四项缺一不可：平台侧**缺键即失败**（v1.7-澄清5），不再"缺失即跳过"。
                "timeouts": {
                    "upstream_first_token_s": s.upstream_first_token_timeout,
                    "upstream_first_content_s": s.upstream_first_content_timeout,
                    "upstream_chunk_idle_s": s.upstream_chunk_idle_timeout,
                    "upstream_total_s": s.upstream_total_timeout,
                },
            },
            "implementation": {"name": s.implementation_name, "version": s.implementation_version},
        }
    )


async def list_models(request: Request) -> Response:
    s = get_settings()
    return JSONResponse({"object": "list", "data": [{"id": m, "object": "model"} for m in s.models]})


async def healthz(request: Request) -> Response:
    return JSONResponse({"status": "ok", "upstream": request.app.state.upstream.name})


async def liveness(request: Request) -> Response:
    return JSONResponse({"status": "alive"})


async def readiness(request: Request) -> Response:
    return JSONResponse({"status": "ready"})


def create_app() -> Starlette:
    settings = get_settings()
    app = Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/health/liveness", liveness),
            Route("/health/readiness", readiness),
            Route("/v1/capabilities", capabilities),
            Route("/v1/models", list_models),
            Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        ],
        # SW-4 最后一道网：连 handler 都没兜住的异常也必须产出 §7 错误体（裸 500 会让
        # 平台拿不到 `error.code`，退避/熔断/归因全瞎）。
        exception_handlers={Exception: _unhandled_exception_handler},
    )
    app.state.upstream = _make_upstream()
    app.state.window = IdempotencyWindow(
        settings.idempotency_window_seconds,
        max_entries=settings.idempotency_cache_max_entries,
        max_chars=settings.idempotency_cache_max_chars,
        inflight_ttl_seconds=settings.idempotency_inflight_ttl_seconds,
        error_ttl_seconds=settings.idempotency_error_ttl_seconds,
    )
    app.state.scenario = Scenario()
    return app


app = create_app()
