"""上游 provider：把「真实模型调用」抽象成可替换接口。

- `FakeUpstream`：离线确定性，测试/本地默认。带可编程场景（错误/慢/沉默/心跳）与取消观测，
  行为严格对齐协议 §5/§6/§8，供 model-service/tests 跑 T1–T11。
- `LiteLLMProvider`：生产可选（MS_UPSTREAM=litellm），lazy import，失败映射为 §7 错误。

成本口径（红线2）：cost_usd 由费率表推算，不可得 → **null + unknown，绝不 0**。
"""

import asyncio
import time
from contextlib import suppress
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, AsyncIterator

from msvc.errors import from_upstream_exception

# 极简费率表（USD/token），仅供 fake provider 产出「exact」成本演示。
_RATE_TABLE: dict[str, tuple[float, float]] = {
    "deepseek-chat": (2.5e-7, 1e-6),
    "qwen-plus": (3.5e-7, 7e-7),
}


@dataclass
class UpstreamResult:
    content: str
    usage: dict[str, Any]
    x_model_service: dict[str, Any]
    # 非流式收尾原因（§6.6）：真实上游给 `stop`/`length`；缺失才回落到默认值。
    finish_reason: str = "stop"


@dataclass
class Scenario:
    """单次请求的注入场景（测试驱动）。"""

    error: dict[str, Any] | None = None
    # 流式：把 error 推迟到「已产出 N 个内容 chunk 之后」再抛（首字节**之后**的错误）。
    # None ⇒ error 在首字节前抛出（HTTP 错误状态路径）。见 §6.7。
    error_after_chunks: int | None = None
    delay_seconds: float = 0.0  # 上游响应前延迟（测超时）
    first_token_seconds: float = 0.0  # 流式首字节延迟（测首 token 超时）
    chunk_delay: float = 0.01  # 流式相邻 chunk 间隔；测 T6 取消时调大以续住连接
    # 流式：产出 N 个 chunk 后**停顿** M 秒（中途空闲，测 O1 的 chunk 间隔预算）。
    stall_after_chunks: int | None = None
    stall_seconds: float = 0.0
    silence_after_chunks: int | None = None  # 发满 N 块后沉默
    keepalive_during_silence: bool = False
    silence_seconds: float = 5.0
    # RB-2③（协议 §28.2）：沉默期间每隔多久发一行心跳（`keepalive_during_silence` 打开时生效）。
    keepalive_interval_seconds: float = 1.0
    # RB-1/RB-2：模拟 reasoning 型上游——先只"推理"（产 N 个不产出正文的上游 chunk），
    # 之后才出正文。fake 与 `LiteLLMProvider` 在此**同形**：都发 `{"kind":"keepalive"}` 事件，
    # 这样服务侧用例不必依赖真 litellm 也能锁住对外字节流形态（真实 provider 另有对拍用例）。
    reasoning_only_chunks: int = 0
    reasoning_chunk_interval: float = 0.05
    cost_mode: str = "exact"  # exact | unknown | absent
    provider: str = "deepseek"
    model_used: str = "deepseek/deepseek-chat"
    # 流式收尾的 `finish_reason`（§6.6：`stop` / `length` / 上游实际值）。P1-8。
    finish_reason: str = "stop"


