# 模型服务协议契约（Model Service Protocol）

| 项 | 值 |
|---|---|
| 版本 | **v1**（协议版本 `X-Model-Service-Version: 1`） |
| 状态 | 正式契约（S2 产物）—— 被平台 `RemoteBackend` 与模型服务实现**同时消费** |
| 作者 | 架构师 agent（2026-10-09） |
| 上游决策 | `docs/model-service-design.md` §9.2 定案：Q1=B(优先级链) / Q2=A+B可选链 / Q3=C(Proxy起步→自研)；Q4–Q9 采纳 §9.1 立场 |
| 变更规则 | 破坏性变更须升协议版本号并走 `AGENTS.md`「三」；扩展字段加法不升版本（客户端宽松解析） |

> 本文是**可执行契约**：S3（模型服务实现）与 S4（平台 RemoteBackend）以本文验收，
> 任何与本文不符的实现都是 bug。设计动机与理由见上游文档，本文只写「必须是什么」。

---

## 1. 边界铁律（继承设计红线，验收时逐条检验）

1. 模型服务**不得**认识 `user_id` / `conversation_id` / `persona` —— 任何接口出现即越界。
2. `cost_usd` 不可得时返回 `null` + `cost_status: "unknown"`，**绝不返回 0**。
3. 响应必须如实返回 `model_used` + `provider`，供平台账本归因。
4. 模型服务**无状态**：可随时重启、可多副本、不持有会话/记忆/账本。
5. 合规策略优先于调用方 `routing` 请求，调用方**不可放宽**。
6. 平台断开连接时，模型服务**必须中止上游请求**。
7. 协议版本不兼容时平台**启动即失败**（过渡模式的例外见 §8）。
8. 超时预算分层：模型服务内部上游超时 < 平台总超时。
9. 平台 mock 后端**不得**作为生产降级路径。
10. `persona:` 前缀的请求，模型服务**显式 400 拒绝**（Q7-B）。

---

## 2. 端点总览

| 端点 | 方法 | 用途 | Proxy 过渡期 | 自研后 |
|---|---|---|---|---|
| `/v1/chat/completions` | POST | 对话（流式/非流式） | ✅ 有 | ✅ 有 |
| `/v1/models` | GET | 模型目录发现 | ✅ 有 | ✅ 有 |
| `/v1/capabilities` | GET | 能力声明 + 协议版本 | ❌ 无 | ✅ 必须 |
| `/healthz` | GET | 存活/就绪（区分 liveness/readiness） | ⚠️ LiteLLM Proxy 提供 `/health/liveness`、`/health/readiness`，平台按 §8 映射 | ✅ 必须 |

---

## 3. 服务间鉴权（补 §9.1-3 缺口）

| 项 | 规定 |
|---|---|
| 形态 | **静态 Bearer token**（`Authorization: Bearer <MODEL_SERVICE_TOKEN>`），v1 不引入 mTLS |
| 生成/存放 | 部署时生成（≥32 字节随机），经 env/密钥管理注入双方；**不落库、不进 git** |
| 网络边界 | 模型服务**仅集群内网可达，不暴露公网**；token 是纵深防御的第二层，不是唯一屏障 |
| 失败语义 | 401 + `code: "service_auth_failed"`（区别于上游凭证失效的 `upstream_auth_failed`） |
| 轮转 | 双 token 并行窗口（新旧同认）后吊销旧值；协议不规定机制，部署侧保证 |
| 演进 | mTLS / 短期凭证列为 v2 候选，v1 明确不做 |

---

## 4. 请求契约

### 4.1 Headers

| Header | 必填 | 说明 |
|---|---|---|
| `Authorization` | ✅ | §3 的服务间 token |
| `X-Request-Id` | ✅ | uuid，平台生成、贯穿两层日志，排障主键 |
| `Idempotency-Key` | ⚪ | 语义 = **窗口内去重**（窗口 ≥10 分钟，与平台现状对齐）；模型服务在窗口内对同 key 返回首次结果或 409，**不得**二次烧上游 |
| `X-Model-Service-Version` | ✅ | 客户端声明的协议版本（当前恒为 `1`） |

