"""fake model-service 桩（协议 §10.2）。

用途：平台侧按 `docs/model-service-protocol.md` §10 的 T1–T11 清单验收 `RemoteBackend`，
无需真实模型服务。可编程预设行为（错误注入 / 慢响应 / 断连模拟），并记录调用供断言。

路径说明：协议 §10.2 写的是 `model-service/tests/fake/`。本仓库实际放在
`server/tests/` 下，理由：① `server/pyproject.toml` 的 `testpaths=["tests"]` 与测试导入
路径（CWD=server）都锚在这一层，放别处会被收集不到或导入失败；② 强制维持"平台之外"
这一协议本意——它不在 `server/app/` 内、被平台代码 import 会显得很怪，只被测试使用。
文件名刻意避开 `app.py`：pytest 默认导入模式下会与 `server/app/` 包名冲突。

设计：一个 Starlette ASGI app + 进程内 `Control` 状态对象。桩只做「协议规定必须是什么」，
不掺入任何平台逻辑。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

# ---------------------------------------------------------------- 错误 code 表（协议 §7）

_CODE_HTTP: dict[str, int] = {
    "invalid_request": 400,
    "service_auth_failed": 401,
    "upstream_auth_failed": 401,
    "compliance_denied": 403,
    "model_not_found": 404,
    "context_length_exceeded": 413,
    "rate_limited": 429,
    "budget_exhausted": 429,
    "internal_error": 500,
    "upstream_error": 502,
    "service_unavailable": 503,
    "upstream_timeout": 504,
}


@dataclass
class Control:
    """桩的可编程状态。测试直接改这些字段切换场景。"""

    # 成本口径：exact（给 cost_usd）| estimated | unknown（null）| absent（整体缺失＝Proxy 期）
    cost_mode: str = "exact"
    cost_usd: str = "0.000123"
    provider: str = "deepseek"
    model_used: str = "deepseek/deepseek-chat"
    fallback_used: bool = False

    # 计量：provider 实测 | 缺失（触发平台 tiktoken 回退）
    prompt_tokens: int = 120
    completion_tokens: int = 80
    cached_tokens: int = 64
    reasoning_tokens: int = 0
    omit_usage: bool = False

    # 错误注入：{"code": ..., "http": ..., "retry_after": int|None}
    error: dict[str, Any] | None = None
    # 一次性错误序列：前 N 次请求按序失败，之后转正常（验 §7 internal_error 重试 1 次）。
    # 元素形如 {"code": "internal_error"}；列表耗尽后自动恢复正常响应。
    error_sequence: list[dict[str, Any]] = field(default_factory=list)
    # 上游超时：流式/非流式都先 sleep 这么久再响应（测超时分层）
    delay_seconds: float = 0.0
    # 连通性：模拟模型服务整体不可达（直接在传输层断）
    drop_connection: bool = False

    # 目录 / 能力
    models: list[str] = field(default_factory=lambda: ["deepseek-chat", "gpt-4o-mini"])
    capabilities: dict[str, Any] = field(
        default_factory=lambda: {
            "protocol_version": 1,
            "features": {
                "stream_terminate_event": True,
                "cost_attribution": True,
                "routing_hints": True,
                "compliance_policies": ["cn_only_providers"],
                "idempotency_window_seconds": 600,
            },
            "implementation": {"name": "fake-model-service", "version": "0.1.0"},
        }
    )
    # 合规硬策略：请求 routing.allow_providers 含这些 provider 即 403（Q8-B 红线5）
    compliance_denied_providers: list[str] = field(default_factory=lambda: ["openai", "anthropic"])
    # native 模式是否发 event: model_service 终止事件（§6.3）
    emit_terminate_event: bool = True
    # 流式是否插 keepalive 注释行（§6.4）
    emit_keepalive: bool = False
    # 流式收尾 chunk 的 finish_reason（真实流式语义）
    finish_reason: str = "stop"
    # 流式 chunk 之间的间隔。默认 0.01 太快 —— 客户端断开时服务端早已写完全部
    # chunk，"取消传播"（红线6）无法被验证。测 T6 时调大到 0.2s 左右，让服务端
    # 在写第 2 个 chunk 时仍持有连接，从而真实感知断连。
    stream_chunk_delay_seconds: float = 0.01
    # 发满 N 个内容 chunk 后**永久沉默**（不发 usage/终止事件/[DONE]），
    # 用于验证平台侧流式空闲上限（协议 §8.1 缺口 K）。
    silence_after_chunks: int | None = None
    # 沉默期间是否持续发 `: keepalive` 注释行（心跳应能重置空闲计时）。
    keepalive_during_silence: bool = False
    # 沉默持续时长（秒）；配合 silence_after_chunks 使用。
    silence_seconds: float = 5.0

    # 观测记录
    calls: list[dict[str, Any]] = field(default_factory=list)
    idempotency_seen: dict[str, int] = field(default_factory=dict)
    upstream_started: int = 0
    upstream_cancelled: int = 0
    upstream_completed: int = 0
    # 桩是否走完了完整 SSE 序列（含 usage/终止事件/[DONE]）—— 与"客户端收到多少"
    # 相互独立，用于区分"服务端早退"与"客户端提前断开"。
    stream_finished: int = 0
    cancelled_within: list[float] = field(default_factory=list)

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]


# ---------------------------------------------------------------- 辅助


def _error_response(control: Control) -> Response | None:
    """统一的错误注入出口；无注入返回 None。"""
    if not control.error:
        return None
    code = control.error.get("code", "upstream_error")
    status = control.error.get("http") or _CODE_HTTP.get(code, 502)
    headers = {}
    if control.error.get("retry_after") is not None:
        headers["Retry-After"] = str(control.error["retry_after"])
    body = {
        "error": {
            "message": control.error.get("message", f"注入错误 {code}"),
            "type": control.error.get("type", "api_error"),
            "code": code,
            "param": None,
            # §7.1：模型服务声明"重试是否安全"，缺省 false
            "retryable": bool(control.error.get("retryable", False)),
        }
    }
    return JSONResponse(body, status_code=status, headers=headers)


def _check_auth_and_version(request: Request, control: Control) -> Response | None:
    """服务间鉴权（协议 §3）：桩只校验 header 存在与否，token 值由测试断言。"""
    if control.error and control.error.get("code") == "service_auth_failed":
        return _error_response(control)
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or not auth[len("Bearer ") :].strip():
        return JSONResponse(
            {"error": {"message": "缺少服务间 token", "type": "auth_error", "code": "service_auth_failed"}},
            status_code=401,
        )
    return None


def _compliance_denied(body: dict, control: Control) -> Response | None:
    routing = body.get("routing") or {}
    allow = routing.get("allow_providers") or []
    hit = [p for p in allow if p in control.compliance_denied_providers]
    if hit:
        return JSONResponse(
            {
                "error": {
                    "message": f"合规策略否决：provider {hit} 被禁（调用方不可放宽）",
                    "type": "compliance_error",
                    "code": "compliance_denied",
                    "param": None,
                }
            },
            status_code=403,
        )
    return None


def _xms(control: Control) -> dict[str, Any]:
    """按 cost_mode 产出 x_model_service 扩展块（红线2：unknown → null，绝不 0）。"""
    block: dict[str, Any] = {
        "provider": control.provider,
        "model_used": control.model_used,
        "fallback_used": control.fallback_used,
        "upstream_latency_ms": 12,
        "cached": False,
    }
    if control.cost_mode == "exact":
        block["cost_usd"] = control.cost_usd
        block["cost_status"] = "exact"
    elif control.cost_mode == "estimated":
        block["cost_usd"] = control.cost_usd
        block["cost_status"] = "estimated"
    elif control.cost_mode == "unknown":
        block["cost_usd"] = None
        block["cost_status"] = "unknown"
    # absent：整块都不给（Proxy 过渡期形状，协议 §8.3）
    return block


def _usage(control: Control) -> dict[str, Any]:
    if control.omit_usage:
        return {}
    usage: dict[str, Any] = {
        "prompt_tokens": control.prompt_tokens,
        "completion_tokens": control.completion_tokens,
        "total_tokens": control.prompt_tokens + control.completion_tokens,
        "prompt_tokens_details": {"cached_tokens": control.cached_tokens},
        "completion_tokens_details": {"reasoning_tokens": control.reasoning_tokens},
    }
    return usage


def _sse(obj: dict[str, Any] | str, event: str | None = None) -> str:
    prefix = f"event: {event}\n" if event else ""
    payload = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return f"{prefix}data: {payload}\n\n"


# ---------------------------------------------------------------- 端点


async def healthz(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def liveness(request: Request) -> Response:
    return JSONResponse({"status": "alive"})


async def readiness(request: Request) -> Response:
    return JSONResponse({"status": "ready"})


async def capabilities(request: Request) -> Response:
    control: Control = request.app.state.control
    return JSONResponse(control.capabilities)


async def list_models(request: Request) -> Response:
    control: Control = request.app.state.control
    return JSONResponse(
        {"object": "list", "data": [{"id": m, "object": "model"} for m in control.models]}
    )


async def chat_completions(request: Request) -> Response:
    control: Control = request.app.state.control
    body = await request.json()

    control.calls.append(
        {
            "body": body,
            "headers": dict(request.headers),
            "stream": bool(body.get("stream")),
            "at": time.time(),
        }
    )

    auth_err = _check_auth_and_version(request, control)
    if auth_err is not None:
        return auth_err

    # 红线 10：persona: 前缀显式 400
    model = body.get("model", "")
    if isinstance(model, str) and model.startswith("persona:"):
        return JSONResponse(
            {
                "error": {
                    "message": "模型服务不认 persona:<slug>（红线10：平台业务语义不得下移）",
                    "type": "invalid_request_error",
                    "code": "invalid_request",
                    "param": "model",
                }
            },
            status_code=400,
        )

    denied = _compliance_denied(body, control)
    if denied is not None:
        return denied

    # 幂等：同 key 重放只允许一次上游（§8.2 / T7）
    idem = request.headers.get("Idempotency-Key")
    if idem:
        control.idempotency_seen[idem] = control.idempotency_seen.get(idem, 0) + 1
        if control.idempotency_seen[idem] > 1:
            return JSONResponse(
                {
                    "error": {
                        "message": "重复的 Idempotency-Key：已在窗口内处理",
                        "type": "invalid_request_error",
                        "code": "invalid_request",
                        "param": None,
                    }
                },
                status_code=409,
            )

    if control.delay_seconds:
        await asyncio.sleep(control.delay_seconds)

    # 一次性错误序列优先（验重试：前 N 次失败，之后成功）
    if control.error_sequence:
        control.error = control.error_sequence.pop(0)
        err = _error_response(control)
        control.error = None
        if err is not None:
            return err

    err = _error_response(control)
    if err is not None:
        return err

    if not body.get("stream"):
        resp: dict[str, Any] = {
            "id": "chatcmpl-fake",
            "object": "chat.completion",
            "model": control.model_used,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "这是 fake 模型服务的回复。"},
                    "finish_reason": "stop",
                }
            ],
        }
        usage = _usage(control)
        if usage:
            resp["usage"] = usage
        if control.cost_mode != "absent":
            resp["x_model_service"] = _xms(control)
        return JSONResponse(resp)

    return StreamingResponse(
        _stream_body(control),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


async def _stream_body(control: Control):
    """SSE 流：chunk → usage chunk → （native）终止事件 → [DONE]。

    上游任务注册：客户端断开时 Starlette 会取消本生成器，`finally` 记录取消耗时，
    供 T6 断言「平台断开后上游在 ≤2s 内被中止」（红线6）。
    """
    started = time.time()
    control.upstream_started += 1
    sent = 0
    try:
        if control.emit_keepalive:
            yield ": keepalive\n\n"  # 注释行，平台必须忽略（§6.4）

        for piece in ("这是", " fake ", "模型服务的", "流式回复。"):
            yield _sse(
                {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion.chunk",
                    "model": control.model_used,
                    "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                }
            )
            sent += 1
            await asyncio.sleep(control.stream_chunk_delay_seconds)
            # 测试用：发满 N 个 chunk 后停止一切输出（不发 usage/终止/[DONE]），
            # 用于验证平台侧流式空闲上限会断开连接（协议 §8.1 / 缺口 K）。
            if control.silence_after_chunks is not None and sent >= control.silence_after_chunks:
                deadline = time.time() + control.silence_seconds
                while time.time() < deadline:
                    if control.keepalive_during_silence:
                        yield ": keepalive\n\n"  # 心跳：应重置平台侧空闲计时
                    await asyncio.sleep(0.1)
                return

        # 收尾 chunk 携带 finish_reason（真实 OpenAI 流式语义：最后一个 choice 才带）
        yield _sse(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "model": control.model_used,
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": control.finish_reason}
                ],
            }
        )

        usage = _usage(control)
        if usage:
            yield _sse(
                {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion.chunk",
                    "model": control.model_used,
                    "choices": [],
                    "usage": usage,
                }
            )
        if control.cost_mode != "absent" and control.emit_terminate_event:
            yield _sse({"x_model_service": _xms(control)}, event="model_service")

        yield _sse("[DONE]")
        control.stream_finished += 1
        control.upstream_completed += 1
    except asyncio.CancelledError:
        control.upstream_cancelled += 1
        control.cancelled_within.append(time.time() - started)
        raise


def build_app(control: Control) -> Starlette:
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
    app.state.control = control
    return app
