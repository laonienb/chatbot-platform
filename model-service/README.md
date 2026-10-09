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
- 取消传播（红线6）：客户端断连 → 上游流式调用在 ≤2s 内被中止（`msvc/main.py:_handle_stream`）。
- 超时自持（红线8）：首 token 15s / 总 60s，均 < 平台流式空闲上限 90s。

## Provider
- `FakeUpstream`（默认，`MS_UPSTREAM=fake`）：离线确定性，测试/本地。
- `LiteLLMProvider`（`MS_UPSTREAM=litellm`）：真实上游，lazy import。

## 配置（env，前缀 `MS_`）
`MS_SERVICE_TOKENS`（逗号分隔，双 token）、`MS_UPSTREAM`、`MS_COMPLIANCE_DENY_PROVIDERS`、
`MS_CATALOG_MODELS`、`MS_UPSTREAM_FIRST_TOKEN_TIMEOUT`、`MS_UPSTREAM_TOTAL_TIMEOUT`、
`MS_IDEMPOTENCY_WINDOW_SECONDS`。

## 运行 / 测试
```bash
uvicorn msvc.main:app --host 0.0.0.0 --port 8000
pytest            # 对真实服务跑 T1–T11（test_contract.py）
```