### 4.2 Body

```jsonc
{
  "model": "deepseek-chat",        // 真实模型名。"persona:" 前缀 → 400 invalid_request（红线10）
  "messages": [ { "role": "user", "content": "..." } ],
  "stream": true,
  "stream_options": { "include_usage": true },   // stream=true 时平台必带
  "temperature": 0.8, "top_p": 1.0, "max_tokens": 2048,

  "routing": {                      // 扩展命名空间；模型服务可忽略，但须在 capabilities 声明
    "allow_providers": ["deepseek", "qwen"],
    "deny_providers": ["openai", "anthropic"],
    "fallback": true,               // 是否允许降级到备用供应商/模型
    "max_cost_usd": "0.05"          // 单请求成本熔断上限（十进制字符串）
  },
  "trace": { "tenant": "t-123" }    // 仅日志路由。**必须是部署级不透明标签**（如环境/租户池名），
                                     // 不得由 user_id/conversation 派生 —— 派生即换名传身份，违反红线 1
}
```

**禁止字段**（出现即 400 `invalid_request`）：`user_id`、`conversation_id`、`persona`、
任何形如平台业务身份的字段。人格与记忆已融进 `messages` 内容，模型服务不理解其语义。

**宽松解析**：双方对未知扩展字段一律忽略不报错（forward-compatible）；仅对本文标注
「必填」的字段严格。

### 4.3 合规否决（Q8-B）

模型服务持有合规硬策略（如「禁止境外 provider」）。`routing` 是调用方**意图**，
合规策略是**上限**：请求违反硬策略时返回
`403 + code: "compliance_denied"`，`error.message` 说明被否决的约束。
`routing.allow_providers` 放宽不了合规策略（红线 5）。

---

## 5. 响应契约（非流式）

```jsonc
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "model": "deepseek/deepseek-chat",   // ★ 实际调用的模型串（model_used），非请求别名
  "choices": [ { "index": 0,
                 "message": { "role": "assistant", "content": "..." },
                 "finish_reason": "stop" } ],
  "usage": {
    "prompt_tokens": 120,               // 必须实测；无法实测见 §7 metering 规则
    "completion_tokens": 80,
    "total_tokens": 200,
    "prompt_tokens_details": { "cached_tokens": 64 },      // 有则必填 → 平台 cache_read_tokens
    "completion_tokens_details": { "reasoning_tokens": 0 } // 有则必填 → 平台 reasoning_tokens
  },

  "x_model_service": {                  // ★ 扩展命名空间（自研后必须；Proxy 期缺失，见 §8）
    "cost_usd": "0.000123",             // 十进制字符串；不可得时 null，绝不 0（红线2）
    "cost_status": "exact",             // exact | estimated | unknown
    "provider": "deepseek",             // 实际供应商 → 平台 usage_logs.provider
    "model_requested": "deepseek-chat",
    "model_used": "deepseek/deepseek-chat",   // → 平台 usage_logs.model_upstream
    "fallback_used": false,
    "upstream_latency_ms": 812,
    "cached": false                     // 模型服务侧缓存命中
  }
}
```

### 5.1 平台侧消费规则（Q1-B 优先级链，账本落库口径）

| `x_model_service` 状态 | 平台行为 |
|---|---|
| `cost_status: "exact"` | 采信 `cost_usd` 为上游成本，`metering_source=provider` |
| `cost_status: "estimated"` | 采信但账本 `needs_review=true` |
| `cost_status: "unknown"` / `cost_usd: null` / **字段整体缺失**（Proxy 期） | 回落平台价格表推算；价格表也查不到 → `(0, needs_review=true)` 进对账清单。**绝不把 unknown 当免费。** |

token 计量沿用现有 `metering_source` 三档（provider / tiktoken / estimated）与
`needs_review` 联动，不改语义。

**两条独立路径，不得混淆（对 §9.1 评审洞 #2/#3 的契约级封堵）**：

