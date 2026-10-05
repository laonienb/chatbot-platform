"""应用工厂：路由注册、CORS、/v1 路径下的 OpenAI 风格错误格式。"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.native import admin, auth, conversations, me, memories, models, personas
from app.api.openai_compat import chat as openai_chat
from app.billing.charge import InsufficientBalance
from app.billing.monitor import summarize_margin
from app.billing.quota import QuotaExceeded, reconcile_pending
from app.config import Settings, get_settings
from app.database import SessionLocal

logger = logging.getLogger(__name__)


async def billing_watch_tick(session) -> dict:
    """对账循环的一轮：收编残留 pending + 毛利倒挂告警。返回本轮摘要（供测试断言）。

    进程被 kill 留下的 pending 行若不收编，会永久占额度且账面永远「在飞」；
    负毛利（换算后 billed < upstream）是费率配错的直接证据，必须有人看见。
    """
    abandoned = await reconcile_pending(session)
    if abandoned:
        logger.warning("对账：收编 %d 条残留 pending 账行（needs_review，待人工复核）", abandoned)
    summary = await summarize_margin(session)
    if summary["violations"]:
        logger.warning(
            "毛利告警：%d 条负毛利账行（收入 %s USD < 成本 %s USD），检查费率规则",
            summary["violations"], summary["revenue_usd"], summary["upstream_total"],
        )
    return {"abandoned": abandoned, **summary}


async def _billing_watch(interval_seconds: int) -> None:
    """对账循环（billing step 5/6）：按 reconcile_interval_seconds 周期执行 tick。"""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            async with SessionLocal() as session:
                await billing_watch_tick(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("对账循环本轮失败（下轮重试）")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        interval = settings.reconcile_interval_seconds
        task = asyncio.create_task(_billing_watch(interval)) if interval > 0 else None
        yield
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title=f"{settings.app_name} API",
        version="0.1.0",
        debug=settings.debug,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def is_openai_compat(request: Request) -> bool:
        return request.url.path.startswith("/v1")

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        if is_openai_compat(request):
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": {"message": str(exc.detail), "type": "invalid_request_error", "param": None, "code": None}},
                headers=exc.headers,
            )
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        detail = jsonable_encoder(exc.errors())
        if is_openai_compat(request):
            return JSONResponse(
                status_code=422,
                content={"error": {"message": str(detail), "type": "invalid_request_error", "param": None, "code": None}},
            )
        return JSONResponse(status_code=422, content={"detail": detail})

    def billing_error(request: Request, exc: Exception, status_code: int, err_type: str) -> JSONResponse:
        if is_openai_compat(request):
            return JSONResponse(
                status_code=status_code,
                content={"error": {"message": str(exc), "type": err_type, "param": None, "code": None}},
            )
        return JSONResponse(status_code=status_code, content={"detail": str(exc)})

    @app.exception_handler(QuotaExceeded)
    async def quota_handler(request: Request, exc: QuotaExceeded) -> JSONResponse:
        return billing_error(request, exc, 429, "insufficient_quota")

    @app.exception_handler(InsufficientBalance)
    async def balance_handler(request: Request, exc: InsufficientBalance) -> JSONResponse:
        return billing_error(request, exc, 402, "insufficient_balance")

    app.include_router(auth.router)
    app.include_router(personas.router)
    app.include_router(conversations.router)
    app.include_router(me.router)
    app.include_router(models.router)
    app.include_router(models.admin_router)
    app.include_router(memories.router)
    app.include_router(admin.router)
    app.include_router(openai_chat.router)

    @app.get("/healthz", tags=["meta"])
    async def healthz():
        return {"status": "ok", "backend": settings.llm_backend}

    return app


app = create_app()
