# 模型服务（Model Service, S3）

独立、无状态的模型调用服务。契约 = `../docs/model-service-protocol.md` **v1.7-澄清3**
（线协议仍为 v1）；平台经 `LLM_BACKEND=remote` 的 `RemoteBackend` 调用它。

## 边界铁律
- **永不认识用户**：请求出现 `user_id` / `conversation_id` / `persona` 等禁字段 → 400
  `invalid_request`（`msvc/guards.py`，协议 §1 红线1 / 工作单 P0-3）。
- `persona:<slug>` 前缀 → 400（红线10）。
- 合规硬策略优先于 `routing`（红线5，`msvc/main.py:_check_compliance`）。
- `cost_usd` 不可得 → `null` + `cost_status:"unknown"`，**绝不返回 0**（红线2）。

## 端点（§2）
| 端点 | 用途 |
|---|---|
| `POST /v1/chat/completions` | 对话（流式 / 非流式，OpenAI 兼容 + `x_model_service` 扩展） |
| `GET /v1/models` | 模型目录 |
| `GET /v1/capabilities` | 能力声明 + 协议版本 + **超时值**（供平台启动断言内层<外层，§8.1） |
| `GET /healthz` `/health/liveness` `/health/readiness` | 存活 / 就绪 |

## 关键实现点
- 服务间鉴权：静态 Bearer，接受新旧双 token 轮转（`msvc/auth.py`，§3）。
- 幂等窗口：`Idempotency-Key` ≥10min；入窗按 §8.2.1 三分表 + `retryable` 开关
  （`msvc/idempotency.py`）——瞬时拒绝不入窗、结果不明必入窗防双烧。
  **窗口对「流式 / 非流式」同等适用**（§8.2 告示 + §8.2.2，v1.7）：流式成功、以及
  「已发起上游调用后」的流内错误帧终止同样入窗；命中时按**终态重放**重新生成一条 SSE 流
  （内容文本/usage/`finish_reason`/终止事件/`[DONE]` 与首次一致，**chunk 边界允许不同**）。
  同 key 并发（含流式）→ 在飞位 409。
  - **回放形态**：缓存的是**终态**（一段文本 + 一份 usage），不是事件序列——后者会随
    生成长度无界增长。故不保证逐事件原样重放。
  - **缓存上限与淘汰语义（显式声明，§8.2.2-3）**：`MS_IDEMPOTENCY_CACHE_MAX_ENTRIES`
    （默认 256 条）与 `MS_IDEMPOTENCY_CACHE_MAX_CHARS`（默认 1e6 字符）。超限时按**插入序
    淘汰最旧条目**并打 WARNING 日志；**被淘汰的 key 后续重发按「未命中」处理**（会真实
    调用上游）——本项即协议要求"不得在淘汰后静默重调上游"的显式声明；若要更保守的行为，
    请调大上限而不是依赖淘汰。
  - **容量估算（H3，部署时按此调参）**：窗口要装的是"窗长之内仍可能被同 key 重发"的请求，
    最坏情形上界取 `max_entries ≥ 峰值RPS × MS_IDEMPOTENCY_WINDOW_SECONDS`（默认 600s），
    实际可按观测到的重发率折减（兼容面的指纹键带 10 分钟桶，只有网络抖动重发才会命中同 key）。
    体积上限与之配套：`max_chars ≈ max_entries × 单条终态平均字符数`——默认 256 条配 1e6 字符
    ≈ 每条 3900 字符，短回复够用、长回复会先撞**字符**上限。两个上限谁先到就按插入序淘汰。
    ⚠️ 运维信号：淘汰的 WARNING 日志**正常应为零**，持续增长即说明容量算小了
    （每一次淘汰都可能换来一次真实重调上游，即二次计费）。
  - **幂等窗容量定值（DW-6，生产切换清单 ⑥）**：上面的公式落到具体数字。
    **峰值 RPS 假设及其依据**（这是**假设**，不是实测——本仓库没有负载实测数据）：平台自身限流
    `settings.rpm_default = 120` / 60s 窗口（`server/app/billing/ratelimit.py:63-72`），
    即**默认每个 API Key / 每个用户至多 2 RPS**。⚠️ 这个 2 是**默认值不是硬上限**：同一处代码取的是
    `api_key.rpm_limit or settings.rpm_default`，管理员可逐 key 抬高——只要有一个 key 抬过 120/min，
    下面的档位就得按实际值重算，别拿这张表当免检结论。进入本服务的请求只可能来自平台，于是
    `峰值RPS ≈ 活跃身份数 × 每身份 RPS`。取「5 个持续活跃身份 × 默认 2 RPS」为部署基线 ⇒ **峰值 10 RPS**。

    | 假设活跃身份数 | 峰值 RPS | `max_entries`（= RPS × 600s） | `max_chars`（= entries × 1500） | 终态字符上限 ≈ |
    |---|---|---|---|---|
    | 1（单人自测） | 2 | 1 200 | 1 800 000 | 1.8 M |
    | **5（部署基线）** | **10** | **6 000** | **9 000 000** | **9 M** |
    | 25（多人群聊高峰） | 50 | 30 000 | 45 000 000 | 45 M |

    算例（基线档）：`600s × 10 RPS = 6 000` 条；每条终态 ≈ 1 500 字符（一次典型回复的内容文本
    + 一份 usage + `x_model_service` 块，按 `msvc/idempotency.py:_size()` 的口径计），
    ⇒ `max_chars = 6 000 × 1 500 = 9 000 000`。
    **落点**：`deploy/docker-compose.yml` 的 `MS_IDEMPOTENCY_CACHE_MAX_ENTRIES=6000` /
    `..._MAX_CHARS=9000000`，模板同值见 `deploy/.env.example`；代码内默认仍是保守的
    256 / 1e6（`msvc/config.py`），即**不注入 env 就是单人自测量级**，别看代码默认值以为已定过值。
    **内存代价要一起算**（否则"容量够"变成"OOM 够"）：`_size()` 计的是**字符数**，而 CPython
    存中英混排字符串按 4 字节/字符，故 9e6 字符 ≈ **36 MB** 上界（45 M 字符 ≈ 180 MB，
    多人档就该考虑独立进程数或调小窗长）。
    **两条判据**：① 换档时 `max_entries` 与 `max_chars` **成对改**，只改一个就是让另一个先触发；
    ② 改完看淘汰 WARNING——**正常运行应为零**，持续增长即容量算小了，而每一次淘汰都可能换来
    一次真实重调上游（二次计费），所以宁可调大上限，也不要靠淘汰兜底。
