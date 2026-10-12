"""场景化的**真服务**入口（RB-2④ 对拍用）。

`msvc` 的场景（`app.state.scenario`）是测试夹具里的进程内对象，HTTP 客户端碰不到它；
平台侧要拿**真实服务**对拍心跳/超时这类**时序**行为，就必须有人在建 app 之后把场景设上。
本模块只做这件事：读 `MS_SCENARIO_JSON` ⇒ 用真 `create_app()` 起真 uvicorn。

跑法（父进程传 env）::

    MS_SERVICE_TOKENS=t MS_SCENARIO_JSON='{"reasoning_only_chunks":12,"reasoning_chunk_interval":0.25}' \
    python model-service/tests/serve_scenario.py            # 监听 127.0.0.1:$MS_SERVE_PORT
"""

import json
import os

import uvicorn

from msvc.config import get_settings
from msvc.main import create_app
from msvc.provider import Scenario


def build():
    app = create_app()
    raw = os.environ.get("MS_SCENARIO_JSON")
    if raw:
        app.state.scenario = Scenario(**json.loads(raw))
    return app


if __name__ == "__main__":
    port = int(os.environ.get("MS_SERVE_PORT", "8731"))
    get_settings.cache_clear()
    uvicorn.run(build(), host="127.0.0.1", port=port, log_level="warning")
