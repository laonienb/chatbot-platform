"""A-1 的防线用例（协议 §26.6-丙 五项落点，架构师 A 起草 · B 会签 `0c294b9`）。

命名按落点编号 `test_a1_<项>_*`，便于终轮验收逐条对账：

| 项 | 落点 | 本文件用例 |
|---|---|---|
| 1 | `provider.py` 传 `custom_llm_provider` | `test_a1_1_*` |
| 2 | `config.py` 新增 `MS_LITELLM_PROVIDER`（协议名） | `test_a1_2_*` |
| 3 | `MS_CATALOG_MODELS` 必须含目标 model（无前缀走目录校验） | `test_a1_3_*` |
| 4 | `config.py` 新增 `MS_UPSTREAM_VENDOR` ＋ 启动期断言（红线5 对真实出口对账） | `test_a1_4_*` |
| 5 | `provider.py:_xms` 的 `provider` 归因取 vendor（B 2.3 🔴 静默错账） | `test_a1_5_*` |

被测对象是 `LiteLLMProvider` 本体与 `Settings`/`create_app`，上游边界用**只记录不模拟**的
假 `litellm` 模块（注入 `sys.modules`）——`FakeUpstream` 在这里没有参考价值：五项落点全部
发生在 litellm 调用参数与归因字段上（工作单 §24.2 R4 的同款盲区教训）。
"""

import os
import sys
import types

import httpx
import pytest
from pydantic import ValidationError

from msvc.config import Settings, get_settings
from msvc.main import create_app
from msvc.provider import LiteLLMProvider, Scenario

TOKEN = Settings().service_tokens.split(",")[0].strip()


# ---------------------------------------------------------------- env 与 app
@pytest.fixture
def ms_env():
    """设 `MS_*` env 并重建配置缓存；用例结束自动回滚。

    两个坑都在这里防住：
    - `get_settings()` 是 `lru_cache`——不清缓存则 env 改了、代码仍读旧值，用例**永远绿**；
    - env 必须在**整个用例期间**有效：建 app 后就还原，会让 handler 里的
      `get_settings()` 读回默认值（写第一版时就这样静默失效过一次）。
    """
    saved = dict(os.environ)

    def _set(**env: str):
        for k, v in env.items():
            if v == "":
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()

    yield _set

    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://ms",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


# ---------------------------------------------------------------- 只记录的真上游边界
class _RecordingLiteLLM:
    """注入 `sys.modules["litellm"]`：抄下 `LiteLLMProvider` 递出的 kwargs，返回正常响应。

    不做故障模拟（那是 `test_real_upstream.py` 的职责）。响应里的 model 用**无斜杠**的
    `mimo-v2.6-flash`——真实 OpenAI 兼容网关就长这样，而第5项的错账正发生在这种 model 上。
    """

    def __init__(self, model="mimo-v2.6-flash", price_ok=False):
        self.model = model
        self.price_ok = price_ok
        self.calls: list[dict] = []

    def install(self, monkeypatch):
        mod = types.ModuleType("litellm")

        async def acompletion(**kw):
            self.calls.append(kw)
            if kw.get("stream"):
                async def gen():
                    yield {
                        "model": self.model,
                        "choices": [{"index": 0, "delta": {"content": "你好"}, "finish_reason": None}],
                    }
                    yield {
                        "model": self.model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                    yield {
                        "model": self.model,
                        "choices": [],
                        "usage": {"prompt_tokens": 21, "completion_tokens": 7, "total_tokens": 28},
                    }
                return gen()
            return {
                "id": "chatcmpl-a1",
                "model": self.model,
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "你好"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 21, "completion_tokens": 7, "total_tokens": 28},
            }

        def cost_per_token(**kw):
            if not self.price_ok:
                raise KeyError(f"no price entry for {kw.get('model')}")
            return (1e-7, 2e-7)

        mod.acompletion = acompletion
        mod.cost_per_token = cost_per_token
        mod.exceptions = types.SimpleNamespace()
        monkeypatch.setitem(sys.modules, "litellm", mod)
        return self


def _body(model="mimo-v2.6-flash", **kw):
    b = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    b.update(kw)
    return b


async def _collect(agen):
    return [ev async for ev in agen]


