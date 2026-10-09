"""模型服务 ASGI app（Starlette）——协议 §2 端点总览的服务端实现。

无状态（红线4）：不持会话/账本；只认请求，不认用户（红线1，见 guards）。
"""

import asyncio
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
from msvc.idempotency import CachedResponse, IdempotencyWindow
from msvc.provider import FakeUpstream, LiteLLMProvider, Scenario, UpstreamResult, _ScenarioError


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

    # 幂等仅对非流式生效（流式回放不在 §8.2 范围）。命中缓存直接回放；并发同 key → 409。
    if idem and not is_stream:
        cached = window.get(idem)
        if cached is not None:
            return JSONResponse(cached.body, status_code=cached.status, headers=cached.headers)
        if not window.begin(idem):
            return JSONResponse(
                {"error": {"message": "同 Idempotency-Key 正在处理", "type": "invalid_request_error", "code": "invalid_request", "param": None, "retryable": True}},
                status_code=409,
            )

    if not is_stream:
        return await _handle_nonstream(request, body, model, scenario, idem)
    return await _handle_stream(request, body, model, scenario)


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


async def _handle_stream(request, body, model, scenario) -> Response:
    upstream = request.app.state.upstream
    settings = get_settings()
    model_used = scenario.model_used or model

    async def event_gen():
        try:
            agen = upstream.stream(body, scenario).__aiter__()
            first = True
            while True:
                try:
                    timeout = settings.upstream_first_token_timeout if first else 1.0
                    ev = await asyncio.wait_for(agen.__anext__(), timeout=timeout)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    yield _sse({"error": {"message": "上游首 token 超时", "type": "api_error", "code": "upstream_timeout", "param": None, "retryable": False}})
                    return
                first = False
                kind = ev.get("kind")
                if kind == "chunk":
                    yield _sse(_chunk(ev["content"], model_used))
                elif kind == "usage":
                    yield _sse({"id": "chatcmpl-ms", "object": "chat.completion.chunk", "model": model_used, "choices": [], "usage": ev["usage"]})
                elif kind == "terminate":
                    yield _sse({"x_model_service": ev["x_model_service"]}, event="model_service")
                elif kind == "done":
                    yield _sse("[DONE]")
                    return
        except _ScenarioError as exc:
            err = await _map_upstream_error(exc)
            yield _sse({"error": {"message": err.message, "type": err.err_type, "code": err.code, "param": None, "retryable": err.retryable}})
        except asyncio.CancelledError:
            # 红线6：平台断连 → 取消传播到上游（FakeUpstream 已记 cancelled）
            raise

    return StreamingResponse(event_gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


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
    app.state.window = IdempotencyWindow(get_settings().idempotency_window_seconds)
    app.state.scenario = Scenario()
    return app


app = create_app()