- `cost_usd` **只替换 upstream 一侧**（毛利监控的 `cost_upstream` 来源）。用户扣费
  （billed，积分）**始终走平台 `rating_rules` 费率快照**，与 `cost_usd` 无关 ——
  照「pricing.py 退化为读 cost_usd」的字面实现会把用户按上游成本价扣积分、废掉
  零售与毛利模型，**禁止**。
- `metering_source` 记的是 **token 计量来源**，不是成本来源。平台账本须新增
  `cost_source` 列（`service_exact | service_estimated | price_table | none`，
  S6 实施），否则分不清「Proxy 过渡期本来没有」与「provider 漏报」。

---

## 6. 流式契约（SSE）

1. 逐块 `data: {OpenAI chunk}\n\n`，正常结束以 `data: [DONE]` 收尾。
2. `stream_options.include_usage=true` 时，**`[DONE]` 前必须有一个携带完整 `usage`
   的 chunk**（choices 为空数组）。平台现有 `stream_chunk_builder` 路径据此拼回实测 usage。
3. **终止事件（Q4-B，自研后必须；Proxy 期无）**：usage chunk 之后、`[DONE]` 之前，
   发一个独立命名事件承载扩展字段：

   ```
   event: model_service
   data: {"x_model_service": { ...同 §5... }}
   ```

   不污染 OpenAI chunk 结构；不认识该事件的客户端自然忽略。
4. **心跳**：允许 `: keepalive` 注释行（推理模型长思考防中间层断连）。
   平台侧**必须忽略**一切注释行与未知命名事件（宽松解析）。
5. **取消传播（红线 6）**：平台断开 TCP → 模型服务必须立即中止上游请求。
   验收测试见 §10-T6。中止后已产生的 token 如何上报：连接已断，无法回报——
   由平台侧按现有 `abandoned` 对账路径收编（预扣估算），模型服务侧只记日志。

---

## 7. 错误契约

统一 OpenAI 错误体，**`code` 机器可判是唯一分派依据**（内部协议不依赖 header 有无）：

```json
{ "error": { "message": "...", "type": "...", "code": "rate_limited", "param": null } }
```

| HTTP | `code` | 平台动作 | 备注 |
|---|---|---|---|
| 400 | `invalid_request` | 不重试，报错 | 含红线 10 的 `persona:` 前缀拒绝 |
| 401 | `service_auth_failed` | 不重试，**运维告警** | 服务间 token 错（§3） |
| 401 | `upstream_auth_failed` | 不重试，**运维告警** | 模型服务侧 provider 凭证失效 |
| 403 | `compliance_denied` | 不重试，提示不可用 | 合规否决（§4.3） |
| 404 | `model_not_found` | 不重试；触发目录重新同步（§9） | |
| 413 | `context_length_exceeded` | 不重试；平台侧上下文窗口策略问题 | |
| 429 | `rate_limited` | **可退避重试**（有 `Retry-After` 则按其秒数） | 上游/模型服务限速，临时 |
| 429 | `budget_exhausted` | **不重试**；可选降级到便宜模型或拒绝用户 | 成本熔断/预算耗尽，窗口内无解 |
| 500 | `internal_error` | 可重试 1 次 | |
| 502/503 | `upstream_error` / `service_unavailable` | 可重试/降级；连续失败触发平台熔断（§11） | |
| 504 | `upstream_timeout` | 可重试/降级 | 内部超时预算耗尽（§8.3） |

**平台对外契约的映射（Q5-B，加法演进）**：平台把上表翻译成对客户端的响应时，
`rate_limited` → 429 带 `Retry-After`；`budget_exhausted` → 429 不带 `Retry-After`
（兼容现有前端约定），**同时**在对外错误体新增 `error.code` 字段透传语义。
旧约定保留一个版本周期后废弃；该对外变更走 `AGENTS.md`「三」登记。

---

## 8. 超时 / 取消 / 幂等 / 过渡模式

### 8.1 超时预算分层（红线 8）

