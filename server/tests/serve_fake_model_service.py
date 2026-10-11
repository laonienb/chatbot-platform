"""可编程地跑起 fake model-service（协议 §10.2 的桩），用来在**本地真造流式错误形态**。

为什么需要它：`AGENTS.md`「三」登记过「上游限流/超时在本地开发环境不可复现」，前端只能靠
静态证据替代结论。桩本身早就有注入开关（`Control.stream_error` 等），但那些开关只在
pytest 进程内可改——真实 uvicorn 起来后没有入口。本脚本补的就是这个入口：一个 **HTTP 控制面**
（`/__control/scenario`），让你在不起真上游、不改生产默认的前提下当场换剧本。

⚠️ 三条边界
- 这是 **测试桩**，只活在 `server/tests/`，平台应用代码不 import 它；`deploy/docker-compose.yml`
  里也没有它的服务条目。**绝不可当作生产服务暴露**（控制面无鉴权，正是为了能随手改剧本）。
- 不改任何生产默认：`LLM_BACKEND` 的 `remote` 只出现在下面命令行的**临时环境变量**里，
  不要把 `deploy/.env` 或 compose 翻转（那是 DW-4，待用户定）。
- 数据库用**仓库外的临时文件**，别用 `server/dev.db`——那是共享库，别的会话正在读里面的毛利数字。

────────────────────────────────────────────────────────────────────────────
## 完整可复制序列（三条命令起两个进程，然后 curl 看三种形态）

前置：Windows 用 Git Bash；两个终端。`$TMPDB` 指仓库外的临时库路径。

    # 终端 A —— 起桩（fake 上游，带控制面）
    cd server && .venv/Scripts/python -m tests.serve_fake_model_service --preset normal --port 8123

    # 终端 B —— 建库 + 起平台，指向那个桩（env 只在本次 shell 生效，不入库）
    cd server
    export TMPDB="$(mktemp -d)/sse-faults.db"
    export DATABASE_URL="sqlite+aiosqlite:///$TMPDB" \
           LLM_BACKEND=remote \
           MODEL_SERVICE_MODE=native \
           MODEL_SERVICE_BASE_URL=http://127.0.0.1:8123 \
           MODEL_SERVICE_TOKEN=dev-token \
           SECRET_KEY=local-live-check-secret-key-0123456789abcdef
    .venv/Scripts/python -m alembic upgrade head
    .venv/Scripts/python -m uvicorn app.main:app --port 8010

    # 一次性准备：注册 → 建人设 → 建会话（下面第 4 步复用这三个值）
    TOKEN=$(curl -s -XPOST localhost:8010/api/v1/auth/register \
      -H 'Content-Type: application/json' \
      -d '{"email":"sse-probe@local.dev","password":"password123","display_name":"SSE Probe"}' \
      | .venv/Scripts/python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
    PID=$(curl -s -XPOST localhost:8010/api/v1/personas \
      -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
      -d '{"name":"探针","slug":"sse-probe","system_prompt":"你是探针。"}' \
      | .venv/Scripts/python -c "import sys,json;print(json.load(sys.stdin)['id'])")
    CID=$(curl -s -XPOST localhost:8010/api/v1/conversations \
      -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
      -d "{\"persona_id\":\"$PID\"}" \
      | .venv/Scripts/python -c "import sys,json;print(json.load(sys.stdin)['id'])")

    # 第 4 步 —— 每换一次剧本，切一次再拉流（`-N` 关闭缓冲，才看得到逐块）
    SSE() {  # 用法：SSE <preset>
      curl -sN -XPOST "http://127.0.0.1:8123/__control/scenario" \
           -H 'Content-Type: application/json' -d "{\"preset\":\"$1\"}" >/dev/null
      echo "───── preset=$1 ─────"
      curl -sN -XPOST "localhost:8010/api/v1/conversations/$CID/messages?stream=true" \
        -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
        -d '{"content":"说一句","stream":true}'
      echo
    }
    SSE rate_limited       # ① 流内错误帧 rate_limited + retry_after_s，随后 [DONE]
    SSE chunk_timeout      # ② 首字节之后超时 → 流内错误帧，随后 [DONE]
    SSE abort              # ③ 模型服务不带 [DONE] 就断流（看平台是否兜底）

三种形态各自**实测**看到什么（2026-10-11 23:4x 本机跑通，SSE 原文见 `AGENTS.md`「三」）：

| preset | 桩做的事 | 平台对外（`/api/v1/...` 的 SSE）实测结果 |
|---|---|---|
| `normal` | 干净流 | 4 个内容 chunk → `data: [DONE]`，无错误帧 |
| `rate_limited` | 发 1 个 chunk 后 `data:{"error":{code:"rate_limited",retry_after_s:30,retryable:true}}`，再 `[DONE]` | **原样透出**：1 chunk + 错误帧（含 `retry_after_s`）+ `[DONE]` |
| `chunk_timeout` | 同上但 code `upstream_timeout`（2 chunk 后） | 2 chunk + 错误帧 + `[DONE]`；平台按码**计入**熔断（`rate_limited` 不计） |
| `pre_first_byte_429` | 首字节**前**返回 HTTP 429 + `Retry-After: 7` | 错误帧 `code:rate_limited, retry_after_s:7` + `[DONE]`（HTTP header 被转成帧内字段——流式路径没有 header 可带） |
| `abort` | 生成器里抛异常，连接被掐、**桩侧不发** `[DONE]` | ⚠️ 客户端仍看到 `data:{"error":{code:"service_unavailable",…}}` **+ `[DONE]`** —— 服务侧断流被**平台兜底补全**了 |

**所以「没有 `[DONE]`」这一形造不出来吗？造得出来，但只能在客户端这一侧造**：截断必须发生在
**平台自身或平台与客户端之间**，模型服务侧断流一律会被兜底补上 `[DONE]`。实测做法是拉慢桩 +
客户端提前退出（不需要杀进程，Windows/Git-Bash 下 `kill -9` 打不死 uvicorn，SIGTERM 又是优雅收尾）：

    curl -s -XPOST "http://127.0.0.1:$STUB/__control/scenario" -H 'Content-Type: application/json' \
         -d '{"preset":"normal","stream_chunk_delay_seconds":2.5}'
    curl -sN --max-time 9 -XPOST "http://127.0.0.1:$SRV/api/v1/conversations/$CID/messages" \
         -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
         -d '{"content":"slow","stream":true}' > out.txt   # 用 > 重定向，不要用 -o
    # 实测：data 行=3、含 [DONE]=0、含 error 帧=0，curl 退出码 28（超时中断）
    #       —— 这就是浏览器 fetch 遇到连接被掐时看到的东西，前端 truncated 判据认它

────────────────────────────────────────────────────────────────────────────
## Windows / Git-Bash 三条实测坑（不写下来就会浪费你一小时）

1. **capture 的输出一律过 `tr -d '\r'`。** 本仓库的 python 是 Windows 版，stdout 是 CRLF；
   `PORTS=$(python -c …)` 会把 `\r` 带进变量，于是 `curl http://127.0.0.1:6268\r/docs` 永远
   返回 `000`、文件名变成 `live-6268\r.db` 并报 `unable to open database file`。
2. **curl 的 argv 会被按 GBK 重编码**，所以 `-d '{"name":"探针"}'` 这种**内联中文**在服务端
   只得到 `{"detail":"There was an error parsing the body"}`。要么载荷全用 ASCII，要么
   写进文件用 `--data-binary "@file"`（实测两者都通，纯 ASCII 最省事，本文件里的命令都这么写）。
3. **临时库路径要给 Windows 形式**（`D:/...` 正斜杠即可），Git-Bash 的 `/tmp/...` 交给
   SQLAlchemy 打不开。且**别用 `server/dev.db`**——那是共享库，别的会话正在读里面的毛利数字。

另有两条顺手记下的：`-o file` 在 curl 被超时中断时可能不落文件，用 shell 的 `>`；
控制面 `{"preset":…,"其他键":…}` 现在**会**把覆盖键叠在预设之后（先前版本静默丢掉，我就踩过一次：
`stream_chunk_delay_seconds` 没生效，流秒完，于是「截断形」看起来永远造不出来）。
────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import uvicorn

from tests.fake_model_service import Control, build_app

# 预设剧本：名字 → 要覆盖的 Control 字段。只列**与默认值不同**的键，其余保持默认。
PRESETS: dict[str, dict[str, Any]] = {
    # 干净流：内容 → finish_reason → usage → 终止事件 → [DONE]
    "normal": {},
    # ① 首字节之后的限流：流内错误帧带 retry_after_s（v1.7-澄清2），其后仍 [DONE]
    "rate_limited": {
        "stream_error": {"code": "rate_limited", "message": "上游限流（本地注入）",
                         "retry_after_s": 30, "retryable": True},
        "stream_error_after_chunks": 1,
        "stream_error_usage": True,  # G3-2：帧带实测部分 usage
    },
    # ② 首字节之后的超时：与 rate_limited 的差别只在 code（平台按码决定是否计熔断）
    "chunk_timeout": {
        "stream_error": {"code": "upstream_timeout", "message": "上游中途空闲超预算（本地注入）",
                         "retryable": False},
        "stream_error_after_chunks": 2,
    },
    # ②b 服务侧自己产生的中途空闲码（O1 归因，平台计熔断）
    "chunk_idle": {
        "stream_error": {"code": "upstream_chunk_timeout", "message": "chunk 间隔预算耗尽（本地注入）",
                         "retryable": False},
        "stream_error_after_chunks": 2,
    },
    # ③ 不带 [DONE] 直接掐断连接
    "abort": {"abort_after_chunks": 1, "stream_chunk_delay_seconds": 0.3},
    # 附赠：首字节**之前**就 429（该形态走 HTTP 错误状态，平台在开流前就 raise）
    "pre_first_byte_429": {
        "error": {"code": "rate_limited", "retry_after": 7, "retryable": True},
        "stream_error": None,
    },
    # 附赠：沉默但持续心跳（平台空闲计时不该误杀，绝对上限才该切）
    "keepalive_stall": {
        "silence_after_chunks": 1, "keepalive_during_silence": True, "silence_seconds": 20.0,
    },
}

# 控制面只允许覆盖这些字段——免得有人把桩改成不认识的东西后 debug 半天
KNOBS = (
    "cost_mode", "provider", "model_used", "prompt_tokens", "completion_tokens", "omit_usage",
    "error", "error_sequence", "delay_seconds", "drop_connection", "emit_terminate_event",
    "emit_keepalive", "finish_reason", "stream_chunk_delay_seconds", "silence_after_chunks",
    "keepalive_during_silence", "silence_seconds", "stream_error", "stream_error_after_chunks",
    "stream_error_usage", "abort_after_chunks", "models", "capabilities",
    "compliance_denied_providers",
)


def apply(control: Control, name: str) -> None:
    """把某个预设**整体**盖到当前 Control 上（先复位成默认，再叠加）。"""
    if name not in PRESETS:
        raise KeyError(f"未知 preset {name!r}，可选：{sorted(PRESETS)}")
    fresh = Control()
    for f in list(vars(fresh)):
        setattr(control, f, getattr(fresh, f))
    control.calls.clear()
    control.idempotency_seen.clear()
    for k, v in PRESETS[name].items():
        setattr(control, k, v)


def serveable_app(control: Control):
    """桩 app ＋ 一个 `/__control/scenario` 控制面（GET 读当前剧本 / POST 换剧本）。"""
    app = build_app(control)

    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    ctl = control  # 别在闭包里再叫 control：端点函数一旦同名就会遮蔽实例（我第一版就中了）

    def _snapshot() -> dict[str, Any]:
        keys = ("stream_error", "stream_error_after_chunks", "stream_error_usage",
                "abort_after_chunks", "silence_after_chunks", "keepalive_during_silence",
                "silence_seconds", "error", "finish_reason", "upstream_started",
                "stream_finished")
        return {k: getattr(ctl, k, None) for k in keys}

    async def control_endpoint(request: Request) -> JSONResponse:
        if request.method == "GET":
            return JSONResponse({"preset": ctl.preset, "now": _snapshot()})
        body = await request.json()
        overrides = {k: v for k, v in body.items() if k != "preset"}
        unknown = [k for k in overrides if k not in KNOBS]
        if unknown:
            # 未知键必须报错：静默丢掉会让调用方以为剧本生效了（我自己就踩过一次
            # {"preset":"normal","stream_chunk_delay_seconds":1.3} 里 delay 被吞掉）
            return JSONResponse({"error": f"未知字段 {unknown}，允许：{sorted(KNOBS)}"}, status_code=400)
        if "preset" in body:
            apply(ctl, body["preset"])
            ctl.preset = body["preset"]
        for k, v in overrides.items():  # preset 之后再叠覆盖，两者可同传
            setattr(ctl, k, v)
        return JSONResponse({"preset": ctl.preset, "now": _snapshot()})

    # 一条路由两种方法（不要注册两条同路径不同 methods 的 Route：Starlette 对
    # 「路径匹配但方法不匹配」留的是 partial match，读代码的人很容易误判成 405）
    app.router.routes.insert(0, Route("/__control/scenario", control_endpoint, methods=["GET", "POST"]))
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--preset", default="normal", choices=sorted(PRESETS))
    args = ap.parse_args()

    control = Control()
    control.preset = args.preset  # 挂在桩上当调试标签，GET 时回显
    apply(control, args.preset)
    app = serveable_app(control)

    print(json.dumps({"serving": f"http://{args.host}:{args.port}", "preset": args.preset,
                      "knobs": {k: v for k, v in PRESETS[args.preset].items()}},
                     ensure_ascii=False, indent=2), flush=True)
    # log_level=info：能看到桩侧每个请求，配合平台侧日志定位「谁先断的」
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", log_config=None)


if __name__ == "__main__":  # pragma: no cover
    # 线程无关：本脚本自己就是主进程。测试里不用它（测试直接 build_app(Control())）。
    main()