# ================================================================ 项1 / 项2：协议名进 kwargs
async def test_a1_1_unprefixed_model_passes_custom_llm_provider(monkeypatch):
    """项1（A-1 核心）：无前缀 model 必须带上 `custom_llm_provider`，否则 litellm 定不出 provider。"""
    rec = _RecordingLiteLLM().install(monkeypatch)
    await LiteLLMProvider().complete(_body(), Scenario())
    assert rec.calls[0].get("custom_llm_provider") == "openai", (
        "§26.6-丙 第1项未落地：litellm 收到裸 model 会报未知 provider，冒烟直接失败"
    )


async def test_a1_1_prefixed_model_omits_custom_llm_provider(monkeypatch):
    """项1 的边界：**有前缀**时不传——litellm 在 provider 与前缀不一致时再拼一层，
    `deepseek/deepseek-chat` + `openai` ⇒ `openai/deepseek/deepseek-chat`（model id 被弄错）。
    """
    rec = _RecordingLiteLLM(model="deepseek/deepseek-chat").install(monkeypatch)
    await LiteLLMProvider().complete(_body(model="deepseek/deepseek-chat"), Scenario())
    assert "custom_llm_provider" not in rec.calls[0], rec.calls[0]
    assert rec.calls[0]["model"] == "deepseek/deepseek-chat", "model 必须原样交给 litellm"


async def test_a1_1_stream_path_also_carries_provider(monkeypatch):
    """项1 对**流式**同等成立（流式是 native 主路径，漏了它生产切换后首条流即失败）。"""
    rec = _RecordingLiteLLM().install(monkeypatch)
    await _collect(LiteLLMProvider().stream(_body(), Scenario()))
    assert rec.calls[0]["custom_llm_provider"] == "openai"
    assert rec.calls[0]["stream"] is True


def test_a1_2_protocol_name_defaults_to_openai():
    assert Settings().litellm_provider == "openai"


def test_a1_2_protocol_name_binds_from_env(ms_env):
    """项2：协议名来自 `MS_LITELLM_PROVIDER`，不是硬编码——改名必须生效。"""
    ms_env(MS_LITELLM_PROVIDER="groq")
    assert Settings().litellm_provider == "groq"
    assert get_settings().litellm_provider == "groq"


def test_a1_2_provider_and_vendor_env_names_both_bind(ms_env):
    """两个新 env 的名字必须**真的绑到字段**上。

    `extra="ignore"` 会把对不上的 env 静默吞掉：配置看起来生效、其实读的是默认值
    （第一版把字段写成 `litellm_upstream_vendor`，`MS_UPSTREAM_VENDOR` 就是这样被吞掉的，
    用例当时直接变红——这条把「名字即契约」钉住）。
    """
    ms_env(MS_LITELLM_PROVIDER="openai", MS_UPSTREAM_VENDOR="xiaomimimo")
    s = Settings()
    assert (s.litellm_provider, s.upstream_vendor) == ("openai", "xiaomimimo")


async def test_a1_2_kwarg_value_follows_env_not_hardcoded(monkeypatch, ms_env):
    """项2 的另一半：递给出上游的 kwargs **值**必须来自 env——只断言字段绑定抓不到
    `custom_llm_provider="openai"` 这种硬编码（字段照样绑定成功，用例全绿，配置却失效）。
    """
    ms_env(MS_LITELLM_PROVIDER="groq")
    rec = _RecordingLiteLLM().install(monkeypatch)
    await LiteLLMProvider().complete(_body(), Scenario())
    assert rec.calls[0]["custom_llm_provider"] == "groq", rec.calls[0]


# ================================================================ 项3：目录必须含无前缀 model
async def test_a1_3_unprefixed_model_in_catalog_is_not_404(ms_env):
    """项3（B 2.2 🟡）：无前缀 model 走 `main.py` 目录校验 ⇒ 必须列进 `MS_CATALOG_MODELS`。"""
    ms_env(MS_CATALOG_MODELS="deepseek-chat,qwen-plus,mimo-v2.6-flash")
    app = create_app()
    async with _client(app) as c:
        r = await c.post("/v1/chat/completions", json=_body())
        assert r.status_code == 200, f"目录里已声明仍非 200 ⇒ 目录校验位置错了：{r.status_code} {r.text}"
        # 平台 §9.1 同步读 /v1/models，两处必须同源
        ids = [m["id"] for m in (await c.get("/v1/models")).json()["data"]]
    assert "mimo-v2.6-flash" in ids