| 层 | 默认 | 约束 |
|---|---|---|
| 平台总超时 | 120s | 最外层 |
| 模型服务首字节超时 | 15s | 无首 token 即 504 `upstream_timeout` |
| 模型服务上游总超时 | 60s | **必须 < 平台总超时**，留结算与传输余量 |

### 8.2 幂等

`Idempotency-Key` = 窗口内去重（≥10 分钟）。平台现状（`chat.py`：显式 header
不加窗口 / 无 header 用 `api_key+model+消息体` 指纹带 10 分钟桶窗）在平台侧生成
key 后**必须**通过 header 传给模型服务，保证「平台重试 ≠ 上游双烧」。

### 8.3 过渡模式（Q3-C 的关键补丁：Proxy 期无 capabilities 与红线 7 的冲突）

平台配置 `MODEL_SERVICE_MODE = proxy | native`：

| 模式 | 启动自检 | 成本口径 | 终止事件 |
|---|---|---|---|
| `proxy`（LiteLLM Proxy 过渡期） | **跳过** `/v1/capabilities` 自检；改为探活 `/health/liveness` + 用固定模型名调 `/v1/models` 确认目录可达。启动日志**必须**打「过渡模式：无成本/能力契约」警告 | §5.1 第三行（价格表回落） | 无，平台忽略缺失 |
| `native`（自研后） | **必须**调 `/v1/capabilities`；协议版本不兼容 → **拒绝启动**（红线 7 全额生效） | §5.1 全链生效 | 必须消费 `event: model_service` |

`/v1/capabilities` 响应形状（native 模式必须）：

```json
{
  "protocol_version": 1,
  "features": {
    "stream_terminate_event": true,
    "cost_attribution": true,        // 能提供 x_model_service.cost_usd
    "routing_hints": true,           // 支持 routing.* 字段
    "compliance_policies": ["cn_only_providers"],
    "idempotency_window_seconds": 600
  },
  "implementation": { "name": "model-service", "version": "0.1.0" }
}
```

### 8.4 平台侧降级（Q2 定案）

- 生产默认：模型服务不可用（连接失败/熔断打开）→ 平台对客户端返回
  **503**，错误体 `{"detail": "模型服务暂不可用，请稍后再试"}`。
- 可选降级链：`MODEL_SERVICE_FALLBACK_DIRECT=<model>` 配置存在时，平台可直连该
  provider 完成请求（账本 `model_upstream` 标注 `direct:<model>`、
  `needs_review=true`）。**默认不配置**。
- mock 后端仅限开发环境（红线 9），配置层保证 `LLM_BACKEND=mock` 时拒绝以生产
  profile 启动。

---

## 9. 模型目录同步

- 平台定期（建议 5 分钟）+ 收到 404 `model_not_found` 时即时，拉取 `GET /v1/models`。
- 同步产物写入平台 `llm_models` 的**纯展示属性**（可用模型集合、上游 id）；
  展示名/排序/启用/白名单仍是平台业务字段，不被同步覆盖（Q6-A）。
- 同步后平台侧不再需要 `llm_models.api_base` / `api_key` —— 凭证迁移到模型服务，
  平台注册表两列废弃（迁移由 S4/S5 执行，属后端 agent 范围，此处只定契约：
  **同步不携带任何凭证**）。

---

## 10. 契约测试清单（S2 交付的一部分，S3/S4 按此验收）

双方各自跑同一份清单；平台侧用 **fake model-service 桩**（§10.2）跑，
模型服务侧对真实实现跑。

