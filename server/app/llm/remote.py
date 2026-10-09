"""RemoteBackend：平台 → 模型服务的 HTTP 客户端（S4）。

契约 = `docs/model-service-protocol.md` v1；任何与协议不符的实现都是 bug。本模块只做
「平台侧消费」：把 §5/§6 的响应字段映射进 gateway 的 LLMResult/StreamDone，把 §7 的
错误 code 映射成平台异常，红线 6 的取消传播靠 httpx 流上下文在生成器关闭时断连实现。

设计要点：
- 无凭证下沉：协议 §9 —— api_base/api_key 迁到模型服务，本后端**忽略**这两个入参，
  只用 MODEL_SERVICE_BASE_URL + 服务间 Bearer（§3）。
- cost_usd 只影响 upstream 一侧（§5.1）；扣积分仍走 rating_rules，与本模块无关。
- 429/400/403 等业务错误**不计入**熔断（§11），只有 5xx/连接错才计。
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal, InvalidOperation

import httpx

from app.config import get_settings
from app.llm.gateway import LLMResult, StreamDone, _estimate_tokens, _tiktoken_count

logger = logging.getLogger("app.llm.remote")

# §7 错误 code → (平台对外 HTTP 状态, 是否运维告警, 是否计入熔断)。
# 平台对外状态是「翻译后」的语义（§7 Q5-B），不等于模型服务返回的原始码。
_CODE_TO_PLATFORM: dict[str, tuple[int, bool, bool]] = {
    "invalid_request": (400, False, False),
    "service_auth_failed": (502, True, False),
    "upstream_auth_failed": (502, True, False),
    "compliance_denied": (403, False, False),
    "model_not_found": (404, False, False),
    "context_length_exceeded": (413, False, False),
    "rate_limited": (429, False, False),
    "budget_exhausted": (429, False, False),
    "internal_error": (502, False, True),
    "upstream_error": (503, False, True),
    "service_unavailable": (503, False, True),
    "upstream_timeout": (504, False, True),
}


class ModelServiceError(Exception):
    """模型服务调用失败（连接/协议/上游）。路由层据 code+status 渲染对外响应。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 502,
        retry_after: int | None = None,
        alert: bool = False,
        err_type: str = "api_error",
        retryable: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry_after = retry_after
        self.alert = alert
        self.err_type = err_type
        # §7.1：模型服务声明该错误「重试是否安全」。缺省 False（保守）。
        # 注意：协议 §7.1 硬规则规定平台**收到任何响应后一律不重试**，故该字段
        # 当前只作诊断/可观测用途，不驱动重试；保留以符合契约形状。
        self.retryable = retryable


# 协议 §1-1 / §4.2：这些字段一律不得出现在发往模型服务的请求里。
# 平台业务身份（user_id/conversation_id/persona）下移即越界；trace 只允许**部署级
# 不透明标签**，不得由 user_id/conversation 派生 —— 派生即换名传身份（§4.2）。
_FORBIDDEN_BODY_FIELDS = (
    "user_id",
    "user",
    "conversation_id",
    "conversation",
    "persona",
    "persona_id",
    "trace",  # 平台当前不发送 trace；将来发送须是部署级常量，见协议 §4.2
)


def _guard_forbidden_fields(
    model: str,
    messages: list[dict],
    body: dict,
) -> None:
    """红线 1 的平台侧守卫：发现禁字段**立即失败**，不把身份悄悄发给模型服务。

    审计（协议 §13 C）指出该条款此前「声明了却零实现」。平台现有调用方并不传这些
    字段，所以这是一道**契约防御**而非线上故障修复 —— 它的价值在于：将来有人图方便
    `body["user_id"] = str(user.id)` 时，不会静默越界。
    """
    if isinstance(model, str) and model.startswith("persona:"):
        raise ModelServiceError(
            "invalid_request",
            "不得把 persona:<slug> 下移给模型服务（红线1：人格是平台业务语义）",
            status=400,
            err_type="invalid_request_error",
        )
    for key in _FORBIDDEN_BODY_FIELDS:
        if key in body:
            raise ModelServiceError(
                "invalid_request",
                f"请求体含禁字段 {key!r}：平台业务身份不得下移（协议 §4.2 红线1）",
                status=400,
                err_type="invalid_request_error",
            )
    for idx, msg in enumerate(messages):
        if isinstance(msg, dict):
            leaked = [k for k in _FORBIDDEN_BODY_FIELDS if k in msg]
            if leaked:
                raise ModelServiceError(
                    "invalid_request",
                    f"messages[{idx}] 含禁字段 {leaked}：不得借消息体传平台身份（红线1）",
                    status=400,
                    err_type="invalid_request_error",
                )