- 取消传播（红线6）：客户端断连 → 上游流式调用在 ≤2s 内被中止（`msvc/main.py:_handle_stream`）。
  断连时**终态未知**，故不入窗、只清在飞位（§6.5：由平台按 `abandoned` 对账收编）。
- 超时自持（红线8）：首 token 15s / 中途空闲 30s（O1，`MS_UPSTREAM_CHUNK_IDLE_TIMEOUT`）/
  总 60s（仅约束非流式，v1.7-澄清2），三者均 < 平台流式空闲上限 90s，并在 `/v1/capabilities`
  的 `features.timeouts` 暴露实际值供平台启动断言「内层 < 外层」。

## Provider
- `FakeUpstream`（默认，`MS_UPSTREAM=fake`）：离线确定性，供本地与契约测试。
- `LiteLLMProvider`（`MS_UPSTREAM=litellm`）：真实上游，lazy import。
  **流式已实现**（工作单 SW-1）——此前 `stream()` 抛 `NotImplementedError`，而 native 模式
  流式是主路径，`LLM_BACKEND=remote` 切换后所有流式请求会裸 500（协议 §24.1 R1 实测）。
  现逐块转发内容 → 收尾 `finish_reason`（§6.6，位于 usage chunk **之前**）→ usage →
  `x_model_service` 终止事件 → done；成本走 `litellm.cost_per_token`，算不出即
  `null + unknown`（红线2）。所有上游异常统一经 `errors.from_upstream_exception` 映射为
  带 §7 `code` 的 `ServiceError`（SW-4），不再裸抛。
  **联网冒烟已跑过一次**（2026-10-11，A-1/协议 §26.6-乙，端点为 OpenAI 兼容网关的 reasoning
  模型，12 次真调用）：流式 5/5 成功、`finish_reason=stop`、`[DONE]` 齐全，
  `usage.completion_tokens_details.reasoning_tokens` 每次都带（23–194），
  成本 `cost_status=unknown` / `cost_usd=null`（litellm 价表无此 model，红线2 的**正确**行为，
  平台按 §5.1 回落自己的价格表）。**一条运维必读**：上游首 *content* token 实测
  **3.9–11.3s**（首 reasoning token 0.5–9.7s），而 `MS_UPSTREAM_FIRST_TOKEN_TIMEOUT=15` 量的是
  **首个对外事件**——reasoning chunk 不产出事件，故这 15s 实为「整段推理的预算」，最坏余量仅 1.3×。
  超时后按 §8.2.1 **入窗**（同 Idempotency-Key 在窗口内重发一律回放 504，即使上游已恢复），
  判据用例：`tests/test_a1_upstream_identity.py::test_b_consequence_first_token_timeout_enters_window_and_replays_504`。
  离线防线仍保留：以**注入的假 `litellm` 模块**为边界跑 `LiteLLMProvider` 本体
  （`tests/test_real_upstream.py`、`tests/test_a1_upstream_identity.py`），不依赖网络。
  ⚠️ 进程内 `httpx.ASGITransport` 会**攒完整个响应体**才返回，量不到 chunk 间时序——
  流式计时必须以真 uvicorn 为准（本仓库的 T6 取消用例早就为此用真连接）。