async def test_a1_3_control_unprefixed_model_absent_from_catalog_is_404(ms_env):
    """同一条的**反向**判据（B 2.2）：env 回默认（不含该 model）⇒ 404 `model_not_found`，
    且报错不得把人引向上游——冒烟若只看这个 code 会误判成上游故障。
    """
    ms_env(MS_CATALOG_MODELS="deepseek-chat,qwen-plus")
    app = create_app()
    async with _client(app) as c:
        r = await c.post("/v1/chat/completions", json=_body())
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "model_not_found"
    assert "上游" not in err["message"], err["message"]


async def test_a1_3_prefixed_model_bypasses_catalog_gate():
    """带前缀的 model 免目录校验——这条**不是** A-1 的接法，写下来钉住两种形态的差别。"""
    app = create_app()
    async with _client(app) as c:
        r = await c.post("/v1/chat/completions", json=_body(model="deepseek/deepseek-chat"))
    assert r.status_code == 200, r.text


# ================================================================ 项4：供应商名 + 启动期对账
def test_a1_4_startup_refuses_when_vendor_hits_deny_list(ms_env):
    """项4（B 2.4）：`upstream=litellm` 且供应商名命中 deny ⇒ **拒绝启动**（红线5 补臂）。

    A-1 之后请求里的 model 无前缀 ⇒ `_check_compliance` 对真实流量恒不命中，真实出口
    只由 env 决定。所以这条判定必须搬到启动期，而不是等请求 403。
    """
    ms_env(MS_UPSTREAM="litellm", MS_UPSTREAM_VENDOR="openai",
           MS_LITELLM_API_BASE="https://api.openai.com/v1")
    with pytest.raises(ValidationError) as ei:
        Settings()
    msg = str(ei.value)
    assert "MS_UPSTREAM_VENDOR" in msg and "红线5" in msg, msg


def test_a1_4_app_creation_fails_not_only_settings(ms_env):
    """拒启动必须落在**建 app** 的路径上（`uvicorn msvc.main:app` 走的就是这条）。"""
    ms_env(MS_UPSTREAM="litellm", MS_UPSTREAM_VENDOR="anthropic")
    with pytest.raises(ValidationError):
        create_app()


def test_a1_4_declared_vendor_outside_deny_starts(ms_env):
    """正向：供应商名不在 deny 列表 ⇒ 正常构建（OpenAI **兼容协议** ≠ OpenAI **供应商**，
    这正是拆两个字段的全部意义）。"""
    ms_env(MS_UPSTREAM="litellm", MS_UPSTREAM_VENDOR="xiaomimimo",
           MS_LITELLM_API_BASE="https://api.xiaomimimo.com/v1")
    s = Settings()
    assert (s.upstream_vendor, s.litellm_provider) == ("xiaomimimo", "openai")
    assert create_app().state.upstream.name == "litellm"


def test_a1_4_undeclared_vendor_warns_but_starts(ms_env, caplog):
    """vendor 未声明 ⇒ WARNING 不 fail-fast（升级窗口里旧部署没这个 env）。"""
    ms_env(MS_UPSTREAM="litellm", MS_UPSTREAM_VENDOR="")
    with caplog.at_level("WARNING", logger="msvc.config"):
        s = Settings()
    assert s.upstream_vendor == ""
    assert any("MS_UPSTREAM_VENDOR" in r.message for r in caplog.records), caplog.text


def test_a1_4_reconciliation_skipped_when_upstream_is_fake(ms_env):
    """假上游不对应任何真实出口 ⇒ 不该因为 vendor=openai 就起不来（否则测试/本地全炸）。"""
    ms_env(MS_UPSTREAM="fake", MS_UPSTREAM_VENDOR="openai")
    assert Settings().upstream_vendor == "openai"
    assert create_app().state.upstream.name == "fake"


# ================================================================ 项5：provider 归因
async def test_a1_5_xms_provider_is_declared_vendor(monkeypatch, ms_env):
    """项5（B 2.3 🔴 静默错账）：`provider` 取 `MS_UPSTREAM_VENDOR`，**不是** model 前缀。

    该字段经 `remote.py` → `ledger.py` 进 `usage_logs.provider` 与供应商统计，
    写错了没有任何报错——所以必须有用例钉住。
    """
    ms_env(MS_UPSTREAM="litellm", MS_UPSTREAM_VENDOR="xiaomimimo")
    _RecordingLiteLLM().install(monkeypatch)
    events = await _collect(LiteLLMProvider().stream(_body(), Scenario()))
    xms = [e for e in events if e["kind"] == "terminate"][0]["x_model_service"]
    assert xms["provider"] == "xiaomimimo"
    assert xms["provider"] != "deepseek", "回落前缀 = 静默错账（§26.6-丙 第5项未落地）"
    assert xms["model_requested"] == "mimo-v2.6-flash"


