"""幂等窗口（协议 §8.2 / §8.2.1，v1.5 三分表）。

入窗判定按「是否可能已计费 / 同输入是否同结果」，**不是按状态码段**：

| 情形 | 入窗 |
|---|---|
| 成功响应 | ✅ 回放 |
| 结果不明（超时、已发起调用后的 5xx） | ✅ 必须入窗（防双烧） |
| 确定性拒绝（413 / 上游 404） | ✅ 入窗（同输入必同拒绝） |
| 瞬时拒绝（上游 429 / 503，`retryable=true`） | ❌ 不入窗（重发可能成功） |
| 本地拒绝（400/401/403、未送达） | ❌ 不入窗（无副作用） |

`retryable` 是唯一开关：true ⇒ 不入窗；false 且已伴随上游调用 ⇒ 入窗。
"""

import time
from dataclasses import dataclass


@dataclass
class CachedResponse:
    status: int
    body: dict
    headers: dict[str, str]
    expires_at: float


class IdempotencyWindow:
    def __init__(self, window_seconds: int = 600):
        self.window_seconds = window_seconds
        self._store: dict[str, CachedResponse] = {}
        self._inflight: set[str] = set()

    def get(self, key: str) -> CachedResponse | None:
        hit = self._store.get(key)
        if hit is None:
            return None
        if time.monotonic() >= hit.expires_at:
            del self._store[key]
            return None
        return hit

    def begin(self, key: str) -> bool:
        """返回 False 表示已有同 key 请求在飞（并发）——调用方应 409。"""
        if key in self._inflight:
            return False
        self._inflight.add(key)
        return True

    def finish(self, key: str, resp: CachedResponse | None) -> None:
        """resp=None 表示「不入窗」（瞬时/本地/未送达失败）→ 仅清除在飞标记。"""
        self._inflight.discard(key)
        if resp is not None:
            self._store[key] = resp

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