## 配置（env，前缀 `MS_`）
`MS_SERVICE_TOKENS`（逗号分隔，双 token）、`MS_UPSTREAM`、`MS_COMPLIANCE_DENY_PROVIDERS`、
`MS_CATALOG_MODELS`、`MS_UPSTREAM_FIRST_TOKEN_TIMEOUT`、`MS_UPSTREAM_CHUNK_IDLE_TIMEOUT`、
`MS_UPSTREAM_TOTAL_TIMEOUT`、`MS_IDEMPOTENCY_WINDOW_SECONDS`、
`MS_IDEMPOTENCY_CACHE_MAX_ENTRIES`、`MS_IDEMPOTENCY_CACHE_MAX_CHARS`、
`MS_IDEMPOTENCY_INFLIGHT_TTL_SECONDS`（SW-5 在飞位兜底，默认对齐上游总超时 60s）、
`MS_LITELLM_API_BASE` / `MS_LITELLM_API_KEY` / `MS_LITELLM_DEFAULT_MODEL`（真实上游）、
`MS_LITELLM_PROVIDER`（**协议名**，默认 `openai`）/ `MS_UPSTREAM_VENDOR`（**供应商名**，默认空）。

### 真实上游的两个名字：协议名 ≠ 供应商名（A-1，协议 §26.6-丙 五项）

「OpenAI 兼容」是**协议**，「小蜜/DeepSeek/…」是**供应商**。此前两者挤在 model 字符串的
前缀里，于是只有两条坏路：发 `openai/xxx` 会撞自家红线5 的 deny 前缀判定（403），发无前缀
model 则 litellm 定不出 provider、归因字段回落到 `MS_LITELLM_DEFAULT_MODEL` 的前缀。拆成
两个显式字段后：

| 落点 | 字段/env | 行为 |
|---|---|---|
| 1 | `provider._kwargs` | model **无斜杠**时传 `custom_llm_provider=MS_LITELLM_PROVIDER`；有斜杠时**不传**（litellm 在两者不一致时会再拼一层前缀，把 model id 弄错） |
| 2 | `MS_LITELLM_PROVIDER` | 协议名，默认 `openai` |
| 3 | `MS_CATALOG_MODELS` | **必须含目标 model id**：无前缀 model 走目录校验（带 `/` 才免检），漏配 ⇒ 到达上游前就 404 `model_not_found`，报错看着像上游问题 |
| 4 | `MS_UPSTREAM_VENDOR` | 供应商名。`MS_UPSTREAM=litellm` 且该名字命中 `MS_COMPLIANCE_DENY_PROVIDERS` ⇒ **拒绝启动**；未声明 ⇒ 只打 WARNING |
| 5 | `provider._xms` | `x_model_service.provider` 取 `MS_UPSTREAM_VENDOR`（该字段经平台进 `usage_logs.provider` 与供应商统计，回落前缀＝静默错账） |

第 4 项为什么必须有：A-1 之后请求里的 model 不带前缀 ⇒ `_check_compliance` 的「前缀 ∈ deny」
那条臂对真实流量恒不命中，而真实出口由 `MS_LITELLM_API_BASE` 决定、合规检查从不读它。
**实测过的失明判据**：`MS_LITELLM_API_BASE=https://api.openai.com/v1` + `MS_UPSTREAM_VENDOR=xiaomimimo`
+ 无前缀 model → 服务照常启动、`/v1/models` 正常（2026-10-11 本机跑过，未向该端点发任何 chat 请求）。
⇒ vendor 是**声明**不是**验证**：它把「真实出口是谁」变成一条启动期会对账、日志会警告的显式配置，
但要真正封住「声明与实际不一致」仍需按 `api_base` 主机名判定，那是协议决策，未在本轮实现。