async def test_a1_5_xms_provider_for_nonstream_too(monkeypatch, ms_env):
    ms_env(MS_UPSTREAM="litellm", MS_UPSTREAM_VENDOR="xiaomimimo")
    _RecordingLiteLLM().install(monkeypatch)
    res = await LiteLLMProvider().complete(_body(), Scenario())
    assert res.x_model_service["provider"] == "xiaomimimo"


async def test_a1_5_falls_back_to_prefix_when_vendor_undeclared(monkeypatch, ms_env):
    """未声明 vendor 才走旧回落（前缀推断）——把「旧行为 = 错账来源」钉在明面上。"""
    ms_env(MS_UPSTREAM="litellm", MS_UPSTREAM_VENDOR="")
    _RecordingLiteLLM().install(monkeypatch)
    events = await _collect(LiteLLMProvider().stream(_body(), Scenario()))
    xms = [e for e in events if e["kind"] == "terminate"][0]["x_model_service"]
    assert xms["provider"] == "deepseek", "未声明时回落 litellm_default_model 前缀（旧行为）"


async def test_a1_5_prefixed_model_still_uses_declared_vendor(monkeypatch, ms_env):
    """前缀是**协议路由语法**，不等于供应商：声明了 vendor 就以 vendor 为准。"""
    ms_env(MS_UPSTREAM="litellm", MS_UPSTREAM_VENDOR="xiaomimimo")
    _RecordingLiteLLM(model="anthropic/claude-3").install(monkeypatch)
    events = await _collect(LiteLLMProvider().stream(_body(model="anthropic/claude-3"), Scenario()))
    xms = [e for e in events if e["kind"] == "terminate"][0]["x_model_service"]
    assert xms["provider"] == "xiaomimimo", (
        "归因跟着 model 前缀走 = 换端点不改声明时账本继续记旧供应商（命中 deny 的名字会在启动期被拒）"
    )


# ================================================================ 乙：后果链的可复跑判据
async def test_b_consequence_first_token_timeout_enters_window_and_replays_504(ms_env):
    """§26.6-乙 判据（离线复现，不烧真网络）：首 token 超时**入窗** ⇒ 同 key 重发仍 504。

    B 实测 `should_cache(504, retryable=False, reached_upstream=True) is True`（对照 429→False），
    配合兼容面的 10 分钟桶指纹键 ⇒ **一次**首 token 超时会让同指纹重发在窗口内一律回放 504，
    即使上游 1 秒后已恢复（§16.2 同型：瞬时故障被放大成窗口级）。

    用「上游只被调用过 1 次」证明第二次是**回放**而不是又失败一次——只有回放才意味着
    「恢复了也被堵」，那才是这条后果链要说的事。
    """
    from msvc.idempotency import IdempotencyWindow

    w = IdempotencyWindow(600)
    assert w.should_cache(504, False, True) is True, "结果不明必须入窗（防双烧）"
    assert w.should_cache(429, True, True) is False, "瞬时拒绝不入窗（对照项）"

    ms_env(MS_UPSTREAM_FIRST_TOKEN_TIMEOUT="0.5", MS_CATALOG_MODELS="mimo-v2.6-flash")
    app = create_app()
    app.state.scenario = Scenario(first_token_seconds=5.0)  # 首字节一定赶不上 0.5s 预算
    up = app.state.upstream
    up.reset()
    app.state.window.clear()
    h = {"Idempotency-Key": "idem-first-token-timeout"}
    async with _client(app) as c:
        r1 = await c.post("/v1/chat/completions", json=_body(stream=True), headers=h)
        assert (r1.status_code, r1.json()["error"]["code"]) == (504, "upstream_timeout"), r1.text
        started_after_first = up.started
        assert started_after_first == 1
        r2 = await c.post("/v1/chat/completions", json=_body(stream=True), headers=h)
    assert r2.status_code == 504, f"窗口内重发必须仍 504（后果链）：{r2.status_code} {r2.text}"
    assert r2.status_code != 409, "在飞位已释放，第二次走的是回放而不是并发拒绝"
    assert up.started == started_after_first, "第二次必须是**回放**（没再调上游）"