class CircuitBreaker:
    """§11 平台→模型服务熔断：滑动窗口内**连续** N 次 5xx/连接错/超时 → 打开。

    业务性错误（429/400/403/404/413）不计入，避免上游限流误杀整条链路（§11）。
    进程级单例。

    半开语义（协议 §11 修订，2026-10-09）：冷却期结束后进入 `half_open`，
    **每次只允许 1 个探测请求在飞**；探测成功 → `closed`；探测失败 → 立即回
    `open` 并重置冷却计时。原实现「冷却一到即放行全量」会使熔断退化成固定
    30s 屏蔽窗，失去保护意义。
    """

    def __init__(self, threshold: int = 5, reset_seconds: float = 30.0):
        self.threshold = threshold
        self.reset_seconds = reset_seconds
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if time.monotonic() - self._opened_at >= self.reset_seconds:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        """放行判定。half_open 下**只放行一个**探测：占用探测位。"""
        state = self.state
        if state == "closed":
            return True
        if state == "half_open" and not self._probe_in_flight:
            self._probe_in_flight = True
            return True
        return False

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        self._probe_in_flight = False
        if self.state == "half_open":
            # 探测失败 → 立即回 open 并重置冷却计时（§11）
            self._opened_at = time.monotonic()
            logger.error("模型服务熔断：半开探测失败，重新打开")
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.threshold and self._opened_at is None:
            self._opened_at = time.monotonic()
            logger.error("模型服务熔断打开：连续 %d 次失败", self._consecutive_failures)

    def reset(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None
        self._probe_in_flight = False


# 进程级单例（一个模型服务）。阈值/冷却从配置读取——修掉此前写死 5/30、
# `model_service_cb_threshold`/`model_service_cb_reset_seconds` 形同虚设的缺陷（复核③）。
# 测试可 reset()（reset 只清状态，不改阈值/冷却，与既有测试用 breaker.threshold 一致）。
_bs = get_settings()
breaker = CircuitBreaker(
    threshold=_bs.model_service_cb_threshold,
    reset_seconds=_bs.model_service_cb_reset_seconds,
)


def _parse_cost(raw: object) -> Decimal | None:
    """x_model_service.cost_usd：十进制字符串或 null → Decimal|None。绝不当 0（红线2）。"""
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        logger.warning("cost_usd 非法，按 unknown 处理：%r", raw)
        return None


def _extract_xms(data: dict) -> dict:
    xms = data.get("x_model_service")
    return xms if isinstance(xms, dict) else {}


def _usage_from(usage: dict | None) -> tuple[int, int, int, int]:
    if not usage:
        return 0, 0, 0, 0
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    cache_read = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)
    reasoning = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0)
    return prompt, completion, cache_read, reasoning