冒烟用的临时 env（**不落 git**，凭证不入库）：
```bash
MS_UPSTREAM=litellm MS_LITELLM_API_BASE=<端点> MS_LITELLM_API_KEY=<一次性调试 key> \
MS_LITELLM_PROVIDER=openai MS_UPSTREAM_VENDOR=<真实供应商名> \
MS_CATALOG_MODELS="deepseek-chat,qwen-plus,<目标model>" \
LITELLM_LOCAL_MODEL_COST_MAP=True uvicorn msvc.main:app --port 8000
```
`LITELLM_LOCAL_MODEL_COST_MAP=True` 必须带：本服务不读平台的 env，缺它时 litellm 会先联网拉
远端价表（失败重试 3 次），那段耗时直接叠在首 token 预算上。

## 排障（协议 §27.4 R2 的执行面，2026-10-12）

- **`404 model_not_found` ＝ 本服务的目录缺口，不是上游。** 无前缀 model 不在
  `MS_CATALOG_MODELS` 里就会被入站关口（`main.py` 的目录校验）挡掉，请求**根本没到达 provider**，
  所以"上游挂了"这个判断在这里是错的。口诀：**404 = 目录缺口；403 只会来自合规 deny
  （显式发 `openai/…` 这类前缀时；A-1 之后无前缀 model 不命中）；到达上游之后的失败才带 §7
  的上游 `code`。**
  这条**不进启动自检**：自检只能对账启动期已知量（vendor ∈ deny、超时分层），而"平台会发哪个
  model"是运行时决定的，服务侧启动时无从自检——所以它是文档/口诀的活，不是断言的活。
- **目标 model 的启用位只有一个：本服务的 `MS_CATALOG_MODELS`。** 管理端把某个模型 enable 管的是
  "平台选哪个模型"，**不改变**本服务的目录校验 ⇒ 两侧必须成对：目录含该 id，**且**管理端把对应行
  enable（或让平台 `llm_default_model` 指向目录内某个 id）。只配一边就会复现
  "目录同步插入的行 `enabled=0` ⇒ 平台回落 `gpt-4o-mini` ⇒ 开流即 404"。
- **供应商名命中 deny ⇒ 拒绝启动，这是期望行为（A1-4），别为了让服务起来去删
  `MS_COMPLIANCE_DENY_PROVIDERS` 里的项。** 那是运营决策、留用户（协议 §26.6 丙项）。
  冒烟/示例里的 `xiaomimimo` 不命中默认 deny，可直接用；若目标供应商恰好叫 `openai`/`anthropic`
  而又确实要接它，走的是**改策略**这条路，不是在本服务配置里静默放宽（红线5）。
- **一次性冒烟的 model 不进模板主值，只作注释示例**（`mimo-v2.6-flash` 即此类）：它绑的是
  一次性调试凭证、凭证不入库，写成默认目录等于暗示"生产就用它"，照抄的人没有 key、目录里却有它，
  更迷惑。完整示例见上面那个冒烟 bash 块。

## 流式错误语义（§6.7，v1.7）
- **首字节之前**的失败（尚未写出任何事件）→ **HTTP 错误状态**（同 §7 表：
  429/504/502…，`Retry-After`/`error.code` 正常），**不得**发成「200 + 流内错误帧」。
- **首字节之后**（部分内容已产出）→ 流内错误帧 `data: {"error": { …§7 错误体… }}`
  收尾，**随后仍发 `data: [DONE]`**（G3-1），错误帧**可**携带实测部分 `usage`
  （G3-2，加法字段，平台优先采信）。已产出的部分内容按 §8.1.1 口径结算。

## 运行 / 测试
```bash
uvicorn msvc.main:app --host 0.0.0.0 --port 8000
pytest            # 服务侧契约测试
```
- `tests/test_contract.py`：T1–T11（含 P0-3 入站禁字段、§8.2.1 入窗边界）。
- `tests/test_real_upstream.py`：**T12**（①`test_t12_pre_first_byte_error_returns_http_status`
  / ②③`test_t12_post_first_byte_error_frame_then_done`）+ 真实 provider（SW-1）与
  SW-2/4/5 防线。⚠️ §10 要求 T12「两侧各配用例」：平台侧在
  `server/tests/test_model_service_contract.py`，服务侧此前**缺**、由本文件补齐。
