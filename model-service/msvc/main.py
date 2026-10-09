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
from msvc.errors import CODE_HTTP, ServiceError
from msvc.guards import guard_request
from msvc.idempotency import CachedResponse, IdempotencyWindow, StreamTerminal
from msvc.provider import FakeUpstream, LiteLLMProvider, Scenario, UpstreamResult, _ScenarioError

SSE_HEADERS = {"Cache-Control": "no-cache"}


def _make_upstream():
    return LiteLLMProvider() if get_settings().upstream == "litellm" else FakeUpstream()


async def _map_upstream_error(exc: _ScenarioError) -> ServiceError:
    payload: dict[str, Any] = exc.payload
    code = payload.get("code", "upstream_error")
    retryable = bool(payload.get("retryable", False))
    return ServiceError(
        code,
        payload.get("message", f"上游错误 {code}"),
        err_type=payload.get("type", "api_error"),
        retry_after=payload.get("retry_after"),
        retryable=retryable,
    )


def _build_completion(res: UpstreamResult, requested_model: str) -> dict:
    body: dict[str, Any] = {
        "id": "chatcmpl-ms",
        "object": "chat.completion",
        "model": res.x_model_service.get("model_used") or requested_model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": res.content},
                "finish_reason": "stop",
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
) -> dict:
    """§7 错误体（流内错误帧的 `error` 值）。"""
    return {
        "message": message,
        "type": err_type,
        "code": code,
        "param": param,
        "retryable": retryable,
    }


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
        body = await request.json()
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
    """命中幂等窗：流式 → **终态重放**（§8.2.2-3）；非流式/首字节前错误 → 回放原 JSON。"""
    if cached.stream is None:
        return JSONResponse(cached.body, status_code=cached.status, headers=cached.headers)
    return StreamingResponse(
        _replay_stream(cached.stream, model), media_type="text/event-stream", headers=SSE_HEADERS
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
        CachedResponse(200, {}, {}, time.monotonic() + window.window_seconds, stream=terminal),
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
        _finish_window(window, idem, err.http_status, err.retryable, reached_upstream, err.to_response())
        return err.to_response()
    except _ScenarioError as exc:
        err = await _map_upstream_error(exc)
        _finish_window(window, idem, err.http_status, err.retryable, reached_upstream, err.to_response())
        return err.to_response()
    except ServiceError as err:
        _finish_window(window, idem, err.http_status, err.retryable, reached_upstream, err.to_response())
        return err.to_response()

    resp = JSONResponse(_build_completion(res, model))
    _finish_window(window, idem, 200, False, reached_upstream, resp, cache_body=True)
    return resp


def _finish_window(window, idem, status, retryable, reached_upstream, response, *, cache_body=False):
    if not idem:
        return
    should = window.should_cache(status, retryable, reached_upstream)
    if should and cache_body:
        window.finish(idem, CachedResponse(status, response_body(response), {}, time.monotonic() + window.window_seconds))
    elif should and not cache_body:
        window.finish(idem, CachedResponse(status, _error_body(response), _response_headers(response), time.monotonic() + window.window_seconds))
    else:
        window.finish(idem, None)  # 不入窗：仅清除在飞标记


def response_body(response: JSONResponse) -> dict:
    import json as _json

    return _json.loads(response.body.decode())


def _error_body(response: JSONResponse) -> dict:
    return response_body(response)


def _response_headers(response: JSONResponse) -> dict[str, str]:
    return {k: v for k, v in response.headers.items() if k.lower() == "retry-after"}


async def _handle_stream(request, body, model, scenario, idem) -> Response:
    upstream = request.app.state.upstream
    window: IdempotencyWindow = request.app.state.window
    settings = get_settings()
    model_used = scenario.model_used or model

    # **首字节是否已发出**是流式错误语义的分界线（§6.7）：首字节之前（尚未向客户端写出
    # 任何事件）必须返回 **HTTP 错误状态**（同 §7 表），不得用「200 + 流内错误帧」代替
    # —— 否则平台的限流退避、`Retry-After` 透传、熔断计数全部失效，且入窗分类也会失真
    # （瞬时拒绝本该不入窗）。故先向上游取**第一个事件**，成功后才构造 StreamingResponse。
    agen = upstream.stream(body, scenario).__aiter__()
    try:
        first_ev = await asyncio.wait_for(
            agen.__anext__(), timeout=settings.upstream_first_token_timeout
        )
    except StopAsyncIteration:
        first_ev = None
    except asyncio.TimeoutError:
        await _aclose(agen)
        err = ServiceError("upstream_timeout", "上游首 token 超时（自持，§8.1）", retryable=False)
        # 首字节前的失败 → 走 §8.2.1 三分表（结果不明 ⇒ 入窗；瞬时拒绝 ⇒ 不入窗）
        _finish_window(window, idem, err.http_status, err.retryable, True, err.to_response())
        return err.to_response()
    except _ScenarioError as exc:
        await _aclose(agen)
        err = await _map_upstream_error(exc)
        _finish_window(window, idem, err.http_status, err.retryable, True, err.to_response())
        return err.to_response()
    except ServiceError as err:
        await _aclose(agen)
        _finish_window(window, idem, err.http_status, err.retryable, True, err.to_response())
        return err.to_response()

    async def event_gen():
        # 流式**终态**（§8.2.2-3）：成功终止与「已发起上游调用后」的错误帧终止都要入窗，
        # 故这里累积的是**终态**（内容文本 + 一份 usage + finish_reason + 终止事件 +
        # 错误帧），**不是**事件序列 —— 后者会随生成长度无界增长。
        terminal = StreamTerminal()
        reached_terminal = False
        try:
            ev = first_ev
            while ev is not None:
                kind = ev.get("kind")
                if kind == "chunk":
                    terminal.content += ev["content"]
                    yield _sse(_chunk(ev["content"], model_used))
                elif kind == "usage":
                    terminal.usage = ev["usage"]
                    yield _sse(_usage_chunk(ev["usage"], model_used))
                elif kind == "terminate":
                    terminal.x_model_service = ev["x_model_service"]
                    yield _sse({"x_model_service": ev["x_model_service"]}, event="model_service")
                elif kind == "done":
                    break
                # 取下一个事件：**首字节之后**的错误一律以流内错误帧收尾（状态码已无法改）
                try:
                    ev = await asyncio.wait_for(agen.__anext__(), timeout=1.0)
                except StopAsyncIteration:
                    ev = None
                except asyncio.TimeoutError:
                    terminal.error = _error_payload(
                        "upstream_timeout", "上游首 token 超时", retryable=False
                    )
                    break
                except _ScenarioError as exc:
                    err = await _map_upstream_error(exc)
                    terminal.error = _error_payload(
                        err.code, err.message, err_type=err.err_type, param=err.param,
                        retryable=err.retryable,
                    )
                    # §6.7 G3-2：错误帧**可**携带实测部分 usage（加法字段）
                    if isinstance(exc.payload.get("usage"), dict):
                        terminal.usage = exc.payload["usage"]
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
                # §8.1-2：暴露实际超时值供平台启动断言「内层 < 外层」
                "timeouts": {
                    "upstream_first_token_s": s.upstream_first_token_timeout,
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
    app = Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/health/liveness", liveness),
            Route("/health/readiness", readiness),
            Route("/v1/capabilities", capabilities),
            Route("/v1/models", list_models),
            Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        ]
    )
    app.state.upstream = _make_upstream()
    app.state.window = IdempotencyWindow(
        get_settings().idempotency_window_seconds,
        max_entries=get_settings().idempotency_cache_max_entries,
        max_chars=get_settings().idempotency_cache_max_chars,
    )
    app.state.scenario = Scenario()
    return app


app = create_app()