class FakeUpstream:
    name = "fake"

    def __init__(self) -> None:
        # 观测（供测试断言取消传播 / 上游调用次数）
        self.started = 0
        self.cancelled = 0
        self.completed = 0
        self._cancel_latency: list[float] = []

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]

    def _usage(self) -> dict[str, Any]:
        return {
            "prompt_tokens": 120,
            "completion_tokens": 80,
            "total_tokens": 200,
            "prompt_tokens_details": {"cached_tokens": 64},
            "completion_tokens_details": {"reasoning_tokens": 0},
        }

    def _xms(self, scenario: Scenario) -> dict[str, Any]:
        # absent（Proxy 过渡期形状，§8.3）：整块都不给
        if scenario.cost_mode == "absent":
            return {}
        block: dict[str, Any] = {
            "provider": scenario.provider,
            "model_used": scenario.model_used,
            "model_requested": scenario.model_used.split("/")[-1],
            "fallback_used": False,
            "upstream_latency_ms": 12,
            "cached": False,
        }
        if scenario.cost_mode == "exact":
            base = scenario.model_used.split("/")[-1]
            pin, pout = _RATE_TABLE.get(base, (0.0, 0.0))
            cost = pin * 120 + pout * 80
            block["cost_usd"] = str(round(cost, 10))
            block["cost_status"] = "exact"
        else:  # unknown → null，绝不 0（红线2）
            block["cost_usd"] = None
            block["cost_status"] = "unknown"
        return block

    async def complete(self, body: dict, scenario: Scenario) -> UpstreamResult:
        self.started += 1
        if scenario.error:
            await asyncio.sleep(scenario.delay_seconds)
            raise _scenario_error(scenario.error)
        if scenario.delay_seconds:
            await asyncio.sleep(scenario.delay_seconds)
        await asyncio.sleep(0)  # 让出，模拟真实异步上游
        self.completed += 1
        return UpstreamResult(
            content="这是模型服务的回复。",
            usage=self._usage(),
            x_model_service=self._xms(scenario),
        )

    async def stream(self, body: dict, scenario: Scenario) -> AsyncIterator[dict[str, Any]]:
        """yield 事件字典：{"kind":"chunk"|"finish"|"usage"|"terminate"|"done", ...}。

        `finish` 是**收尾事件**（§6.6 / 工作单 P1-8）：上游实际终止原因，服务侧据此发
        带非 null `finish_reason` 的最后一个 chunk。真实 provider 从
        `chunk.choices[0].finish_reason` 取；fake 用 `scenario.finish_reason`。
        """
        self.started += 1
        t0 = time.monotonic()
        try:
            if scenario.error and scenario.error_after_chunks is None:
                if scenario.delay_seconds:
                    await asyncio.sleep(scenario.delay_seconds)
                raise _scenario_error(scenario.error)
            if scenario.first_token_seconds:
                await asyncio.sleep(scenario.first_token_seconds)
            # RB-1/RB-2：reasoning 阶段——上游只吐**不产出正文**的 chunk。fake 与真实
            # `LiteLLMProvider` 在此同形（都发 `{"kind":"keepalive"}` 事件），故服务侧用例
            # 不依赖网络也能锁住"推理期有注释行、正文前不 504"这条防线。
            for _ in range(scenario.reasoning_only_chunks):
                await asyncio.sleep(scenario.reasoning_chunk_interval)
                yield {"kind": "keepalive"}
            sent = 0
            for piece in ("这是", " 模型服务的", "流式回复。"):
                yield {"kind": "chunk", "content": piece}
                sent += 1
                # 中途停顿（O1）：真实上游偶发的 >1s 停顿**不得**被误杀
                if scenario.stall_after_chunks is not None and sent >= scenario.stall_after_chunks:
                    await asyncio.sleep(scenario.stall_seconds)
                await asyncio.sleep(scenario.chunk_delay)
                # 首字节**之后**的错误（§6.7）：已产出 N 块后再抛 → 服务侧应发流内错误帧
                if scenario.error_after_chunks is not None and sent >= scenario.error_after_chunks:
                    raise _scenario_error(
                        scenario.error or {"code": "internal_error", "retryable": False}
                    )
                # RB-2③：把此前"声明但零使用"的死字段接上。`silence_after_chunks` 单独就表示
                # "发满 N 块后进入沉默"（字段本意），`keepalive_during_silence` 决定这段沉默里
                # 要不要按 keepalive_interval_seconds 心跳——真实服务里心跳由 reasoning chunk
                # 触发，这里模拟"上游还活着但不出字"的另一种静默形态（也是幂等窗在飞位用例的
                # 确定性抓手：不再靠 0.6s 的时间差去赌并发）。
                if scenario.silence_after_chunks is not None and sent >= scenario.silence_after_chunks:
                    waited = 0.0
                    while waited < scenario.silence_seconds:
                        step = min(scenario.keepalive_interval_seconds, scenario.silence_seconds - waited)
                        await asyncio.sleep(step)
                        waited += step
                        if scenario.keepalive_during_silence:
                            yield {"kind": "keepalive"}
                    break
            yield {"kind": "finish", "finish_reason": scenario.finish_reason}
            yield {"kind": "usage", "usage": self._usage()}
            if scenario.cost_mode != "absent":
                yield {"kind": "terminate", "x_model_service": self._xms(scenario)}
            yield {"kind": "done"}
            self.completed += 1
        except asyncio.CancelledError:
            self.cancelled += 1
            self._cancel_latency.append(time.monotonic() - t0)
            raise


class _ScenarioError(Exception):
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        super().__init__(str(payload))


def _scenario_error(err: dict[str, Any]) -> _ScenarioError:
    return _ScenarioError(err)