class RemoteBackend:
    """LLM_BACKEND=remote 时选用。签名与 MockBackend/LiteLLMBackend 对齐（协议 §10.1）。"""

    def _settings(self):
        return get_settings()

    def _url(self, path: str = "/v1/chat/completions") -> str:
        base = self._settings().model_service_base_url.rstrip("/")
        if not base:
            raise ModelServiceError(
                "service_unavailable",
                "模型服务未配置（MODEL_SERVICE_BASE_URL 为空）",
                status=503,
            )
        return f"{base}{path}"

    def _headers(self, idempotency_key: str | None) -> dict[str, str]:
        s = self._settings()
        h = {
            "Authorization": f"Bearer {s.model_service_token}",
            "X-Request-Id": str(uuid.uuid4()),
            "X-Model-Service-Version": str(s.model_service_version),
            "Content-Type": "application/json",
        }
        # §8.2：平台生成的 Idempotency-Key 必须透传，保证「平台重试 ≠ 上游双烧」
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        return h

    def _body(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        stream: bool,
        temperature: float | None,
        top_p: float | None,
        max_tokens: int | None,
    ) -> dict:
        body: dict = {"model": model, "messages": messages, "stream": stream}
        if stream:
            body["stream_options"] = {"include_usage": True}  # 平台必带（§4.2）
        if temperature is not None:
            body["temperature"] = temperature
        if top_p is not None:
            body["top_p"] = top_p
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        return body

    @staticmethod
    def _guard(body: dict) -> None:
        """红线1 的**最后一道门**：真正出网前的守卫。

        刻意放在这里而不是 `_body()`：`_body` 由子类/调用方组装，若守卫放在组装流程
        中段，任何在其后追加字段的代码都能绕过它（审计 C 要求"声明即实现"，绕过即失效）。
        放在发请求的公共入口，则**没有任何路径能绕过**。
        """
        _guard_forbidden_fields(body.get("model", ""), body.get("messages") or [], body)

    @staticmethod
    def _is_not_delivered(exc: httpx.HTTPError) -> bool:
        """§7.1：判定「请求未送达」——唯一允许平台侧重试的情形。

        只有建连阶段的失败才算未送达（请求没发出去，无副作用）：
        `ConnectError`（含 DNS/拒绝连接）与 `ConnectTimeout`。
        **读超时不算**——那说明请求已送达且上游可能已开始消耗 token，重试会双烧。
        """
        return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))

    async def _post(
        self, client: httpx.AsyncClient, url: str, body: dict, headers: dict
    ) -> httpx.Response:
        """单次 POST（守卫 + 出网）。重试策略由调用方按 §7.1 决定，不在此处。"""
        self._guard(body)
        return await client.post(url, json=body, headers=headers)

    async def _connect_once(
        self, timeout: httpx.Timeout, url: str, body: dict, headers: dict
    ) -> httpx.Response:
        """发起连接（非流式）。**仅在"请求未送达"时重试 1 次**（§7.1）。

        复用同一套 `headers` → `Idempotency-Key` 一致，保证"平台重试 ≠ 上游双烧"。
        收到任何 HTTP 响应（含 5xx）后**一律不重试**：§8.2 的幂等窗口会使第二次请求
        直接命中首次结果，徒增延迟；`internal_error` 交由熔断 + 降级链承担。
        """
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            try:
                return await self._post(client, url, body, headers)
            except httpx.HTTPError as exc:
                if not self._is_not_delivered(exc):
                    raise  # 已送达/读超时 → 不重试
                logger.warning("模型服务建连失败（请求未送达），按 §7.1 重试 1 次")
        # 新建 client 做唯一一次重试（旧连接已不可用）
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            return await self._post(client, url, body, headers)

    def _error_from_response(self, resp: httpx.Response) -> ModelServiceError:
        """把模型服务的 OpenAI 错误体翻译成 ModelServiceError，并按 §11 记熔断。"""
        code = "upstream_error"
        message = f"模型服务返回 {resp.status_code}"
        err_type = "api_error"
        retryable = False
        try:
            payload = resp.json()
            err = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(err, dict):
                code = err.get("code") or code
                message = err.get("message") or message
                err_type = err.get("type") or err_type
                retryable = bool(err.get("retryable", False))  # §7.1 缺省 false
        except Exception:
            pass

        status, alert, counts = _CODE_TO_PLATFORM.get(code, (502, False, True))
        if code not in _CODE_TO_PLATFORM:
            # 未知码：按原始 HTTP 状态归类，5xx 计入熔断
            status = resp.status_code if resp.status_code >= 400 else 502
            counts = resp.status_code >= 500
        retry_after = None
        if code == "rate_limited":
            ra = resp.headers.get("Retry-After")
            if ra:
                try:
                    retry_after = int(ra)
                except ValueError:
                    retry_after = None
        if counts:
            breaker.record_failure()
        if alert:
            logger.error("模型服务鉴权/内部告警 code=%s status=%s: %s", code, status, message)
        return ModelServiceError(
            code,
            message,
            status=status,
            retry_after=retry_after,
            alert=alert,
            err_type=err_type,
            retryable=retryable,
        )

    async def _unavailable(self, reason: str, *, model: str) -> ModelServiceError:
        """构造 §8.4 生产降级 503。

        **此处不计入熔断**：`_unavailable` 既服务「真实传输失败」（由调用点显式
        `record_failure`）也服务「被熔断主动拒绝」的请求——后者根本没出网，若在此计数
        会污染连续失败数、并在 half_open 下清掉在飞探测的 `_probe_in_flight` 位（复核缺陷②）。
        是否计入完全交由调用点决定。
        """
        logger.warning("模型服务不可用：%s", reason)
        return ModelServiceError(
            "service_unavailable", "模型服务暂不可用，请稍后再试", status=503
        )

    def _result_from(self, data: dict, model: str, messages: list[dict[str, str]]) -> LLMResult:
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        prompt, completion, cache_read, reasoning = _usage_from(usage)
        xms = _extract_xms(data)
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            content = ""
        finish = None
        try:
            finish = data["choices"][0].get("finish_reason")
        except (KeyError, IndexError, TypeError):
            pass

        metering = "provider"
        if prompt == 0 or completion == 0:
            # 服务未给实测 usage（异常）→ tiktoken 补，再退字符估算，标复核
            metering = "tiktoken"
            joined = "".join(m.get("content", "") for m in messages)
            prompt = prompt or _tiktoken_count(joined, model) or _estimate_tokens(joined)
            completion = completion or _tiktoken_count(content, model) or _estimate_tokens(content)
            if prompt == 0 or completion == 0:
                metering = "estimated"

        model_used = data.get("model") or xms.get("model_used") or model
        return LLMResult(
            content=content,
            model=model_used,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_read_tokens=cache_read,
            reasoning_tokens=reasoning,
            finish_reason=finish,
            metering_source=metering,
            needs_review=metering != "provider",
            provider=xms.get("provider"),
            model_used=xms.get("model_used") or model_used,
            cost_usd=_parse_cost(xms.get("cost_usd")),
            cost_status=xms.get("cost_status"),
            fallback_used=bool(xms.get("fallback_used", False)),
        )

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        api_base: str | None = None,  # noqa: ARG002 — 凭证已下沉模型服务（§9），忽略
        api_key: str | None = None,  # noqa: ARG002
        idempotency_key: str | None = None,
    ) -> LLMResult:
        s = self._settings()
        if not breaker.allow():
            raise await self._unavailable("熔断打开", model=model)
        timeout = httpx.Timeout(s.model_service_timeout_total, connect=s.model_service_timeout_connect)
        body = self._body(
            model, messages, stream=False, temperature=temperature, top_p=top_p, max_tokens=max_tokens
        )
        try:
            resp = await self._connect_once(
                timeout, self._url(), body, self._headers(idempotency_key)
            )
        except httpx.HTTPError as exc:
            breaker.record_failure()  # 真实传输失败计入熔断；被拒路径走 allow() 分支不记（缺陷①②）
            raise await self._unavailable(f"连接失败：{exc!r}", model=model) from exc

        if resp.status_code != 200:
            raise self._error_from_response(resp)
        breaker.record_success()
        return self._result_from(resp.json(), model, messages)

    async def chat_stream(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        api_base: str | None = None,  # noqa: ARG002
        api_key: str | None = None,  # noqa: ARG002
        idempotency_key: str | None = None,
    ) -> AsyncIterator[str | StreamDone]:
        s = self._settings()
        if not breaker.allow():
            raise await self._unavailable("熔断打开", model=model)
        # 首字节/逐块读超时 = first_byte（§8.1）；建连超时单列
        timeout = httpx.Timeout(
            s.model_service_timeout_total,
            connect=s.model_service_timeout_connect,
            read=s.model_service_timeout_first_byte,
        )
        body = self._body(
            model, messages, stream=True, temperature=temperature, top_p=top_p, max_tokens=max_tokens
        )
        headers = self._headers(idempotency_key)
        url = self._url()
        self._guard(body)  # 出网前最后一道门（红线1），流式路径同样不可绕过

        parts: list[str] = []
        usage: dict | None = None
        xms: dict = {}
        finish_reason: str | None = None
        current_event: str | None = None
        client = httpx.AsyncClient(timeout=timeout, trust_env=False)
        # §8.1 缺口 K：httpx 的 total 超时**对流式不适用**，必须显式做空闲计时。
        # 语义是"沉默时长"而非"总时长"：只有**协议活动**（注释行/事件行/data 行）重置，
        # 纯空白行不算 —— 否则一个持续发空行的服务能把连接无限吊住。
        idle_limit = s.model_service_stream_idle_timeout
        abs_limit = s.model_service_stream_max_duration
        started = time.monotonic()
        terminate_reason: str | None = None
        try:
            # 取消传播（红线6）：消费方断开 → 本生成器被关闭 → async with 退出
            # → httpx 关闭连接 → 模型服务侧感知断连并中止上游。
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    raise self._error_from_response(resp)
                line_iter = resp.aiter_lines().__aiter__()
                while True:
                    # §8.1 两层独立计时：
                    # - 空闲上限 idle_limit：任一协议行（含 `:` 心跳）都重置；
                    # - 绝对上限 abs_limit：墙钟总生存期，心跳**不**重置（缺口 M）。
                    remaining = abs_limit - (time.monotonic() - started)
                    if remaining <= 0:
                        terminate_reason = "stream_max_duration"  # §8.1.1 收尾，见 finally 后
                        break
                    try:
                        line = await asyncio.wait_for(
                            line_iter.__anext__(), timeout=min(idle_limit, remaining)
                        )
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        if abs_limit - (time.monotonic() - started) <= 0:
                            # 绝对上限命中（即便心跳在续命）→ §8.1.1，非 §8.4 降级
                            terminate_reason = "stream_max_duration"
                            break
                        # 纯沉默超空闲上限 → 断开走 §8.4 降级（缺口 K）
                        breaker.record_failure()
                        raise ModelServiceError(
                            "upstream_timeout",
                            f"模型服务流式空闲超过 {idle_limit}s（无任何字节），"
                            "已断开并按 §8.4 降级（协议 §8.1 缺口 K）",
                            status=504,
                        ) from None
                    if not line:
                        continue
                    if line.startswith(":"):
                        continue  # keepalive 注释行，平台必须忽略（§6.4）
                    if line.startswith("event:"):
                        current_event = line[len("event:") :].strip()
                        continue
                    if not line.startswith("data:"):
                        continue  # 未知字段，宽松解析忽略
                    payload = line[len("data:") :].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if current_event == "model_service":
                        # 终止事件（§6.3）：承载扩展字段，不污染 OpenAI chunk
                        inner = obj.get("x_model_service")
                        if isinstance(inner, dict):
                            xms = inner
                        current_event = None
                        continue
                    current_event = None
                    choices = obj.get("choices") or []
                    if choices:
                        choice = choices[0]
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if content:
                            parts.append(content)
                            yield content
                        # §5：finish_reason 必须归因（此前恒为 None，账本失去价值）
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                    if isinstance(obj.get("usage"), dict):
                        usage = obj["usage"]  # usage chunk（choices 为空）
        except httpx.HTTPError as exc:
            # §11：流式是主路径，传输失败（连接错/读超时）必须计入熔断（审计 A）。
            # 只在此记一次；`_unavailable` 已不再记，修掉此前流式连接错被双计数（缺陷①）。
            breaker.record_failure()
            raise await self._unavailable(f"流式连接失败：{exc!r}", model=model) from exc
        finally:
            await client.aclose()

        if terminate_reason == "stream_max_duration":
            # §8.1.1-4：绝对上限属连接类失败，计入熔断；但不 raise，落到下面按已收 token 结算。
            breaker.record_failure()
        else:
            breaker.record_success()
        prompt, completion, cache_read, reasoning = _usage_from(usage)
        metering = "provider"
        if prompt == 0 or completion == 0:
            metering = "tiktoken"
            joined = "".join(m.get("content", "") for m in messages)
            prompt = prompt or _tiktoken_count(joined, model) or _estimate_tokens(joined)
            completion = completion or _tiktoken_count("".join(parts), model) or _estimate_tokens(
                "".join(parts)
            )
            if prompt == 0 or completion == 0:
                metering = "estimated"
        model_used = xms.get("model_used") or model
        yield StreamDone(
            model=model_used,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_read_tokens=cache_read,
            reasoning_tokens=reasoning,
            finish_reason=finish_reason,
            metering_source=metering,
            needs_review=metering != "provider",
            provider=xms.get("provider"),
            model_used=xms.get("model_used"),
            cost_usd=_parse_cost(xms.get("cost_usd")),
            cost_status=xms.get("cost_status"),
            fallback_used=bool(xms.get("fallback_used", False)),
            terminate_reason=terminate_reason,
        )