| # | 用例 | 验收 |
|---|---|---|
| T1 | 非流式基础 | 响应含 `model_used`/`provider`/实测 usage；`cost_usd` 为十进制字符串或 null |
| T2 | 成本不可得 | 返回 `null` + `cost_status:"unknown"`，**断言绝不为 `"0"` 或 `0`** |
| T3 | `persona:` 前缀 | 400 `invalid_request`（红线 10） |
| T4 | 流式完整链 | chunk 序列 → usage chunk → （native）`event: model_service` → `[DONE]`；keepalive 注释被平台忽略 |
| T5 | 429 双语义 | `rate_limited` 带 `Retry-After` 平台退避重试；`budget_exhausted` 平台不重试 |
| T6 | **取消传播** | 平台断开连接后，fake 桩记录的上游调用在 ≤2s 内被中止（红线 6；S3/S4 各配一条） |
| T7 | 幂等窗口 | 同 `Idempotency-Key` 重放，上游只被调一次 |
| T8 | 合规否决 | `routing.allow_providers` 含被禁 provider → 403 `compliance_denied`，且不可被请求放宽 |
| T9 | 版本自检 | native 模式 capabilities 版本不符 → 平台启动失败；proxy 模式跳过自检但打警告日志 |
| T10 | 目录同步 | `/v1/models` 变化 → 平台展示目录更新；404 `model_not_found` 触发即时重同步 |
| T11 | 超时分层 | 上游慢于首字节超时 → 504；平台总超时 > 模型服务上游超时（配置断言） |

### 10.1 平台侧改造对应（供 S4 验收，非本文实现）

`RemoteBackend` 实现 `MockBackend`/`LiteLLMBackend` 同签名接口（`chat` /
`chat_stream` → `LLMResult` / `StreamDone`），把 §5/§6 字段映射进现有 dataclass
（`model`→`model_used`、`provider`、`cost_usd`→计价优先级链、`metering_source`、
`needs_review`），`get_llm_backend()` 增加 `remote` 分支由 `LLM_BACKEND=remote` 选择。

### 10.2 fake model-service 桩

独立目录 `model-service/tests/fake/`（**不放进 `server/app/`**，避免互相 import）：
可编程响应（含错误注入、慢响应、断连模拟）的最小 OpenAI 兼容 HTTP 服务，
覆盖 T1–T11 全部注入场景。

---

## 11. 平台侧熔断（§9.1-6，入 S4 验收）

- 对象：平台 → 模型服务的 HTTP 调用。
- 计数口径（S4 架构审计修订，2026-10-09）：**「连续失败」而非「滑动窗口内累计」**。
  原措辞自相矛盾（"滑动窗口内连续"），且连续计数是更保守选择：一次成功即清零，
  不会因长时间窗口内的稀疏失败累积误开。`threshold=5`。
- 计入的失败：**连接错误、超时（含首字节超时）、5xx**。
  ⚠️ 必须覆盖**流式读写路径**——流式是主路径，漏记等于熔断永不触发。
- **不计入**：4xx 业务错误（400/401/403/404/413/429），避免上游限流误杀整条链路。
- 状态机（半开语义必须明确，否则协议无判定标准）：

  | 状态 | 行为约束 |
  |---|---|
  | `closed` | 正常放行 |
  | `open` | **全部拒绝**，直接走 §8.4 降级路径（503 或可选直连） |
  | `half_open`（冷却期结束） | **限制放行**：每次只允许 1 个探测请求在飞；探测成功 → `closed`；探测失败 → 立即回 `open` 并重置冷却计时。**不得**在 half_open 下放行全量流量 |

  修订理由：原措辞只说"半开探测恢复"，未限定半开期的流量，实现可解读为
  "冷却期一过即恢复全量"，使熔断退化为固定 30s 屏蔽窗（失去了保护意义）。

---

## 12. 变更记录

| 版本 | 日期 | 作者 | 说明 |
|---|---|---|---|
| v1 | 2026-10-09 | 架构师 agent | S2 初版：按 §9.2 定案落契约；补服务间鉴权（§3）、过渡模式（§8.3，解红线 7 与 Q3-C 冲突）、平台侧消费规则（§5.1）、契约测试 T1–T11 与 fake 桩（§10）。同版吸收架构评审 6 项发现：过渡模式三支判定（§8.3）、cost_usd 只替换 upstream 侧 + cost_source 新列（§5.1）、compliance_denied 与 401 双 code（§7）、幂等窗口定案（§4.1/§8.2）、trace.tenant 限定部署级不透明标签（§4.2） |
| v1.1 | 2026-10-09 | 架构师 agent | **S4 实现契约审计**（§13）：修订 §11 熔断计数口径与半开语义（原措辞自相矛盾且可被解读为"冷却期一过恢复全量"）；记录 6 项实现与契约的偏离，其中 3 项（A/C/I）为**阻断验收**项。全部为审计记录，**未改动任何实现代码** |