class LiteLLMProvider:
    """真实上游（lazy import litellm）。

    SW-1：`stream()` 必须实现——native 模式下流式是**主路径**，此前抛
    `NotImplementedError` 会让 `LLM_BACKEND=remote` 切换后所有流式请求裸 500。
    SW-4：所有上游异常统一经 `from_upstream_exception` 映射为带 §7 `code` 的
    `ServiceError`，绝不把 litellm 异常族裸抛给 handler。

    读事件同时支持「对象属性」与「dict」两种形态（真实 litellm 给对象，测试注入给 dict），
    因此测试可以针对**本类本体**跑，而不必退回 `FakeUpstream`。
    """

    name = "litellm"

    def _kwargs(self, body: dict, scenario: Scenario) -> dict[str, Any]:
        from msvc.config import get_settings

        s = get_settings()
        model = body["model"]
        kw: dict[str, Any] = {"model": model, "messages": body["messages"]}
        for key in ("temperature", "top_p", "max_tokens"):
            if body.get(key) is not None:
                kw[key] = body[key]
        if s.litellm_api_base:
            kw["api_base"] = s.litellm_api_base
        if s.litellm_api_key:
            kw["api_key"] = s.litellm_api_key
        # A-1 第1项（协议 §26.6-丙）：**无前缀** model 必须由部署侧显式声明走哪种协议，
        # 否则 litellm 定不出 provider（实测 `mimo-v2.6-flash` 裸 model 直接报错）。
        # 有前缀时**不传** `custom_llm_provider`：litellm 在两者不一致时会把 provider 再拼
        # 一层（`deepseek/deepseek-chat` + `openai` ⇒ `openai/deepseek/deepseek-chat`），
        # 反而把 model id 弄错。前缀本身已经是 litellm 的 provider 语法。
        if "/" not in model:
            kw["custom_llm_provider"] = s.litellm_provider
        return kw

    def _xms(
        self, *, requested_model: str, used_model: str | None, usage: dict[str, Any] | None, latency_ms: int
    ) -> dict[str, Any]:
        from msvc.config import get_settings

        s = get_settings()
        used = used_model or requested_model
        # A-1 第5项（架构师 B 2.3 🔴 **静默错账**）：`provider` 是**供应商名**，必须取部署
        # 声明的 `MS_UPSTREAM_VENDOR`。此前无前缀 model 会回落到 `litellm_default_model` 的
        # 前缀 ⇒ 打小蜜端点却记 `provider=="deepseek"`，而该字段经 `remote.py` →
        # `ledger.py` 进 `usage_logs.provider` 与供应商统计，错了没人发现。
        # vendor 未声明才保留旧回落（升级期不炸既有部署）。
        inferred = used.split("/")[0] if "/" in used else s.litellm_default_model.split("/")[0]
        block: dict[str, Any] = {
            "provider": (s.upstream_vendor.strip() or inferred),
            "model_used": used,
            "model_requested": requested_model,
            "fallback_used": False,
            "upstream_latency_ms": latency_ms,
            "cached": bool((usage or {}).get("prompt_tokens_details", {}).get("cached_tokens")),
        }
        # 成本口径（红线2）：能实测才算 exact；算不出 → null + unknown，**绝不返回 0**。
        try:
            import litellm

            pin, pout = litellm.cost_per_token(
                model=used,
                prompt_tokens=int((usage or {}).get("prompt_tokens", 0) or 0),
                completion_tokens=int((usage or {}).get("completion_tokens", 0) or 0),
            )
            block["cost_usd"] = str(Decimal(str(pin)) + Decimal(str(pout)))
            block["cost_status"] = "exact"
        except Exception:  # 无价格表/上游未给 usage → unknown
            block["cost_usd"] = None
            block["cost_status"] = "unknown"
        return block

    async def complete(self, body: dict, scenario: Scenario) -> UpstreamResult:
        import litellm

        t0 = time.monotonic()
        try:
            resp = await litellm.acompletion(**self._kwargs(body, scenario))
        except Exception as exc:
            raise from_upstream_exception(exc) from exc
        usage = _norm_usage(_get(resp, "usage"))
        choice = _first(_get(resp, "choices")) or {}
        message = _as_dict(choice.get("message") if isinstance(choice, dict) else None)
        finish = choice.get("finish_reason") or "stop"
        xms = self._xms(
            requested_model=body["model"],
            used_model=_get(resp, "model"),
            usage=usage,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        return UpstreamResult(
            content=message.get("content") or "",
            usage=usage or {},
            x_model_service=xms,
            finish_reason=finish,
        )

    async def stream(self, body: dict, scenario: Scenario) -> AsyncIterator[dict[str, Any]]:
        """真实上游流式：逐块转发内容 → 收尾 `finish_reason` → usage → 终止事件 → done。

        事件顺序与 `FakeUpstream` 严格一致（§6.6/P1-8：finish chunk 必须**先于** usage chunk
        且携带非 null finish_reason），否则平台侧 `StreamDone.finish_reason` 在生产路径失效。
        """
        import litellm

        kw = self._kwargs(body, scenario)
        kw["stream"] = True
        kw["stream_options"] = {"include_usage": True}
        t0 = time.monotonic()
        try:
            resp = await litellm.acompletion(**kw)
        except Exception as exc:  # 首字节**前**失败 → 交由 handler 返回 HTTP 错误状态（§6.7）
            raise from_upstream_exception(exc) from exc

        used_model: str | None = None
        finish_reason: str | None = None
        usage: dict[str, Any] | None = None
        try:
            async for chunk in resp:
                used_model = _get(chunk, "model") or used_model
                choice = _first(_get(chunk, "choices"))
                if choice:
                    delta = choice.get("delta") if isinstance(choice, dict) else _obj(choice, "delta")
                    delta = delta if isinstance(delta, dict) else _as_dict(delta)
                    text = delta.get("content")
                    fin = choice.get("finish_reason")
                    if fin:
                        finish_reason = fin
                    if text:
                        yield {"kind": "chunk", "content": text}
                    elif not fin:
                        # RB-1／RB-2（协议 §28.1／§6-4「推理期间必须发」）：**不产出正文**的上游
                        # chunk（reasoning-only 最常见，也含 role-only／空 delta）必须产出一个事件，
                        # 否则服务侧整个推理期对外静默 ⇒ §8.1 的"上游首 token"被实现成"首个对外
                        # 事件"，推理超过预算就误报 504（这正是 B 裁的"定义没错、实现错了"）。
                        # 该事件同时兼作首 token 计时的复位信号与对外的 `: keepalive` 注释行。
                        # 收尾 chunk（带 finish_reason）**不**算心跳——它自己会变成 finish 事件，
                        # 给它再发一条会让 §6.6 的事件顺序出现多余行、也和平台已有用例的形状不符。
                        yield {"kind": "keepalive"}
                raw_usage = _get(chunk, "usage")
                if raw_usage:
                    usage = _norm_usage(raw_usage) or usage
            yield {"kind": "finish", "finish_reason": finish_reason or "stop"}
            if usage:
                yield {"kind": "usage", "usage": usage}
            yield {
                "kind": "terminate",
                "x_model_service": self._xms(
                    requested_model=body["model"],
                    used_model=used_model,
                    usage=usage,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                ),
            }
            yield {"kind": "done"}
        except asyncio.CancelledError:
            # 红线6：平台断连 → 必须中止上游，不得让它继续烧 token
            aclose = getattr(resp, "aclose", None) or getattr(resp, "close", None)
            if aclose is not None:
                with suppress(Exception):
                    await aclose()
            raise
        except Exception as exc:  # 首字节**后**失败 → handler 发流内错误帧 + [DONE]（§6.7）
            aclose = getattr(resp, "aclose", None) or getattr(resp, "close", None)
            if aclose is not None:
                with suppress(Exception):
                    await aclose()
            raise from_upstream_exception(exc) from exc


def _obj(obj: Any, attr: str) -> Any:
    return getattr(obj, attr, None)


def _as_dict(obj: Any) -> dict:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    to_dict = getattr(obj, "model_dump", None) or getattr(obj, "dict", None)
    if callable(to_dict):
        with suppress(Exception):
            return to_dict()
    return {k: v for k, v in vars(obj).items() if not k.startswith("_")} if hasattr(obj, "__dict__") else {}


def _get(obj: Any, attr: str) -> Any:
    """同时支持 dict 与对象两种形态（真实 litellm 给对象，测试注入可给 dict）。"""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(attr)
    return getattr(obj, attr, None)


def _first(choices: Any) -> dict | None:
    if not choices:
        return None
    return _as_dict(choices[0])


def _norm_usage(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    d = _as_dict(raw) if not isinstance(raw, dict) else raw
    out: dict[str, Any] = {
        "prompt_tokens": int(d.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(d.get("completion_tokens", 0) or 0),
        "total_tokens": int(d.get("total_tokens", 0) or 0)
        or int(d.get("prompt_tokens", 0) or 0) + int(d.get("completion_tokens", 0) or 0),
    }
    details = d.get("prompt_tokens_details")
    if details:
        dd = details if isinstance(details, dict) else _as_dict(details)
        if dd.get("cached_tokens") is not None:
            out["prompt_tokens_details"] = {"cached_tokens": int(dd["cached_tokens"] or 0)}
    cdetails = d.get("completion_tokens_details")
    if cdetails:
        cd = cdetails if isinstance(cdetails, dict) else _as_dict(cdetails)
        if cd.get("reasoning_tokens") is not None:
            out["completion_tokens_details"] = {"reasoning_tokens": int(cd["reasoning_tokens"] or 0)}
    return out
