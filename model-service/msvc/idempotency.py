"""幂等窗口（协议 §8.2 / §8.2.1 / §8.2.2，v1.7）。

入窗判定按「是否可能已计费 / 同输入是否同结果」，**不是按状态码段**：

| 情形 | 入窗 |
|---|---|
| 成功响应 | ✅ 回放 |
| 结果不明（超时、已发起调用后的 5xx） | ✅ 必须入窗（防双烧） |
| 确定性拒绝（413 / 上游 404 等永久性拒绝） | ✅ 入窗（同输入必同拒绝） |
| 瞬时拒绝（上游 429 / 503，`retryable=true`） | ❌ 不入窗（重发可能成功） |
| 本地拒绝（400/401/403、未送达） | ❌ 不入窗（无副作用） |

入窗判据是**两个布尔**（§8.2.1）：`retryable=True` ⇒ 不入窗；`retryable=False`
且**已到达上游** ⇒ 入窗；成功响应（2xx）单列。`status` 只用于成功判定，
**不存在 `status in (...)` 式的白名单分支**（v1.6 澄清明文禁止该模式）。

流式（§8.2.2，v1.7）：窗口对**「流式 / 非流式」同等适用**；回放形态是**终态重放**
——命中时用缓存的重放内容**重新生成一条 SSE 流**（内容文本/usage/finish_reason/
终止事件/`[DONE]` 与首次一致，chunk 边界允许不同）。缓存**终态**有界（一段文本 +
一份 usage），缓存**事件序列**会随生成长度无界增长，故不缓存序列。
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("msvc.idempotency")


@dataclass
class StreamTerminal:
    """流式终态（§8.2.2-3「终态重放」的缓存对象）。

    - `error` 非 None ⇒ 首字节**之后**的流内错误帧终止；否则为成功终止。
    - `usage` 为错误帧可携带的实测部分用量（§6.7 G3-2，加法字段）。
    """

    content: str = ""
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    x_model_service: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


@dataclass
class CachedResponse:
    status: int
    body: dict
    headers: dict[str, str]
    expires_at: float
    # `stream` 非 None ⇒ 命中后必须**重新生成一条 SSE 流**（§8.2.2）；
    # 为 None ⇒ 回放 JSON（非流式响应，或首字节前的 HTTP 错误）。
    stream: StreamTerminal | None = None


def _size(resp: CachedResponse) -> int:
    """条目体积估算（字符数）：JSON 体 + 流式终态的内容/usage。"""
    n = len(json.dumps(resp.body, ensure_ascii=False))
    if resp.stream is not None:
        n += len(resp.stream.content)
        if resp.stream.usage:
            n += len(json.dumps(resp.stream.usage, ensure_ascii=False))
        if resp.stream.x_model_service:
            n += len(json.dumps(resp.stream.x_model_service, ensure_ascii=False))
        if resp.stream.error:
            n += len(json.dumps(resp.stream.error, ensure_ascii=False))
    return n


class IdempotencyWindow:
    def __init__(
        self,
        window_seconds: int = 600,
        *,
        max_entries: int = 256,
        max_chars: int = 1_000_000,
    ):
        self.window_seconds = window_seconds
        # §8.2.2-3：缓存必须有**条数/体积上限**（本节两个上限）。
        self.max_entries = max_entries
        self.max_chars = max_chars
        self._store: dict[str, CachedResponse] = {}
        self._inflight: set[str] = set()
        self._chars = 0

    def get(self, key: str) -> CachedResponse | None:
        hit = self._store.get(key)
        if hit is None:
            return None
        if time.monotonic() >= hit.expires_at:
            self._chars -= _size(hit)
            del self._store[key]
            return None
        return hit

    def begin(self, key: str) -> bool:
        """返回 False 表示已有同 key 请求在飞（并发）——调用方应 409。

        §8.2.2-2：在飞位语义对**流式同样适用**（防同 key 并发双流各自真实调用上游）。
        """
        if key in self._inflight:
            return False
        self._inflight.add(key)
        return True

    def finish(self, key: str, resp: CachedResponse | None) -> None:
        """resp=None 表示「不入窗」（瞬时/本地/未送达失败、或终态未知）→ 仅清除在飞标记。"""
        self._inflight.discard(key)
        if resp is None:
            return
        old = self._store.pop(key, None)
        if old is not None:
            self._chars -= _size(old)
        self._store[key] = resp
        self._chars += _size(resp)
        self._enforce_limits()

    def _enforce_limits(self) -> None:
        """先清过期项，再按**插入序**淘汰最旧项（§8.2.2-3 的条数/体积上限）。

        淘汰策略与后果**已显式声明在 `model-service/README.md`**：被淘汰的 key 后续重发
        按**未命中**处理（会真实调用上游），且每次淘汰都打 WARNING 日志——
        不允许"淘汰后静默重调上游"（静默 = 既不声明也不留痕）。
        """
        now = time.monotonic()
        for k in [k for k, v in self._store.items() if now >= v.expires_at]:
            self._chars -= _size(self._store.pop(k))
        while self._store and (
            len(self._store) > self.max_entries or self._chars > self.max_chars
        ):
            key, victim = next(iter(self._store.items()))  # dict 保序 = 插入序（最旧）
            self._chars -= _size(victim)
            del self._store[key]
            logger.warning(
                "幂等窗容量超限（上限 entries=%d chars=%d，当前 entries=%d chars=%d）："
                "淘汰最旧条目 key=%s。该 key 后续重发按未命中处理（§8.2.2-3，语义见 README）",
                self.max_entries,
                self.max_chars,
                len(self._store) + 1,
                self._chars,
                key,
            )

    def should_cache(self, status: int, retryable: bool, reached_upstream: bool) -> bool:
        """§8.2.1 三分表判定：这次结果是否入窗。"""
        if retryable:  # 瞬时拒绝 / 未送达 → 不入窗
            return False
        if 200 <= status < 300:
            return True  # 成功
        if status in (413, 404):
            return True  # 确定性拒绝（同输入必同拒绝）
        if 500 <= status < 600 and reached_upstream:
            return True  # 结果不明（已发起调用后的 5xx/超时）→ 防双烧必须入窗
        return False  # 本地拒绝（400/401/403）等 → 不入窗

    def clear(self) -> None:
        self._store.clear()
        self._inflight.clear()
        self._chars = 0
