"""model-service 测试夹具：ASGITransport 直连真实 app；T6 用真 uvicorn（取消只在真连接上可验）。"""

import os
import sys
import threading
import time

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from msvc.config import get_settings  # noqa: E402
from msvc.main import create_app  # noqa: E402
from msvc.provider import Scenario  # noqa: E402

TOKEN = get_settings().service_tokens.split(",")[0].strip()


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def app():
    application = create_app()
    application.state.scenario = Scenario()
    application.state.upstream.reset()
    application.state.window.clear()
    return application


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ms") as c:
        c.headers.update(auth_headers())
        yield c


@pytest.fixture
def scenario(app):
    """测试内切换场景：`scenario.error = {...}` 等。"""
    s = Scenario()
    app.state.scenario = s
    return s


@pytest.fixture
def live_server(app):
    """真 uvicorn 线程（T6 取消传播必须真实 TCP 断连）。"""
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not server.started:
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
