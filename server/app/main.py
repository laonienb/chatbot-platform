"""应用工厂：路由注册、CORS、/v1 路径下的 OpenAI 风格错误格式。"""

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.native import auth, conversations, me, models, personas
from app.api.openai_compat import chat as openai_chat
from app.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title=f"{settings.app_name} API",
        version="0.1.0",
        debug=settings.debug,
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

    app.include_router(auth.router)
    app.include_router(personas.router)
    app.include_router(conversations.router)
    app.include_router(me.router)
    app.include_router(models.router)
    app.include_router(models.admin_router)
    app.include_router(openai_chat.router)

    @app.get("/healthz", tags=["meta"])
    async def healthz():
        return {"status": "ok", "backend": settings.llm_backend}

    return app


app = create_app()
