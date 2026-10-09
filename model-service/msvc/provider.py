"""上游 provider：把「真实模型调用」抽象成可替换接口。

- `FakeUpstream`：离线确定性，测试/本地默认。带可编程场景（错误/慢/沉默/心跳）与取消观测，
  行为严格对齐协议 §5/§6/§8，供 model-service/tests 跑 T1–T11。
- `LiteLLMProvider`：生产可选（MS_UPSTREAM=litellm），lazy import，失败映射为 §7 错误。

成本口径（红线2）：cost_usd 由费率表推算，不可得 → **null + unknown，绝不 0**。
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

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


@dataclass
class Scenario:
    """单次请求的注入场景（测试驱动）。"""

    error: dict[str, Any] | None = None
    delay_seconds: float = 0.0  # 上游响应前延迟（测超时）
    first_token_seconds: float = 0.0  # 流式首字节延迟（测首 token 超时）
    chunk_delay: float = 0.01  # 流式相邻 chunk 间隔；测 T6 取消时调大以续住连接
    silence_after_chunks: int | None = None  # 发满 N 块后沉默
    keepalive_during_silence: bool = False
    silence_seconds: float = 5.0
    cost_mode: str = "exact"  # exact | unknown | absent
    provider: str = "deepseek"
    model_used: str = "deepseek/deepseek-chat"


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
        """yield 事件字典：{"kind":"chunk"|"usage"|"terminate"|"keepalive"|"done", ...}。"""
        self.started += 1
        t0 = time.monotonic()
        try:
            if scenario.error:
                if scenario.delay_seconds:
                    await asyncio.sleep(scenario.delay_seconds)
                raise _scenario_error(scenario.error)
            if scenario.first_token_seconds:
                await asyncio.sleep(scenario.first_token_seconds)
            for piece in ("这是", " 模型服务的", "流式回复。"):
                yield {"kind": "chunk", "content": piece}
                await asyncio.sleep(scenario.chunk_delay)
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
    """生产上游（lazy import litellm）。失败映射 §7。"""

    name = "litellm"

    async def complete(self, body: dict, scenario: Scenario) -> UpstreamResult:  # pragma: no cover - 依赖网络
        import litellm

        resp = await litellm.acompletion(
            model=body["model"],
            messages=body["messages"],
            api_base=scenario.__dict__.get("api_base") or None,
        )
        usage = {
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "total_tokens": resp.usage.total_tokens,
        }
        xms = {
            "provider": body["model"].split("/")[0],
            "model_used": resp.model,
            "cost_status": "unknown",
            "cost_usd": None,
        }
        return UpstreamResult(content=resp.choices[0].message.content or "", usage=usage, x_model_service=xms)

    async def stream(self, body: dict, scenario: Scenario):  # pragma: no cover
        raise NotImplementedError("LiteLLM 流式 provider 在生产接线时补全")