---

## 13. S4 实现契约审计（架构师 agent · 2026-10-09）

> 审计对象：工作区中的 S4 实施（`app/llm/remote.py`、`app/llm/model_service_ops.py`、
> `app/billing/ledger.py`、`app/billing` 迁移 `e5f6a7b8c9d0`、`tests/fake_model_service.py`）。
> 方式：逐条对照本协议 §1–§11 读实现；**未运行测试**（见偏离 I）。
> 结论：**实现忠实度总体高**（§5.1 优先级链、§5.1 两条独立路径、§8.3 双模式、
> §9 目录同步、§7 error.code 透传均已正确落地），但有 6 项偏离。

### 13.1 偏离清单

| # | 偏离 | 契约条款 | 级别 | 证据 |
|---|---|---|---|---|
| A | **熔断不覆盖流式路径，且超时不计入** | §11 | 🔴 阻断 | `remote.py` 仅两处调用 `record_failure()`（非流式连接失败、`_error_from_response`），`chat_stream` 的 `except httpx.HTTPError` 只抛 `_unavailable` 不记失败；流式为平台主路径 → 熔断永不触发 |
| B | 计数口径「连续失败」vs 契约原文「滑动窗口」 | §11 | 🟡 已修契约 | `remote.py:95` `_consecutive_failures`；实现更保守，故改契约而非改实现（见 §11 修订） |
| C | **红线 1 的「禁止字段出现即 400」未实现** | §1-1 / §4.2 | 🔴 阻断 | 全仓 `invalid_request` 仅出现在 `remote.py` 的错误映射表与 `tests/fake_model_service.py` 桩中；平台侧**无**任何禁字段校验。声明了却零实现 |
| D | 流式 `finish_reason` 恒为 `None` | §5 / §6 | 🟡 降级 | `remote.py:397` 硬编码 `finish_reason=None`，未从 chunk 解析；账本 `finish_reason` 失去归因价值 |
| E | `LLM_BACKEND=mock` 的生产 profile 守卫未实现 | §8.4 尾句 | 🟡 安全网 | `config.py` 无环境/profile 字段，无法判定"生产启动"，守卫缺载体 |
| F | `internal_error`「可重试 1 次」未实现 | §7 表 | 🟡 | 无重试逻辑；平台直接对外 502。需明确"重试"归平台还是归模型服务 |
| I | **§10 契约测试 T1–T11 零实现** | §10 | 🔴 阻断 | `tests/fake_model_service.py`（桩）与 `conftest.remote_mode` 夹具**均已就位**，但 `server/tests/` 内**无任何测试引用它们**（grep `remote_mode` 仅命中夹具定义本身）。故 `RemoteBackend` 从未经 API 层真实执行 → 上述偏离无法被测试发现 |

### 13.2 已确认的改进（非偏离，反向记录）

- `model_service_ops.sync_catalog` 为上游新模型建 `enabled=False` 行，而**不是**按目录
  启用 —— 优于契约字面表述（§9 只说"补缺失"）。新模型上线不会意外改变线上可用集合，
  由管理员显式开启。**建议将此行为固化为契约明文**，防后人"修正"成自动启用。
- `_parse_cost` 对非法 `cost_usd` 返回 `None`（按 unknown 处理），正确兜住红线 2。
- `main.py` 对 native 侧错误体也带 `code`（`{"detail", "code"}`），比契约只要求
  `error.code` 更完整，属加法演进。

### 13.3 处置要求

**A / C / I 为阻断验收项**：在补齐前，S4 不应标记为「完成」——尤其 I，
契约测试是本协议唯一的验收手段，缺它则其余条款都只是"读起来像实现了"。

B / D / E / F 为待办：B 已由 §11 修订闭合；D/E/F 需责任方确认归属后补实现或改契约。

> 本节只记录审计结论，**不替代**责任方的修复。审计未改动实现代码。

