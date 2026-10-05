"""RPM 限流（billing step 6）：可插拔的进程内滑动窗口。

设计（见与用户的方案讨论）：
- 限流与配额是**两件事** —— RPM 是防滥用（可近似、可丢），配额是控成本
  （必须精确、必须持久化）。因此限流用进程内窗口，配额走账本。
- 单 worker 部署（当前 Dockerfile 即是）下进程内窗口是精确的；
  将来多 worker 时把 Limiter 换成 Redis 实现（接口不变），无需改业务代码。
- 超限返回 429 + Retry-After（OpenAI 生态工具按此退避）。
"""

import time
from collections import defaultdict, deque
from threading import Lock

from fastapi import Depends, HTTPException, Request, status

from app.api.deps import get_api_key_principal, get_current_user
from app.config import get_settings


class SlidingWindowLimiter:
    """滑动窗口计数：key → 时间戳队列。进程内，多线程安全（threading.Lock）。"""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str, limit: int, window_seconds: int) -> tuple[bool, int]:
        """记录一次命中并判断是否超限。返回 (是否放行, 剩余秒数)。"""
        if limit <= 0:
            return True, 0
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            q = self._hits[key]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= limit:
                retry_after = max(1, int(q[0] + window_seconds - now) + 1)
                return False, retry_after
            q.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# 进程级单例（测试可通过 dependency override 替换）
limiter = SlidingWindowLimiter()


async def rate_limit(request: Request) -> None:
    """通用限流依赖：读 request.state（由鉴权依赖填充）。

    用法：路由上声明 `Depends(rate_limit)`，且该路由已有鉴权依赖
    （CurrentUser 或 get_api_key_principal）—— 二者在 FastAPI 里按依赖
    图顺序执行，鉴权先填充 state，限流再读。
    """
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        api_key, _user = principal
        limit = api_key.rpm_limit or get_settings().rpm_default
        key = f"apikey:{api_key.id}"
    else:
        user = getattr(request.state, "user", None)
        if user is None:
            return  # 未认证 → 由鉴权依赖给 401，不在此限流
        limit = get_settings().rpm_default
        key = f"user:{user.id}"

    allowed, retry_after = limiter.check(key, limit, window_seconds=60)
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate limit exceeded",
            headers={"Retry-After": str(retry_after)},
        )


async def rate_limit_native(request: Request, user=Depends(get_current_user)) -> None:
    """原生面限流：Depends(get_current_user) 保证鉴权先执行（request.state.user 已填充）。"""
    await rate_limit(request)


async def rate_limit_compat(request: Request, principal=Depends(get_api_key_principal)) -> None:
    """兼容面限流：Depends(get_api_key_principal) 保证鉴权先执行。"""
    await rate_limit(request)
