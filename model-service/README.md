# 模型服务（Model Service, S3）

独立、无状态的模型调用服务。契约 = `../docs/model-service-protocol.md` v1；平台经
`LLM_BACKEND=remote` 的 `RemoteBackend` 调用它。

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
- 取消传播（红线6）：客户端断连 → 上游流式调用在 ≤2s 内被中止（`msvc/main.py:_handle_stream`）。
  断连时**终态未知**，故不入窗、只清在飞位（§6.5：由平台按 `abandoned` 对账收编）。
- 超时自持（红线8）：首 token 15s / 总 60s，均 < 平台流式空闲上限 90s。

## Provider
- `FakeUpstream`（默认，`MS_UPSTREAM=fake`）：离线确定性，测试/本地。
- `LiteLLMProvider`（`MS_UPSTREAM=litellm`）：真实上游，lazy import。

## 配置（env，前缀 `MS_`）
`MS_SERVICE_TOKENS`（逗号分隔，双 token）、`MS_UPSTREAM`、`MS_COMPLIANCE_DENY_PROVIDERS`、
`MS_CATALOG_MODELS`、`MS_UPSTREAM_FIRST_TOKEN_TIMEOUT`、`MS_UPSTREAM_TOTAL_TIMEOUT`、
`MS_IDEMPOTENCY_WINDOW_SECONDS`、`MS_IDEMPOTENCY_CACHE_MAX_ENTRIES`、
`MS_IDEMPOTENCY_CACHE_MAX_CHARS`。

## 流式错误语义（§6.7，v1.7）
- **首字节之前**的失败（尚未写出任何事件）→ **HTTP 错误状态**（同 §7 表：
  429/504/502…，`Retry-After`/`error.code` 正常），**不得**发成「200 + 流内错误帧」。
- **首字节之后**（部分内容已产出）→ 流内错误帧 `data: {"error": { …§7 错误体… }}`
  收尾，**随后仍发 `data: [DONE]`**（G3-1），错误帧**可**携带实测部分 `usage`
  （G3-2，加法字段，平台优先采信）。已产出的部分内容按 §8.1.1 口径结算。

## 运行 / 测试
```bash
uvicorn msvc.main:app --host 0.0.0.0 --port 8000
pytest            # 对真实服务跑 T1–T11（test_contract.py）
```
