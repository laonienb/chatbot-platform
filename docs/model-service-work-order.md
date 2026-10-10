# 模型服务 S3/S4 工作单（Work Order）

| 项 | 值 |
|---|---|
| 状态 | **✅ 全单闭合：P0 3/3、P1 11/11、P2 3/3**（2026-10-10 架构师 A 终轮验收通过，见协议 §22）。<br>✅ **P1-7**（`a1b3fd5`）、**P1-10**（`e96bc8b`，架构师 B §21.2 + A 预审）、**P1-8 / P1-9 / P1-11**（服务侧 `25c4bab`+`ee3dba4` / 平台侧 `a639446`）**全部闭合**；**H1–H4 全部处置完毕**。<br>**门槛解除**：原「P1-8/P1-9/P1-11 闭合前 S3 不得接管生产流量」的条件**已满足**（协议 §22.5）。<br>⚠️ 交付期间另修 **P1-6 遗留真缺陷**（`0a47bd5`：绝对上限/空闲超时的二分读错时钟 → 结算错归因）与 **P2-2 的「已闭合」空转**（红线 8 断言读顶层、真实服务放 `features.timeouts` ⇒ 从未触发；层级已在协议 v1.7-澄清3 钉死）。<br>📌 两处**条文裁定**已并入协议 §6.7/§8.1/§8.3 与 §12 v1.7-澄清3 |
| 出具 | 架构师 agent（2026-10-09） |
| 依据 | `docs/model-service-protocol.md` **v1.7**（契约）+ §13 审计 + §18 第五轮流式契约审计 + §19 第六轮交叉评审（已由 §20 会签生效） |
| 执行 | 后端 agent（S4 平台侧）+ 模型服务实现方（S3） |
| 规则 | 每项**逐条勾选**；勾选须附 commit 或「已执行 + 核验方式」。契约有疑义**先找架构师**，不要自行改契约来迁就实现 |

> 架构师**不写实现代码**。本单只定义：做什么、验收标准、归属、依赖。
> 与协议冲突时**以协议为准**；若认为协议有误，提出来改协议（架构师负责），
> **不允许**实现与协议静默偏离。

---

## 0. 验收门槛（先看这条）

**S4 不得标记「完成」直到 P0 全部闭合。** 理由：P0 含契约测试，
而契约测试是本协议唯一的验收手段——没有它，其余条款只是"读起来像实现了"
（这正是 P0-1/P0-2 能被漏掉的原因）。

| 优先级 | 含义 | 项数 | 状态 |
|---|---|---|---|
| **P0 阻断** | 不闭合则 S4 验收不通过 | 3 | ✅ **3 项全部闭合**（P0-1 契约测试 / P0-2 熔断 / P0-3 由 S3 实现 `c8bec13`；P0-3 的 2 处测试遗留已由 `a1b3fd5` 补齐，架构师已核对代码） |
| **P1 待办** | 影响正确性/可运维性，应与 S4 同批交付 | 11 | ✅ **11 项全部闭合**（P1-1/2/3/5/6 + P1-4 定案 + P1-7 `a1b3fd5` + P1-10 `e96bc8b` + P1-8/P1-9/P1-11 `25c4bab`/`ee3dba4`/`a639446`，架构师 A 终轮验收见协议 §22）。**H1–H4 全部处置**；架构师四处自设证伪均复现（服务侧 40 passed / 平台侧 180 passed）。**门槛条件已满足**（协议 §22.5） |
| **P2 协议缺口** | 已由架构师补入协议，实现按新条款跟进 | 3 | ✅ **3 项全部落地**（P2-1/P2-3 平台侧、P2-2 两侧；capabilities 超时暴露亦已由 S3 实现） |

---

## 1. P0 — 阻断验收（3 项）

### P0-1 补齐协议 §10 契约测试 T1–T11 🔴

| 字段 | 内容 |
|---|---|
| **归属** | 后端 agent（平台侧，对 fake 桩跑）+ 模型服务实现方（对真实实现跑） |
| **现状** | 脚手架**已就位**但零使用：`server/tests/fake_model_service.py`（可编程桩，含断连/慢响应/错误注入）、`conftest.py` 的 `remote_mode` 夹具（用真 HTTP 而非 mock transport，因为 T6 取消传播只能在真实连接上验）。`server/tests/` 内**无任何测试引用它们** |
| **做什么** | 按协议 §10 表实现 T1–T11 十一条用例 |
| **落点** | `server/tests/test_model_service_contract.py`（新建，命名可自定，需能 `pytest -q` 全绿） |
| **验收标准** | ① 十一条全部有对应用例，**用例名可追溯到 T 编号**；② 每条断言对应协议条款，不是"调通了就算过"；③ `pytest -q` 全绿并报出新增用例数；④ **负面用例必须真会失败**——提交前至少挑 T1/T2 各故意破坏一次，确认测试能红（防止写出永绿的空壳测试） |
| **阻塞** | 无它则 S4 无法验收；且 P0-2/P0-3 的漏出正是它的缺失所致 |
| **注意** | T6 需断言"平台断开后桩记录的上游调用 ≤2s 内被中止"——这是红线 6，唯一能验它的手段 |
| **✅ 后端已完成（2026-10-09）** | 落点 `server/tests/test_model_service_contract.py`（**54 个用例**）+ `server/tests/fake_model_service.py`（桩，含成本/计量/错误/沉默/心跳/幂等/合规/取消追踪共 20+ 可编程开关）。T1–T11 逐条对应：`test_t1_*`(2) / `test_t2_*`(2) / `test_t3_*`(2) / `test_t4_*`(2) / `test_t5_*`(4) / `test_t6_*`(1) / `test_t7_*`(2) / `test_t8_*`(2) / `test_t9_*`(3) / `test_t10_*`(3) / `test_t11_*`(2)，另加 §5.1 成本优先级链 6 条与审计/P1/P2 回归 12 条。<br>验收①✅ 用例名含 T 编号；②✅ 每条断言指向具体条款（docstring 注明）；③✅ 全量 **163 passed**（原 109，新增 54）；④✅ `test_p0_1_negative_cases_actually_fail` —— **不修改源码**，用局部 patch 构造"被破坏的实现"（把 unknown 成本当 0 / token 硬编码 999），断言正确实现下必须失败，证明测试有牙齿（不是永绿空壳）。<br>**真 HTTP 而非 mock transport**：T6 的取消传播只有在真实 TCP 断开时才可验证；为此桩支持 `silence_after_chunks`（发 N 个 chunk 后沉默）与 `keepalive_during_silence`。 |

### P0-2 熔断覆盖流式路径与超时 🔴

| 字段 | 内容 |
|---|---|
| **归属** | 后端 agent |
| **现状** | `record_failure()` 仅在非流式两处调用；`chat_stream` 的 `except httpx.HTTPError` 只抛 `_unavailable` **不记失败**；超时同样不计入。流式是平台主路径 → **熔断永不触发** |
| **做什么** | ① 流式路径的 5xx、连接错误、超时（含读超时与空闲超时）**全部计入**熔断；② 按协议 §11 修订版实现状态机：`half_open` **每次只放行 1 个探测请求**（当前实现半开放行全量，等价于固定 30s 屏蔽窗）；③ 4xx 业务错误仍**不计入** |
| **落点** | `server/app/llm/remote.py`（`CircuitBreaker` / `chat_stream`） |
| **验收标准** | ① 用例：流式连续 N 次 5xx → 熔断打开（**现有代码下此用例必须能捕获失败**，否则等于没测）；② 用例：half_open 下并发 10 个请求，只有 1 个真正发往上游；③ 用例：4xx 连打 10 次**不**打开熔断；④ 超时计入的用例 |
| **依赖** | 需先有 P0-1 的桩能力（错误注入 + 慢响应） |
| **✅ 后端已完成（2026-10-09）** | ① `test_audit_a_stream_failures_trip_breaker`（修复前必红：原 `chat_stream` 的 except 只抛不记）；② `test_p0_2_half_open_allows_only_one_of_many_concurrent`（`asyncio.gather` 并发 10 个，断言实际发往上游 == 1）；③ `test_p0_2_fourxx_never_trips_breaker`（400/429 各连打 10 次保持 closed）；④ `test_audit_a_stream_timeout_trips_breaker`（首字节超时也计入）。<br>实现侧：`CircuitBreaker` 改为半开**单探测位**（`_probe_in_flight`，探测成功→closed、失败→立即回 open 并重置冷却）；`chat_stream` 的 `except httpx.HTTPError` 补 `record_failure()`；空闲超时路径亦记失败。计数口径按 §11 修订保持**连续失败**（一次成功清零）。 |

### P0-3 红线 1「禁止字段出现即 400」实现 🔴

| 字段 | 内容 |
|---|---|
| **归属** | **模型服务实现方**（S3）——这是模型服务的入站校验，不是平台的事。**验收对象仅限 native 实现** |
| **Proxy 过渡期** | ⚠️ **本项记 N/A**（v1.4 会签，§14.3/§15.3）。Q3=C 定案 S3 先以 LiteLLM Proxy 起步，Proxy **无法**实现自定义入站校验，本项在过渡期客观不可闭合——否则整单会被一个不存在的实现方无限期卡住。**S3 native 交付时必须闭合本项；闭合前 native 实现不得接管生产流量** |
| **⚠️ 不可替代性** | 平台出网守卫与模型服务入站校验**拦截的是不同类别的错误**（§15.3-1）：守卫只能防**本平台自己犯错**（代码变更/新需求引入禁字段）；入站校验才拦得住**所有调用方**（将来的其他服务、误配直连、渗透测试）。**红线 1 的强制点在入站校验**，故本项**不得**因守卫存在而降级为 P2 |
| **现状（审计当时，已过时）** | ⚠️ 本栏描述的是**提出本项时**的状态，现已由下方两行回执解决，勿据此认为未实现。原文：协议 §1-1/§4.2 明令 `user_id`/`conversation_id`/`persona`/`trace`(v1，§14.3 收编) 出现即 400 `invalid_request`，但全仓 `invalid_request` 仅存在于平台错误映射表与 fake 桩中，**无任何入站校验实现** |
| **做什么** | 模型服务在请求解析阶段拒绝禁用字段；`fake` 桩已有此逻辑，可作参考实现 |
| **验收标准** | ① 用例：body 含 `user_id` / `conversation_id` / `persona` / `trace` 各一 → 400 + `code: "invalid_request"`；② 用例：正常 body（含**其他**未知扩展字段）**不得**被拒（宽松解析，协议 §4.2）；③ 错误信息说明触发了哪条红线，便于排障 |
| **⚠️ 后端说明（2026-10-09）** | **本项归属不变，仍属 S3 未闭合**：入站校验必须在模型服务实现（平台拦不住别人怎么调它）。<br>但后端已在**平台侧额外加一道出网守卫**作纵深防御：`remote.py` 的 `_guard()` 放在**真正出网处**（`_post` / 流式 `client.stream` 之前），因此**无法被组装流程绕过** —— 初版把守卫放在 `_body()` 内，子类在其后追加字段即可绕过；该缺陷由 `test_audit_c_forbidden_fields_rejected_platform_side` 用「会注入禁字段的子类」捕获，随后把守卫移到出网口才通过。<br>平台侧覆盖的禁字段：`user_id`/`user`/`conversation_id`/`conversation`/`persona`/`persona_id`/`trace`（body 顶层）＋ `messages[*]` 内同名键。另覆盖红线 10（`persona:` 前缀本地即 400）。 |
| **✅ 已完成（S3，2026-10-09，commit `c8bec13`）—— 架构师 A 已核对代码，本项闭合** | **落点** `model-service/msvc/guards.py`（`guard_request`）：`FORBIDDEN_TOP_FIELDS` 覆盖 `user_id`/`user`/`conversation_id`/`conversation`/`persona`/`persona_id`/`trace`；`FORBIDDEN_MESSAGE_FIELDS` 覆盖消息条目夹带；违反即 400 `invalid_request`，`param` 指向触发字段（满足验收③"说明触发哪条红线"）。<br>**调用位置即验收①的结构证据**：`main.py:98` 的 `guard_request(body)` **先于** `Idempotency-Key` 读取（`:109`）与 `_handle_nonstream`（`:131`）⇒ 结构上先于任何上游调用。<br>用例：`test_p0_3_forbidden_field_rejected_400`(3 参数)、`test_p0_3_message_level_forbidden_field`、`test_p0_3_unknown_extension_not_rejected`（验收②宽松解析）、`test_t3_persona_prefix_rejected`（红线10）。服务侧 `pytest -q` **21 passed**。<br>**⚠️ 架构师核对遗留（不推翻闭合，但需补）**：① 验收①列了**四个**字段，用例只参数化三个（`user_id`/`conversation_id`/`persona`）——**`trace` 无拒绝用例**，恰是 v1.5 新收编、最易漏的一个；② 用例 `test_p0_3_forbidden_field_rejected_400` 的注释写了「禁字段必须在任何上游调用前被拒（app.state.upstream 计数不动）」，但**没有对应断言**——注释里的验收点未落地（结构上已成立，缺的是把它钉住的断言，防后人把校验挪到上游调用之后）。 |
| **✅ 后端已补齐 2 处测试遗留（2026-10-09，commit `a1b3fd5`）—— 待架构师复核** | 上行「⚠️ 架构师核对遗留」逐条处置：<br>① **`trace` 已加入参数化**：`test_p0_3_forbidden_field_rejected_400` 现参数化 **4** 个字段（`user_id`/`conversation_id`/`persona`/`trace`），`trace`（协议 §4.2 v1.5 收编为「v1 禁发」、最易漏的那个）的拒绝用例已落地——400 + `code: "invalid_request"`。<br>② **「先于上游调用」已钉成断言**：该用例与 `test_p0_3_message_level_forbidden_field` 均补 `assert app.state.upstream.started == 0`，对齐平台侧 `test_audit_c_forbidden_fields_rejected_platform_side` 的 `assert control.calls == []` 强度——注释里宣称的验收点不再只是注释，防后人把校验挪到上游调用之后。<br>③ **证伪（确认断言有牙齿）**：把 `main.py:98` 的 `guard_request(body)` 挪到 `window.begin()` 之后 → 上述断言全部变红（`test_t7_local_reject_not_cached` 第二次请求得 409，在飞位残留）；已还原，`git diff model-service/msvc/` 为空。<br>④ 服务侧 `pytest` **21 → 25 passed**（本项 2 处为既有用例的参数/断言加强，不增实例数）；**实现零改动**。 |
| **✅✅ 架构师 A 复核通过：P0-3 测试遗留清零（2026-10-09）** | 架构师独立实跑 `pytest -k p0_3` 得 **6 passed**（4 字段参数化 + message 级 + 宽松解析），确认：① `trace` 的拒绝用例**确实存在并断言了 400 + `code: "invalid_request"`**（不再只是清单里列着）；② `upstream.started == 0` 断言**确实落地**（注释宣称的验收点已变成可执行断言）。<br>结合 `c8bec13` 的 `guards.py`（7 个顶层禁字段）与 `main.py:98` 的位置证据，**P0-3 的验收标准①②③与 2 处测试遗留全部满足**——该项自本行起**无遗留**。<br>**一处架构师裁决（不同意后端的可选加固提议）**：后端提议给 `test_p0_3_forbidden_field_rejected_400` 加 `Idempotency-Key` 以"单条自足"。**不采纳**——P0-3 的验收标准只要求"400 且先于上游调用"，"先于幂等窗登记"是 **P1-7 验收④的职责**，且已由 `test_t7_local_reject_not_cached`（带 key、2 参数）覆盖。给两条用例加同一职责会造成重复维护，并在 P0-3 引入与它无关的契约关注点。**职责边界保持现状。** |

---

## 2. P1 — 待办（11 项）

### P1-1 流式 `finish_reason` 解析

- **归属**：后端 agent ｜ **落点**：`remote.py:397`（当前硬编码 `finish_reason=None`）
- **做什么**：从流式 chunk 中提取 `choices[0].finish_reason`（通常出现在最后一个内容 chunk），填进 `StreamDone`
- **验收**：用例断言流式结束的 `StreamDone.finish_reason == "stop"`，且账本该字段落库非空
- **为何重要**：`finish_reason` 是判断"正常结束 vs 被 max_tokens 截断"的唯一依据，恒为 None 使账本失去该归因能力
- **✅ 后端已完成（2026-10-09）**：流式循环累积 `choices[0].finish_reason` 并带入 `StreamDone`；桩也按真实 OpenAI 语义补了收尾 chunk（`delta:{}` + `finish_reason`）。用例 `test_t4_stream_chunks_usage_and_terminate_event` 断言 `done.finish_reason == "stop"`。

### P1-2 流式空闲超时（缺口 K）

- **归属**：后端 agent ｜ **落点**：`remote.py` 的 `chat_stream`
- **做什么**：按协议 §8.1 修订版实现**空闲计时**——无任何字节（含心跳）超过 90s 则断开
- **验收**：① 用例：桩发 3 个 chunk 后永久沉默 → 平台在空闲上限内断开并走 §8.4 降级；② 用例：桩持续发 `: keepalive`（间隔 < 上限）→ 连接**不被**断开
- **注意**：**不得**依赖 httpx 的 total 超时保护流式（对流式不适用，这正是缺口根因）
- **✅ 后端已完成（2026-10-09）**：新增 `settings.model_service_stream_idle_timeout`（默认 90s）；`chat_stream` 用 `asyncio.wait_for` 对**每一行**计时，只有协议活动行（注释/事件/data）重置计时，纯空白行不算（防"持续发空行吊住连接"）。用例 `test_p1_2_idle_stream_aborted`（沉默 → 504 `upstream_timeout` + 计入熔断）与 `test_p1_2_keepalive_resets_idle_timer`（心跳期不误杀）。<br>**如实说明一处语义重叠**：平台仍保留 `model_service_timeout_first_byte`（默认 15s）作为 httpx 的 **read 超时**（协议 §8.1 表里的"平台读超时"）。当它小于空闲上限时，它先于看门狗生效；因此空闲看门狗是**覆盖 httpx 行为差异的第二道保险**，而非唯一防线。协议要求的硬约束（**不依赖 total 超时**、显式空闲计时的**沉默**语义、心跳可重置）均已满足。测试中把读超时调大以单独验证看门狗。

### P1-3 `LLM_BACKEND=mock` 生产守卫（缺口 E）

- **归属**：后端 agent ｜ **落点**：`server/app/config.py`（当前**无环境/profile 概念**，守卫缺载体）
- **做什么**：引入环境标识（如 `APP_ENV=dev|prod`），`APP_ENV=prod` 且 `LLM_BACKEND=mock` → **拒绝启动**（协议 §8.4 尾句 / 红线 9）
- **验收**：① 用例：`APP_ENV=prod` + `LLM_BACKEND=mock` → 启动抛错且信息明确；② 用例：`APP_ENV=dev` + mock → 正常启动
- **为何重要**：mock 会**假装成功**，是生产环境最危险的降级路径
- **✅ 后端已完成（2026-10-09）**：`config.py` 新增 `app_env`（默认 `dev`，可 `APP_ENV` 注入）；`main.py` 的 lifespan 开头守卫，`prod` + `mock` → `RuntimeError`（文案含红线 9 与整改指引）。用例 `test_p1_3_prod_with_mock_refuses_startup` / `test_p1_3_dev_with_mock_starts_fine`。

### P1-4 重试责任划分与 `error.retryable`（缺口 F — 架构师已定案，转实现）

| 字段 | 内容 |
|---|---|
| **归属** | 模型服务实现方（发 `retryable`）+ 后端 agent（透传与诊断） |
| **定案** | 协议 **§7.1**（v1.3 立规则，**v1.4 终裁**）：**重试阶梯只有一级** —— 模型服务对上游重试（可换 provider）；平台**只**在"请求未送达"（建连/DNS 失败）时重试 1 次，**收到任何 HTTP 响应后一律不重试**；`internal_error` **不逐请求重试**，交由平台熔断 + 降级链（§8.4/§11）承担 |
| **✅ 裁决已给出（架构师 B §14.2 + 架构师 A §15.2，2026-10-09）** | 后端提出的「§7.1 内部张力」**成立**，且是**架构师 A 的疏漏**：v1.3 只加了硬规则，未同步清理 §7 表三处「可重试」残留与 T5，致使 `retryable` 按原文成为死字段。<br>**终裁：维持硬规则（方案 B），`retryable` 重定位为诊断/透传字段，平台不据此重试。后端现有实现正确，无需返工。**<br>同步已并入：§7 表清理 + `rate_limited` 改「透传 `Retry-After` 由客户端退避」+ T5 修订方向 + `retryable` 新定义（语义 = 失败发生在进入上游处理**之前**；**与状态码正交**，不得隐含"仅 5xx 可能为 true"）。 |
| **做什么（残留）** | ① 模型服务在错误体返回 `error.retryable: bool`（缺省 `false`）——S3 侧；② 平台把该字段**透传**进对外错误体供客户端判断重试安全性（平台自身不重试）——后端侧 |
| **验收标准** | ① 用例：`retryable: true` 的错误 → 平台**不**重试（诊断/透传语义）；② 用例：`retryable: false`（含 `internal_error`）→ 不重试；③ 用例：字段缺失 → 视为 `false`；④ 用例：建连失败（请求未送达）→ 重试 1 次且**复用同一 `Idempotency-Key`**；⑤ 用例：该字段**与状态码正交**——503 + `retryable: true`（未发起上游调用）可被正确解析并透传 |
| **为何定案为"都不逐请求重试"** | 状态码无法表达"重试是否安全"；`internal_error` 重试同一实例无收益；而**收到响应后**重试会命中 §8.2 幂等窗口，直接返回首次结果，纯属徒增延迟 |
| **✅ 后端已完成（2026-10-09）** | 初版按 v1 的 §7 表实现了"`internal_error` 重试 1 次"——**该实现已被 v1.3 推翻，现已移除**。改为：`_connect_once` 仅在 `httpx.ConnectError`/`ConnectTimeout`（**请求未送达**）时重试 1 次，且复用同一套 headers（`Idempotency-Key` 不变）；收到任何响应（含 500）**一律不重试**。`error.retryable` 已解析进 `ModelServiceError.retryable`。<br>用例：`test_p1_4_no_retry_after_any_http_response`（500 只打 1 次）、`test_p1_4_retryable_field_is_parsed_but_not_retried`、`test_p1_4_retryable_defaults_false`、`test_p1_4_connection_failure_retried_once`。<br>⚠️ **契约内部张力（请架构师裁决）**：§7.1 同时要求"平台**必须**依据 `retryable` 字段决定重试"与"收到任何响应后一律不重试"。两条叠加的实际语义是**该字段不产生重试行为**（目前只作诊断/可观测用途）。后端按"硬规则优先"实现（不重试），并用 `test_p1_4_retryable_field_is_parsed_but_not_retried` 把这一解读钉住，避免后人各自发挥。若架构师意图是"`retryable=true` 时平台仍要重试"，请明示，我改实现。
| **验证补充** | 建连失败用例让平台指向 `http://127.0.0.1:1`（必然 ECONNREFUSED），并断言真实桩 `upstream_started == 0` —— 证明确实"未送达"而非"慢超时"。 |

### P1-5 启动自检失败的可读性（缺口 H）

- **归属**：后端 agent ｜ **落点**：`model_service_ops.startup_self_check`
- **做什么**：连接失败/超时时抛出明确的操作指引（协议版本不符已清晰，但连不上时是裸 `httpx` 异常 → 部署期体验差）
- **验收**：用例断言 connect 失败时异常信息含"模型服务不可达"与 `MODEL_SERVICE_BASE_URL` 提示
- **✅ 后端已完成（2026-10-09）**：新增 `_unreachable(path, exc)`，把 `httpx.HTTPError` 换成 `ProtocolVersionError`，文案含"模型服务不可达"、失败路径、`MODEL_SERVICE_BASE_URL` 当前值与三条排查步骤；`fetch_capabilities`/`fetch_models`/proxy 探活三处统一走它。用例 `test_p1_5_unreachable_service_gives_actionable_error` 断言含"模型服务不可达"与 `MODEL_SERVICE_BASE_URL`。

### P1-6 流式绝对总时长兜底（缺口 M，v1.4 会签新增）

| 字段 | 内容 |
|---|---|
| **归属** | 后端 agent ｜ **落点**：`remote.py` 的 `chat_stream` |
| **为何需要** | §8.1 重写后，平台对流式的保护 = 读超时 15s + 空闲上限 90s，而**空闲计时会被心跳重置**：不合规/故障的服务只要每 <90s 发一个 `: keepalive` 就能**无限期挂住连接**（慢速资源耗尽）。启动断言只约束**合规**实现 |
| **做什么** | 新增平台侧单流绝对上限 `model_service_stream_max_duration`（§15.4 倾向默认 **180s**，可配），超限按协议 **§8.1.1 收尾语义**处理 |
| **验收标准** | ① 用例：桩持续发心跳但永不结束 → 平台在绝对上限处断开（与 P1-2 的「心跳期间不误杀」用例**互补**：那条验证 <上限 时心跳保活，这条验证 >上限 时心跳保不住）；② 用例：超限时**不得**改状态码为 503（SSE 已以 200 发出）；③ 用例：超限时**已产生的部分内容照常落库**，账本按已收 token 结算（**不是 `failed`**）；④ 用例：超限计入熔断；⑤ `error_code == "stream_max_duration"` 落库 |
| **⚠️ 易错点** | 流已开始（200 已发出）时**无法**再走 §8.4 的 503 降级——实现方容易写出"流到一半抛 503"这种不可能的行为。收尾必须按 §8.1.1 |
| **契约依据** | 协议 §8.1（绝对上限）+ §8.1.1（收尾语义）；会签 §15.4 |
| **✅ 后端已完成（2026-10-09，commit `1ef9975`）** | 新增 `settings.model_service_stream_max_duration`（默认 180.0，`config.py:53`）；`remote.py:486` 起以墙钟预算 `min(idle, remaining)` 逐行等待，到点置 `terminate_reason="stream_max_duration"` 并绝对切断（**心跳不重置**，与空闲计时相互独立）。收尾按 §8.1.1：不改状态码/不 503、优雅关流、部分内容按已收 token **结算 settled（非 failed）**、计熔断一次；`StreamDone.terminate_reason` 透传至 `chat.py:454`/`:657` 落库为 `error_code`。<br>用例 2 条：心跳拖不断（与 P1-2「心跳期不误杀」互补）+ 超限 settled 归因。平台侧全量 **167 passed**（架构师 A 本地复核 2026-10-09）。｜ **⚠️ 后端补记（2026-10-10，commit `0a47bd5`）：本项的计时二分有一处真缺陷，由 P1-9 的新用例查出并修复** | 上行「心跳不重置、到点绝对切断」的实现里，触发 `TimeoutError` 后**再取一次** `time.monotonic()` 与 `abs_limit` 比较，用来区分「绝对上限」还是「空闲超时」。但 `asyncio.wait_for` 用的是事件循环**每轮缓存**的时钟，可能比 monotonic 早触发几微秒 ⇒ 复核算出「还没到上限」，于是绝对上限被**冒名**成空闲超时：抛 504 `upstream_timeout` 走 §8.4 降级。后果不止文案——raise 会让服务层 `_settle(«upstream_error:...»)` 把该单结算成 **failed**，而 §8.1.1-3 要求部分内容按已收 token 结算为 **settled**。单独跑不复现、全量并发才现，所以此前一直显示为绿。<br>**改法**：不再重读时钟，直接用**本次等待实际取的那个上界**判定 —— `timeout = min(idle, remaining)`，故 `remaining <= idle` 时触发时刻的沉默时长恰为 `remaining`（≤ idle），空闲不可能先到，只能是绝对上限；反之空闲先到。判定与定时器误差无关。<br>**触发者**：P1-9 的 `test_p1_9_native_absolute_cap_gives_no_error_frame`（§8.1.1 对外不给错误帧）。三条 cap 用例连跑 3 次全绿，平台全量 **180 passed**。

### P1-7 幂等窗口入窗边界（§8.2.1 新增，防双烧）

| 字段 | 内容 |
|---|---|
| **归属** | **模型服务实现方（S3）** —— 这是模型服务的幂等窗口行为；后端侧仅需核对透传 |
| **为何需要** | v1.4 新增 §8.2.1 的入窗判定：**按「是否伴随上游调用」而非按状态码类别**。若 S3 按"4xx 一律不入窗"实现，`413 context_length_exceeded` 会被排除——而它恰恰是**上游已处理并已计费**之后返回的，客户端在窗口内重发将导致上游**再次被调用并再次计费**（真实双烧漏洞） |
| **做什么** | 按协议 **§8.2.1 三分表**（v1.5 定稿）实现入窗：① **结果不明**（超时／流中断／已发起调用后的 5xx）→ **必须入窗**（可能已计费，重发即双烧）；② **确定性拒绝**（`413`、上游 404）→ 入窗（同输入必同拒绝，回放正确且省一次调用）；③ **瞬时拒绝**（上游 `429`、上游 `503`）→ **不入窗**（未计费无双烧；重发可能成功，入窗会堵死退避恢复）；④ 本地拒绝（400/401/403/本地 429）与未送达 → 不入窗。**判定权用 `retryable` 字段表达**：`true` ⇒ 不入窗；`false` 且伴随上游调用 ⇒ 入窗 |
| **验收标准** | ① 用例：`413` 后同 key 重放 → **回放**首次结果，上游 `upstream_started` 不增加；② 用例：**上游 429 后同 key 重发 → 真实重调上游（不回放）** ⭐ 本轮新增，防"瞬时限速被放大成 10 分钟故障"；③ 用例：**上游超时后同 key 重发 → 回放首次错误（不重调）** ⭐ 防双烧；④ 用例：本地 400 / 本地 429 后同 key 重放 → 不入窗，可正常重新处理 |
| **为何必须成对验证** | ②③ 是**方向相反**的两个用例，缺一即会漏掉一类错误：只测 ③ 会写出"一切失败都入窗"（堵死退避），只测 ② 会写出"一切失败都不入窗"（双烧）。协议 §17.4 已把这条升格为**红线 11** |
| **为何同时要求"本地拒绝不入窗"** | 否则终端客户端在窗口期内重发同一请求会直接回放首次的 429/400，**退避重试永远失败**（§14.2 的必需配套） |
| **契约依据** | 协议 §8.2.1（三分表，**v1.5**）+ §7.1（`retryable` 三用途）+ §1 红线 11；会签 §15.2-1 / §17.2 |
| **✅ 已定稿，可开工** | v1.4 版本的"伴随上游调用一律入窗"经 §16.2 证伪，**已于 v1.5 替换**。P1-7 解除冻结 |
| **⚠️ 架构师 A 核对（2026-10-09，commit `c8bec13`）：实现已落地，验收缺 2 条，本项暂不闭合** | **实现侧已正确**：`idempotency.py:56` 的 `should_cache(status, retryable, reached_upstream)` 落实三分——`retryable` 为真直接不入窗（`:58`）、`status in (413, 404)` 入窗（`:62`）、其余按是否到达上游判定；`main.py:141-165` 三处 `_finish_window` 均传真实 `reached_upstream` 与 `err.retryable`。<br>**已有用例（T7）**：`test_t7_transient_error_not_cached`（验收②，上游 429 → 重发真实重调且 `upstream.started == 2`）、`test_t7_unknown_result_must_cache`（验收③，已发起调用后的 5xx → 回放且 `started == 1`）、`test_t7_same_key_calls_upstream_once`（成功回放）。<br>**缺的两条（正是本项的核心保护，不可省）**：<br>① **验收①无用例**——`413 context_length_exceeded` 的**确定性拒绝入窗正例**没有任何测试。当前只测了"瞬时不入窗"（②）与"结果不明入窗"（③），**唯独漏了 `413` 入窗这一条**；而 `413` 入窗恰恰是本项 v1.4 立项目的（防 `413` 被当 4xx 排除造成双烧）。若实现被改成"4xx 一律不入窗"，现有 3 条用例**全部仍会通过**——即这道防线目前无人看守。<br>② **验收④无用例**——"本地 400 / 本地 429 不入窗可重新处理"未测。结构上成立（`guard_request` 在 `main.py:98`，早于 `:109` 读 `Idempotency-Key`，本地拒绝根本不进窗口），但**缺断言**。<br>**为何不放过**：本项的价值就是把"4xx 一刀切"这类错误钉死；只测两个方向中的一个方向，等于把工作单自己写的「缺一即漏一类错误」当成了建议。补齐前 **S3 不得接管生产流量**（与 P0-3 同一门槛口径）。 |
| **✅ 后端已补齐 2 条验收用例（2026-10-09，commit `a1b3fd5`）—— 待架构师复核后闭合** | 上方「缺的两条」逐条处置（**实现侧零改动**：本项实现原本就正确，未动 `msvc/`，未动契约条文）：<br>① **验收①（`413` 确定性拒绝入窗正例，本项存在的理由）已补**：`test_t7_deterministic_reject_413_must_cache`——注入 `scenario.error = {code: context_length_exceeded, retryable: false}` → 首次断言 **413** 且 `upstream.started == 1`；清掉 error（模拟上游恢复）后用**同一个 `Idempotency-Key`** 重发 → 仍 **413**（回放缓存）且 `upstream.started == 1`（未重调上游）。docstring 已写明它与 `test_t7_unknown_result_must_cache` 的语义区别（「结果不明/可能已计费 → 防双烧」vs「确定性拒绝/同输入必同拒绝 → 省一次上游调用」），避免被误读为重复用例。<br>② **验收④（本地拒绝不入窗）已补**：`test_t7_local_reject_not_cached`，参数化 **2** 例（禁字段 `user_id` → 400；`model="persona:libai"` → 400）——首次带 key 发禁用 body → **400** 且 `upstream.started == 0`；随后**用同一个 key** 发正常 body → **200** 且 `upstream.started == 1`。作用是把「`guard_request` 在 `main.py:98` **先于** `:109` 读 `Idempotency-Key` 与 `:120` 的 `window.begin()`」这一结构事实钉成断言，防后人把校验挪到 `begin()` 之后（那会让本地拒绝占住在飞位，同 key 重发得 409，客户端退避重试永远失败）。<br>③ **证伪（两道防线都确认有牙齿，改完已还原）**：(a) 把 `idempotency.py:62` 的 `if status in (413, 404):` 改成 `if False:` → `test_t7_deterministic_reject_413_must_cache` **变红**（`assert 200 == 413`，同 key 重发真实重调了上游），**同时原有 3 条 T7 用例仍全绿**——实证架构师判断「旧用例在『4xx 一律不入窗』下会全部通过」；(b) 把 `main.py:98` 的 `guard_request(body)` 挪到 `window.begin()` 之后 → `test_t7_local_reject_not_cached` **两个参数均变红**（第二次请求得 409，在飞位残留）。两处均已还原，`git diff model-service/msvc/` 为空。<br>④ 回归：服务侧 `pytest` **21 → 25 passed**（新增实例：413 +1、本地拒绝 +2、`trace` 参数 +1）。 |
| **✅✅ 架构师 A 复核通过：P1-7 闭合（2026-10-09）** | **验收标准四条逐条落实**：① `413` 入窗有 `test_t7_deterministic_reject_413_must_cache`（回放且 `started` 不增）；② 上游 429 真实重调有 `test_t7_transient_error_not_cached`；③ 结果不明入窗有 `test_t7_unknown_result_must_cache`；④ 本地拒绝不入窗有 `test_t7_local_reject_not_cached`（2 参数）。<br>**架构师独立复现证伪（不只读回执）**：把 `msvc/idempotency.py:62` 的 `if status in (413, 404):` 改为 `if False:` 后 `pytest -k t7` 得 **1 failed, 5 passed** —— **只有新用例变红，原有 3 条 T7 全绿**。这条实测结果**正是** v1.4 立项时的判断依据（"旧用例在『4xx 一律不入窗』下会全通过"），现已被经验证实，不再是推测。改坏处已还原（`git diff` 为空）。<br>**独立核实**：`a1b3fd5` 只改 `model-service/tests/test_contract.py`（+67/−4），`msvc/` 零 diff；服务侧 `pytest` 架构师实跑 **25 passed**。<br>**结论：P1-7 闭合**，原「⚠️ 架构师核对」一行的"暂不闭合"结论**已被本行取代**。 |

### P1-8 流式收尾 chunk 携带 `finish_reason`（缺口 N，v1.6 新增）

| 字段 | 内容 |
|---|---|
| **归属** | **模型服务实现方（S3）** ｜ **落点**：`model-service/msvc/main.py` `_handle_stream` + `provider.py` 的 stream 事件 |
| **为何需要** | 平台 P1-1 已实现「从流式 chunk 提取 `finish_reason` 填进 `StreamDone`」（`remote.py:557`，断言见 `test_model_service_contract.py:156`），但**真实模型服务从不发**——`main.py:203` 调 `_chunk(ev["content"], model_used)`，`finish` 默认 `None`；`provider.py` 的 `FakeUpstream.stream` 也不 yield 收尾 chunk。协议 §6 原未规定（v1.6 补为 §6.6）。**后果**：切 `remote`/`native` 后 `StreamDone.finish_reason` 恒 `None`，P1-1 在生产路径失效；平台契约测试跑自家桩（桩已发）故全绿，**桩分歧掩盖** |
| **做什么** | ① 在 usage chunk **之前**补一个收尾 chunk：`choices[0].delta` 为空、`finish_reason` 非 null（`stop`/`length`，与上游实际值一致）；② 服务侧 T4 补断言「存在一个 `finish_reason != null` 的 chunk，且位于 usage chunk 之前」 |
| **验收标准** | ① 用例：模型服务流式响应含非 null `finish_reason` 的收尾 chunk；② 平台侧 T4 在**对真实服务跑**时同样成立 |
| **契约依据** | 协议 §6.6（v1.6）+ §5；评审 §18.2-F1 |
| **为何平台契约测试没发现** | 平台侧 T4 跑的是**平台自己的桩**（`server/tests/fake_model_service.py:379-389` 已按真实 OpenAI 语义发收尾 chunk）——**"两端各自跑自己桩"是本轮两处缺口的共同结构性盲区** |
| **✅ 后端已完成（2026-10-10，commit `25c4bab`）—— 待架构师复核** | ① **服务侧真发收尾 chunk**：`provider.py` 的 `FakeUpstream.stream` 新增 `{"kind":"finish","finish_reason":scenario.finish_reason}` 事件（`Scenario.finish_reason` 默认 `stop`，可注入 `length` 以验「与上游实际值一致」），`main.py` 收到后发 `_chunk(None, model, finish)`（`delta` 为空、`finish_reason` **非 null**）且**位于 usage chunk 之前**，同时写入 `StreamTerminal.finish_reason` ⇒ P1-10 的终态重放也保持该字段（§8.2.2-3 一致性清单里本就列着它）。<br>② 验收①：`test_t4_stream_chunks_usage_terminate_done` 补**结构级**断言（逐行 `json.loads`，要求"存在非 null finish_reason 的 chunk，其索引 < usage chunk 的索引"）。文本匹配不行——内容 chunk 里就带 `"finish_reason": null`，`"finish_reason" in text` 会假绿。另加 `test_p1_8_finish_reason_reflects_scenario`（回显上游实际值，防硬编码 `stop`）、`test_p1_8_success_stream_carries_finish_reason`、`test_p1_8_replay_preserves_finish_reason`（回放一致性）。<br>③ **证伪**：删掉 `main.py` 的 finish 分支 → **4 条变红**，含被收紧的 T4 —— 即 T4 的**旧版**在"服务侧不发 finish"下仍全绿，实证本项立项理由（桩分歧掩盖）。改坏已还原。<br>④ 回归：服务侧 **31 → 39 passed**。<br>⑤ 验收②（平台侧 T4 对**真实服务**跑）：平台自家桩本来就发收尾 chunk，故平台 T4 一直绿，"两端同形"并不因此自动成立。本轮在平台侧加了 `test_p1_9_clean_stream_still_counts_as_success`，断言 `done.finish_reason == control.finish_reason`；真正的两端同形请架构师在终轮验收里按 §21 口径复核。

| **✅✅ 架构师 A 终轮验收通过：P1-8 闭合（2026-10-10，协议 §22）** | 独立实跑服务侧 **40 passed**；**自设证伪**：删掉 `msvc/main.py` 的 `kind == "finish"` 分支 → **4 条变红**（含被收紧的 T4）。核验落地：收尾 chunk 在 usage **之前**、`finish_reason` 非 null 且取上游实际值（`Scenario.finish_reason` 可注入 `length`）、写入 `StreamTerminal` ⇒ 终态重放保持该字段；T4 用**结构级**断言（逐行 json 索引比较）而非文本匹配，避免被 `"finish_reason": null` 蒙混。**结论：闭合**。 |

### P1-9 流式错误语义：首字节前 HTTP 码 / 首字节后流内错误帧（缺口 O，v1.6 新增）

| 字段 | 内容 |
|---|---|
| **归属** | **模型服务实现方（S3，出错误）+ 后端 agent（S4，识别并计入熔断）** |
| **为何需要** | 现状：模型服务把**首字节前**的上游错误（含 429/5xx）也发成 **200 + 流内 `data:{"error":…}`**（`msvc/main.py:198` 首 token 超时、`:211-213` 上游错误），而平台流式循环**完全不解**该帧（`remote.py:536-560` 只认 choices/usage）→ 帧被跳过、流正常收尾 → `remote.py:573` 记 `record_success()`、无 `error_code`。**流式（主路径）下上游限流/超时对平台不可见、熔断永不计数**（违反 §11），账本按成功结算、`Retry-After` 丢失。协议 §6 原未定义（v1.6 补为 §6.7） |
| **做什么** | ① **S3**：首字节**前**的错误（尚未写出任何事件）→ 返回 **HTTP 错误状态**（同 §7 表，`Retry-After`/`error.code` 正常）；首字节**后**的错误 → 用**流内错误帧** `data: {"error": {…}}` 收尾；② **S4**：平台流式循环**必须识别** `data` 帧中的顶层 `"error"` → 记为失败、计入熔断（§11）、按 `error.code` 归因，**不得**按"正常结束"结算；部分内容按 §8.1.1 口径按已收 token 结算；③ 平台侧桩需可注入「流内错误帧」场景 |
| **验收标准** | ① 用例：对 `stream=true` 注入首字节**前**的 429 → 模型服务返回 **HTTP 429**（非 200），`Retry-After`/`error.code` 可读；② 用例：首字节**后**的错误 → 平台识别流内错误帧、**熔断计数 +1**、结算 `error_code` 为该 code（**非成功**）；③ 平台侧：桩发一个流内 `{"error":…}` 帧 → 断言平台不把它当成功、且已产出的部分内容按 §8.1.1 结算 |
| **契约依据** | 协议 §6.7（v1.6）+ §11 + §8.1.1；评审 §18.2-F2 |
| **⚠️ 架构师 A 预裁决（2026-10-09）：流内错误帧计入熔断与否，必须按"错误码"而非"一律计入"** | §18.2-F2 的表述「平台识别该帧、**计入熔断**」若直读成"任何流内错误帧都计一次失败"，会与 **§11 的负列表**（「**不计入**：4xx 业务错误（400/401/403/404/413/429），避免上游限流误杀整条链路」）直接冲突——典型反例：上游 `429` 的流内错误帧若也计数，**重复限流会把整条链路熔断，正是 §11 要避免的误杀**。<br>**已核实平台既有实现给出了唯一自洽解**：`remote.py:31-44` 的 `_CODE_TO_PLATFORM` 已把「错误码 → (对外 status, 告警, **是否计熔断**）」编码完毕，非流式路径正是走它计数（`:336-350`）。其中 `rate_limited`/`budget_exhausted` = **不计**，`internal_error`/`upstream_error`/`service_unavailable`/`upstream_timeout` = **计**。<br>⇒ **P1-9 的 S4 实现必须复用这张表**（把流内错误帧构造成等价错误对象后走同一分类），**不得**在流式路径里另写一套"一律计入"。判定规则：**"是否计熔断"由 `error.code` 经 `_CODE_TO_PLATFORM` 决定，不由"是否流内帧"决定**。<br>⇒ 验收③相应细化：桩注入流内 `rate_limited` 帧 → **不得**计熔断；注入流内 `upstream_timeout`/`internal_error` 帧 → **必须**计熔断。**两条都要有**（只测一条会写出另一个方向的一刀切）。<br>（本条不改契约条文，属对 §6.7 与 §11 的**一致性解释**；已写入协议 §6.7 的补充说明。） |
| **依赖** | 与 P1-2（空闲超时）、P1-6（绝对上限）同属"流式收尾"家族，建议同批做 |
| **➕ v1.7 追加范围（G3/G4/O1，架构师 A §20.5 采纳）** | ① **G3-1**：错误帧之后**必须仍发 `data: [DONE]`**（S3 现状直接 `return`）——平台循环以 `[DONE]` 为唯一正常终止信号，统一终态让客户端解析器不必区分"错误收尾"与"连接异常断开"；② **G3-2**：错误帧**可**携带 `usage` 扩展字段（实测部分用量，加法字段）——错误终止时上游已产出部分 token 且**已计费**，而服务侧明知实测值却让平台降档估算（`metering_source` 降档 + `needs_review`）；平台优先采信、缺失时按估算回退；③ **O1（真实缺陷，非风格问题）**：`msvc/main.py:190-196` 把**首字节后的 chunk 间隔超时硬编码为 1.0s**，且触发时文案写「上游首 token 超时」——真实上游（`LiteLLMProvider`）中途正常停顿 >1s 即被**误杀**，且**归因错误**。改为独立可配预算（建议默认 **30s**）、纳入 capabilities `timeouts` 暴露、**受红线 8"内层 < 外层"启动断言约束**，错误码与首 token 超时**区分**。<br>**验收追加**：④ 错误帧后必须出现 `[DONE]`；⑤ 错误帧携带 `usage` 时平台采信该值（`metering_source` 不为降档估算）；⑥ 中途停顿 2s 的流**不得**被误杀；⑦ 中途停顿超预算时的 `error_code` **区别于**首 token 超时 |
| **⚠️ 与 P1-10 的交叉（重要）** | P1-9 与 **P1-10（流式入幂等窗）改同一段 `_handle_stream`**。**建议同一实现方连续做完**，否则两处改动会互相覆盖；尤其错误帧终止态**也要入窗**（P1-10 的入窗分类已含"流内错误帧终止"），故 P1-9 的收尾路径必须与 P1-10 的 `window.finish` 调用点对齐 |
| **⚠️ 架构师 A 预审（2026-10-10，读在飞代码 + 协议 §6.7/§11/§8.1/§8.1.1）** | **S3 侧 O1 在飞改动方向正确**：独立可配 `upstream_chunk_idle_timeout=30s`、错误码 `upstream_chunk_timeout` 与首 token 的 `upstream_timeout` 区分、`capabilities.timeouts` 补 `upstream_chunk_idle_s`、桩补 `stall_after_chunks`。**S4 侧问题（尚未落地，当前仅在 `_CODE_TO_PLATFORM` 加了映射）**：<br>① **必须复用 `_CODE_TO_PLATFORM` 分类**，不得在流式路径另写"流内帧一律计数"。建议抽共用小函数（如 `_classify_error_code(code, fallback_status) -> (status, alert, counts)`），让 `_error_from_response` 与流式路径走同一处，防两套口径漂移。<br>② **尾部判定必须改为按码**：现为 `terminate_reason=="stream_max_duration" ? record_failure : record_success`；加入流内错误帧后规则应是「**任何流内错误帧都不得 `record_success`**；其 `counts==True` 者才 `record_failure`」。注意流内 `rate_limited` 帧**既不 success 也不 failure**（§11 负列表）——否则重复限流会误杀整条链路，正是 §11 要防的。<br>③ **防双计**：错误帧之后若连接再抛 `httpx.HTTPError`，勿重复 `record_failure`（每请求至多记一次）。<br>④ **结算口径**：按 §6.7 末句走 §8.1.1——已产出部分内容**按已收 token 结算**、`error_code=帧内 code`、**不改状态码**（复用 P1-6 的 `terminate_reason` 通路最省）；帧内 `usage`（G3-2）优先采信实测值，缺失才回退估算（`metering_source` 不得无故降档）。<br>⑤ **桩能力**：平台侧 `server/tests/fake_model_service.py` 当前**无法注入流内错误帧**，验收③须先补该能力，并**证伪**（把识别逻辑去掉，新用例必须变红）。<br>⑥ **启动断言（呼应 B 的 H2）**：`startup_self_check` 须补 `upstream_chunk_idle_s < model_service_stream_idle_timeout`（90s）；否则配成 >90 时平台空闲超时先断，模型服务的归因永远看不到。<br>⑦ **两处契约小缺口（非阻断，建议顺手澄清）**：(a) 流内 `rate_limited` 帧**无法携带 `Retry-After`**（帧内只有 `error` 体、无 header），§7 的"透传 Retry-After"在流式路径**失效**——要么给 `error` 加可选 `retry_after` 字段，要么在 §6.7 明文声明流式路径不承诺；(b) `upstream_total_s=60` 在**流式路径未被强制**（`_handle_stream` 只有首 token + 中途空闲两个预算），与"模型服务自持总超时"的表述有出入——要么流式也套总预算，要么在 §8.1 明写"流式由 (first_token, chunk_idle) + 平台绝对上限 180s 约束"。 |
| **✅ 后端已完成（2026-10-10，commit `25c4bab` 服务侧 + `a639446` 平台侧）—— 待架构师复核** | **接手说明**：另一后端会话 00:05 起停手，留下**未提交**的 O1 在飞改动（`config`/`main`/`provider` 共 25 行，方向正确但**零用例**）。本会话在其之上补完，未回退其改动。<br>**S3 侧**（`25c4bab`）：O1 的实现属那边，本会话补验收 ⑥⑦ —— `test_p1_9_mid_stream_stall_within_budget_is_not_killed`（默认 30s 预算下停顿 2s **不得**被误杀，并断言默认值 ≥5s，防有人压回 1s）、`test_p1_9_chunk_budget_exceeded_has_its_own_code`（超预算 → `upstream_chunk_timeout`，**不等于** `upstream_timeout`；首字节已过故只能流内帧、其后仍 `[DONE]`、部分内容保留）、`test_p1_9_capabilities_exposes_chunk_idle_budget`。另给两条 break 路径补 `_aclose(agen)`（红线6：上游生成器不悬空）。<br>**S4 侧**（`a639446`）逐条对预审 ①–⑥：① **抽了共用函数** `_classify_error_code(code, *, raw_status=None)`，`_error_from_response` 与 `_error_from_frame` 走同一处；② 尾部判定改为**按码**：流内帧一律**不** `record_success`，是否 `record_failure` 由 `_CODE_TO_PLATFORM` 的第三元决定（流内 `rate_limited` **既不 success 也不 failure**，§11 负列表）；③ **防双计**：帧后关流若再抛 `httpx.HTTPError`，同一请求不再重复计入（`stream_error` 已置则只记日志）；④ 结算走 §8.1.1 通路：归因写进 `StreamDone.terminate_reason`，`chat.py` 既有代码把它落成 `error_code`，内容按已收 token 结算为 **settled**（不是 failed）、状态码不改；帧内 `usage`（G3-2）优先采信；⑤ **桩原先根本无法注入流内错误帧** —— 补 `stream_error` / `stream_error_after_chunks` / `stream_error_usage` 三个开关；⑥ 启动断言见 P2-2 那行的自我更正（顺带挖出一处更大的问题）。<br>**对外那一半（新增，已走「三」通告）**：原生面 `event_stream` 此前**一个 try/except 都没有**，而 Starlette 在 `stream_response` 里**先发 200 响应头再迭代 body**（`starlette/responses.py:251`）⇒ 状态码早已不可改，异常只能截断流，前端看到"没有 [DONE] 的半截回复"、code 与退避时长全丢。现两面统一`data:{"error":{…code、retryable、retry_after_s…}}` 然后 `[DONE]`；兼容面另补上"错误帧后不发 [DONE]"这处**平台自己违反 G3-1**（拿它验收模型服务，自己转发时却漏）。判据用 `error_message` 有值而非 `terminate_reason` 有值 —— §8.1.1 的绝对上限对外只表现为「回复中断」、不给错误提示，该边界由 `test_p1_9_native_absolute_cap_gives_no_error_frame` 钉住。<br>**验收对应**：①→`test_t12_pre_first_byte_error_keeps_http_semantics`；②→`test_p1_9_instream_internal_error_frame_counted_exactly_once`；③两方向→`test_p1_9_instream_rate_limited_frame_not_counted`（预置 3 次连续失败：计了变 4、当成功则归零，两个方向的错法都能抓）+ 上一条；④→错误帧后必发 `[DONE]`（`25c4bab` 服务侧 / `a639446` 对外两面）；⑤→`test_p1_9_instream_error_usage_adopted_not_downgraded`；⑥⑦→上面 S3 那两条。<br>**证伪五处**（改坏必红后全部还原，`grep FALSIFY` 归零）：删帧识别 → 6 红；改"流内帧一律计入" → `rate_limited` 那条红；兼容面去掉帧后 [DONE] → 对应红；原生面吞掉 done 错误 → 对应红；红线 8 只读顶层 → 两条红。<br>⚠️ **自查出一处永绿用例**：`usage_adopted` 初版**只**断言 usage 数字 —— 而帧里的 `usage` 会被循环末尾那条通用 usage 提取顺手捡到，把"错误帧识别"整段删掉也照样通过。已补 `terminate_reason` 断言才有牙齿。建议记进 §17.4 的共同教训：**断言"值被采纳"不够，必须同时断言"这条路径被走到"**。<br>**回归**：平台全量 **167 → 180 passed**（契约 58 → 71）。
| **✅ 后端已补齐 H3 / H4（2026-10-10）** | **H2**（启动断言补 `upstream_chunk_idle_s`）已随 P1-9 的 `a639446` 落地，见 P2-2 那行的自我更正；**H1**（docstring 与实现矛盾）随 P1-11 落地。<br>**H4**（首字节前流式超时的入窗**正例**）：新增 `test_h4_pre_first_byte_timeout_must_be_cached` —— 首 token 超时属「结果不明/可能已计费」（`retryable=false` + `reached_upstream=true`）⇒ 必须入窗，同 key 重发回放 504 且 `started` 不增。与既有的 `test_p1_10_pre_first_byte_failure_not_cached`（上游 429 瞬时拒绝**不**入窗）构成**方向相反的一对**；只有后者时，实现写成"首字节前一律不入窗"会**全绿**。<br>**证伪**：把该分支的 `reached_upstream` 传成 `False`（等价于"首字节前一律不入窗"）→ H4 用例变红而 429 那条保持绿，正是双向原则要的两类各一条。已还原。<br>**H3**（部署文档给幂等窗容量公式）：写入 `model-service/README.md` —— `max_entries ≥ 峰值RPS × 窗长(600s)` 的最坏上界、`max_chars ≈ max_entries × 单条终态平均字符数`（默认 256 条 / 1e6 字符 ≈ 每条 3900 字符，长回复会先撞字符上限）、两上限谁先到按插入序淘汰，并把"淘汰 WARNING 正常应为零，持续增长即容量算小"写成运维信号（每次淘汰都可能换来一次真实重调上游 = 二次计费）。顺带把 README 的超时自持一行补全为「首 token 15s / 中途空闲 30s（O1）/ 总 60s 仅约束非流式（v1.7-澄清2）」。<br>回归：服务侧 **39 → 40 passed**。至此 §21.3 的 H1–H4 全部处置完毕。

| **✅✅ 架构师 A 终轮验收通过：P1-9 闭合（2026-10-10，协议 §22）** | 独立实跑平台侧 **180 passed**（104.61s）；**自设证伪两处**：① 删 `remote.py` 流内错误帧识别 → **6 条变红**；② 把 `_error_from_frame` 的按码分类改成"一律计熔断" → `test_p1_9_instream_rate_limited_frame_not_counted` 变红（`_consecutive_failures` `3→4`）。核验：首字节**前** → HTTP 错误码（T12）；首字节**后** → 流内帧 + 其后 `[DONE]`；**是否计熔断由 `error.code` 经 `_classify_error_code` 决定**（流式/非流式同一处，预审①落实）；部分内容按 §8.1.1 记 **settled**、帧内 `usage` 采信（G3-2）；H2 启动断言补 `upstream_chunk_idle_s` 并修掉"只读顶层"的空转（P2-2）。**遗留 O-a（非阻断）**：流内 `rate_limited` 帧未回填 `retry_after_s`（限速多在首字节前走 HTTP 429，流内罕见）。**结论：闭合**。 |

### P1-10 流式入幂等窗 + 终态重放（G2 🔴，v1.7 会签新增）

| 字段 | 内容 |
|---|---|
| **归属** | **模型服务实现方（S3）** ｜ **落点**：`model-service/msvc/main.py` 的 `_handle_stream` + `idempotency.py` |
| **为何需要** | S3 在 `main.py:114` 以注释把窗口范围缩到非流式（`if idem and not is_stream:`），**协议从未做过此豁免**。而**流式成功必然已调用上游、必然已计费**，按 §8.2.1 三分表属「成功响应 ✅ 入窗」。后果是**真实双烧**：兼容面幂等键 = `api_key+model+消息体` 指纹带 10 分钟桶窗，**流式路径同样生成该键**（`chat.py:612`，已核实）并经 `remote.py:246-248` 透传；客户端（AstrBot/QQ 网络抖动重发）在窗口内重发 → 模型服务**真实重调上游、二次计费**，且平台在 `chat.py:613` **再次 `reserve_usage`** ⇒ 用户也被重复扣费。与 §16.2 是同一暴露面、**方向相反**（那边"不该入窗的入了"，这边"该入窗的没入"） |
| **做什么** | ① **入窗分类与传输形态无关**（§8.2 告示 + §8.2.2）：流式**成功**终止、以及**已发起上游调用后**的流内错误帧终止 → **必须入窗**；**首字节前**的失败走 §6.7 的 HTTP 错误状态、按现有三分表判定（**不属**此项范围）。**不允许实现以注释自缩契约范围**（`main.py:114` 的注释必须删掉并改为实现真实语义）。<br>② **在飞位（`begin` → 409）语义对流式同样适用** —— 防同 key 并发双流各自真实调用上游。<br>③ **回放形态 = 「终态重放」**（§8.2.2 第 3 条，取代 §19.2-2 的 (a)/(b) 二选一）：命中窗口时**以缓存的重放内容重新生成一条 SSE 流** —— 内容文本、`usage`、`finish_reason`、终止事件、`[DONE]` 与首次一致；**chunk 边界允许不同**（**不**缓存事件序列）。理由：缓存**终态**有界，缓存**事件序列**随生成长度无界；OpenAI 兼容客户端关心内容与用量，不关心分块切法。<br>④ **不采用 409 显式拒绝**：会把"重发失败"暴露给客户端（而重发正是幂等窗要吸收的正常行为），且与"并发在飞"的 409 语义重叠、客户端无法区分"稍后重试"与"永久失败"。<br>⑤ 缓存须有**条数/体积上限**；**淘汰后不得静默重调上游**——须按 §8.4 降级或显式声明未命中语义（写进 `model-service/README.md`） |
| **验收标准** | ① 用例：`stream=true` 带 `Idempotency-Key` 发一次 → 成功；**同 key 重发 → 上游 `upstream.started` 不增加**（证明未重调上游、未二次计费），且重放的**内容文本与 `usage` 与首次一致**；② 用例：**同 key 并发两条流** → 第二条被在飞位拒绝（409），且 `upstream.started` 只加 1；③ 用例：流式**错误帧终止**（已发起上游调用）后用同 key 重发 → **回放**该错误终止态，`upstream.started` 不增加；④ 用例：**首字节前**的失败（HTTP 层 429）**不入窗** —— 上游恢复后同 key 重发**必须真实重调**（与 ③ 方向相反，缺一即漏一类）；⑤ **证伪要求**：把入窗判定改回"流式不入窗"，①③ 必须**变红**（防止写出永绿用例） |
| **契约依据** | 协议 §8.2（v1.7 告示）+ **§8.2.2**（终态重放）+ §8.2.1 三分表；评审 §19.2 + 会签 §20.1/§20.2 |
| **⚠️ 与 P1-9 的交叉** | 与 P1-9 改同一段 `_handle_stream`，**建议同一实现方连续做完**；（本项 ③ 与 P1-9 的错误帧收尾是同一处代码路径） |
| **门槛** | 与 P0-3/P1-7/P1-8/P1-9 同口径：**本项闭合前 S3 不得接管生产流量** |
| **✅ 后端已完成（2026-10-10，commit `e96bc8b`）—— 待架构师复核** | 逐条处置：<br>① **入窗分类与传输形态无关已落地**：`chat_completions` 的窗口分支去掉 `not is_stream`（原 `main.py:114` 的自缩注释已删除并改为真实语义），流式/非流式共用同一窗口；顺序保持「先 get 再 begin」。**首字节前的失败**同样按 §8.2.1 三分表判定——为此在 `_handle_stream` 里前移了「先取第一个事件」的分界（P1-9 的结构前提，见下）：首字节前失败 → HTTP 错误状态 + `_finish_window`（瞬时/`retryable` 不入窗）；首字节后 → 流内错误帧并**必须入窗**（`_cache_stream_terminal`，不套 `should_cache` 的原因写在注释里：§8.2.1 排除"瞬时拒绝"的理由是"未处理未计费"，首字节后不成立）。<br>② **在飞位对流式适用**：`window.begin` 对流式同样调用（409 分支共用）。<br>③ **终态重放**：新增 `StreamTerminal`（内容文本/usage/finish_reason/终止事件/错误帧）+ `_replay_stream`，命中即**重新生成一条 SSE 流**；**不缓存事件序列**（`CachedResponse.stream` 只存终态）。<br>④ **未采用 409 显式拒绝**。<br>⑤ **缓存有界 + 淘汰语义显式**：`IdempotencyWindow(max_entries=256, max_chars=1e6)`（env `MS_IDEMPOTENCY_CACHE_MAX_*`），超限按插入序淘汰最旧并打 WARNING；**被淘汰 key 按未命中处理**已写入 `model-service/README.md`（协议允许该支，但要求显式声明、不得静默）。<br>**验收**：① 成功流同 key 重发回放（内容/usage/终止事件一致，`started` 不增）；② 同 key 并发第二条 409 且 `started` 只加 1（**用真 uvicorn + 真 TCP**：httpx `ASGITransport` 会整条缓冲响应体，用它写并发用例会假绿）；③ 首字节后错误帧终止态回放、`started` 不增；④ 首字节前 429 不入窗、上游恢复后同 key 重发**真实重调**；⑤ 条数上限与"淘汰后按未命中"另有专门用例。另补：回放 chunk 边界可与首次不同但内容一致。<br>**证伪（已还原）**：窗口分支改回 `if idem and not is_stream:` → `tests/test_stream_semantics.py` **3 failed, 3 passed**，变红的正是 ①②③（④⑤ 保持绿）。<br>**回归**：服务侧 pytest **25 → 31 passed**（新增 6 实例）。<br>**同提交顺带落地（属 P1-9 范围、此处为结构前提）**：首字节分界的 HTTP 错误路径 + **错误帧后仍发 `[DONE]`**（§6.7 G3-1，也是终态重放一致性的前提）；既有用例 `test_t11_first_token_timeout` 由"响应文本含 upstream_timeout"（在 JSON 错误体下会假绿）收紧为断言 **HTTP 504 + code + `retryable=false`**。P1-9 的剩余范围（S4 识别流内帧、按 code 计熔断、部分 usage 结算、O1 中途停顿预算）不在本提交。 |
| **✅✅ 架构师 B 复核通过，本项闭合（2026-10-10，§21.2）** | 独立实跑服务侧 **31 passed**；逐点读码核验：流式入窗（窗口分支只判 `idem`、先 get 再 begin）、在飞位真 uvicorn 并发 409、终态重放一致性（`_replay_stream` 逐项重发内容/finish_reason/usage/终止事件/`[DONE]`，chunk 边界参数化用例钉住）、首字节前失败 HTTP 化并按三分表入窗、错误终态回放、G3-1/G3-2、O1 独立 30s 中途预算 + capabilities 暴露 `upstream_chunk_idle_s`、淘汰 WARNING + README 声明、断连不入窗与 §6.5 abandoned 路径一致。A 预审（上行）结论相同。**非阻断跟进转 P1-9/P1-11**：H1（docstring 宣称"无白名单"与 `should_cache:152` 现状矛盾）、H2（平台启动断言补 `chunk_idle < 90s`）、H3（部署文档给幂等窗容量公式 `max_entries ≥ 峰值RPS×600`）、H4（补首字节前流式超时**入窗正例**） |

### P1-11 `should_cache` 去白名单化（G6 🟡，v1.7 会签新增）

| 字段 | 内容 |
|---|---|
| **归属** | **模型服务实现方（S3）** ｜ **落点**：`model-service/msvc/idempotency.py:62` |
| **为何需要** | 该行仍是 `if status in (413, 404): return True` —— **正是 §8.2.1（v1.6）明文禁止的状态码白名单模式**。后果：上游返回 **400/401/403** 这类**已到达上游的永久性拒绝**被判为**不入窗**，于是窗口期内每次重发都**白打一次上游**（这些失败重发必然同样失败，入窗回放才对）。该实现写于澄清之前，且 P1-7 的验收用例只钉了 `413` 方向，**测试拦不住** |
| **做什么** | 入窗判定改为**两布尔规则**：`retryable` ＋ `reached_upstream`（成功响应单列），**不再用状态码判断**。`should_cache` 的 `status` 形参可保留用于成功判定，但**不得**再出现 `status in (…)` 式的白名单分支 |
| **验收标准** | ① 用例：上游 **401**（`retryable=false`，已到达上游）→ 同 key 重发**回放**、`upstream.started` 不增加；② 用例：上游 **403** 同上；③ **现有 5 条 T7 用例全部保留且全绿**（尤其 `test_t7_deterministic_reject_413_must_cache` 与 `test_t7_local_reject_not_cached` —— 后者是本项的安全网：它保证"本地拒绝不入窗"不被顺手改坏）；④ **证伪要求**：把两布尔规则改回 `status in (413, 404)`，① ② 必须**变红** |
| **契约依据** | 协议 §8.2.1（v1.6 澄清 + v1.7 定案）+ §7.1（`retryable` 重写定义）；评审 §19.3-G6 + 会签 §20.4 |
| **⚠️ 反复发生的教训（架构师 A 记）** | 同一处、同一写法**已错两次**：架构师写白名单 → B 发现语义错 → 架构师改两布尔 → **实现仍留白名单**。说明**仅澄清条文不足以纠正实现，必须有用例钉住**。故本项**强制证伪**，不接受"改完跑绿即可" |
| **✅ 后端已完成（2026-10-10，commit `25c4bab`）—— 待架构师复核** | `should_cache` 现为 §8.2.1 规定的**两个布尔**：`retryable` ⇒ 不入窗；2xx ⇒ 入窗；其余（`retryable=False`）由 `reached_upstream` 决定 ⇒ 上游 400/401/403/413/404 这类**已到达上游的永久性拒绝**与"结果不明"同支入窗（入窗结论相同、归因不同），本地拒绝因 `reached_upstream=False` 不入窗。`status` 形参只用于成功判定，**函数体内已无任何 `status in (...)` 分支**。<br>验收：①② → `test_p1_11_upstream_deterministic_reject_must_cache`（参数化上游 401 `upstream_auth_failed` / 上游 403 内容策略；首请求断 `started == 1` 以证明错误**确实来自上游**、与本地拒绝用例划清界限；重发回放且 `started` 不增）；③ → 现有 5 条 T7 **全部保留且全绿**（`test_t7_local_reject_not_cached` 作安全网未被改坏）；④ **证伪**：把末支改回 `status in (413, 404)` → `pytest -k "p1_11 or t7"` 得 **2 failed, 5 passed**，红的正是两条新用例，而 `test_t7_deterministic_reject_413_must_cache` **保持绿** —— 复现架构师"旧用例不看守此防线"的判断。<br>顺带闭合 H1：模块 docstring 早已宣称"不存在 `status in (...)` 式白名单分支"，代码却是白名单；现在条文与实现一致。<br>回归：服务侧 **31 → 39 passed**（本项 +2 实例）。

| **✅✅ 架构师 A 终轮验收通过：P1-11 闭合（2026-10-10，协议 §22）** | 独立实跑服务侧 **40 passed**；**自设证伪**：`should_cache` 改回 `status in (413, 404)` → 上游 **401/403 两条变红、`413` 那条保持绿**（再次复现"旧用例不看守此防线"）。核验：两布尔规则（`retryable` / `reached_upstream`），`status` 仅用于成功判定、**函数体内无 `status in (...)` 白名单**；上游 401/403 这类**已到达上游的永久性拒绝**入窗回放；docstring 与实现一致（H1 闭合）。**结论：闭合**。 |

---

## 3. P2 — 协议缺口（架构师已补，实现跟进）

> 这三条上一轮**协议没说到**，实现按旧契约做并无错。协议已修订，实现按新条款跟进。

### P2-1 目录同步的「下线」方向（缺口 J，协议 §9.1 新增）
- **归属**：后端 agent ｜ **落点**：`model_service_ops.sync_catalog`
- **做什么**：上游 `/v1/models` 中**消失**的 id → 对应 `llm_models` 行标 `enabled=False`；若该行是 `is_default` 则清除默认标记并按现有回退逻辑重选
- **契约硬约束**：**下线永不删除行**（行承载展示名/排序/白名单等运营配置，"临时下架又恢复"不应变成人工重建）
- **验收**：① 用例：目录少一个 id → 该行 `enabled=False` 且**未被删除**；② 用例：被下线行原为默认 → 默认标记被清除且默认回退到其他启用行；③ 用例：同步**不启用**任何行、**不改**展示名/排序
- **现状提醒**：当前 `sync_catalog` 只 `add`，不处理消失项
- **✅ 后端已完成（2026-10-09）**：`sync_catalog` 增加下线方向——上游缺失的**已启用**行标 `enabled=False`（**从不删除**）；若被停用行是默认，则清除其标记并按"其他启用行里优先取原 is_default"回退，写回 `new_default`。返回值扩展为 `{added, disabled, total_upstream, new_default}`。用例：`test_p2_1_disappeared_model_disabled_never_deleted`、`test_p2_1_disabled_default_falls_back`、`test_p2_1_sync_never_enables_rows`。

### P2-2 超时分层重写（缺口 L，协议 §8.1 重写）

- **归属**：模型服务实现方（暴露能力）+ 后端 agent（读并断言）
- **做什么**：模型服务在 `/v1/capabilities` 暴露 `timeouts: {"upstream_first_token_s": N, "upstream_total_s": M}`；平台启动自检**断言内层 < 外层**（替代人工读配置）
- **验收**：① 用例：capabilities 声明内层 ≥ 外层 → 平台**拒绝启动**并说明违反了红线 8；② 用例：正常分层 → 启动通过（即 T11 自动化）
- **✅ 后端已完成（2026-10-09）**：`startup_self_check` 在 native 模式读取 `caps["timeouts"]`，对 `upstream_total_s` 与 `upstream_first_token_s` 各自断言 `< model_service_stream_idle_timeout`，违反则抛 `ProtocolVersionError`（文案含"红线 8"与整改方向）；字段缺失时跳过（向后兼容 Proxy 期）。用例：`test_p2_2_capabilities_timeout_layering_violation_refuses_startup`、`test_p2_2_capabilities_timeout_layering_ok`。<br>注：形状按协议 §8.1 的 `timeouts` 读取；桩的默认 capabilities 已带该字段。
- **⚠️ 后端自我更正（2026-10-10，commit `a639446`）：上行那条"已完成"不成立，断言实际从未执行**：上行说"在 native 模式读取 `caps["timeouts"]`"——读的是**顶层**，而真实服务（`msvc/main.py` 的 capabilities）把 `timeouts` 放在 **`features.timeouts`** 里；再叠上"字段缺失时跳过（向后兼容 Proxy 期）"，结果是**对真实服务一条断言都不会触发**。测试之所以一直绿，是因为 P2-2 的两条用例把 `timeouts` 注入在**顶层**（与实现对齐、与真实服务不对齐）—— 与 P1-8 那条"两端各自跑自己桩"是同一类盲区，只是这次藏在一条**已登记为闭合**的条目里，比未做的更危险。<br>已修：`startup_self_check` **两处都读**（`caps["timeouts"]` 或 `caps["features"]["timeouts"]`），三个预算在同一循环里按同一条规则校验，并新增 `upstream_chunk_idle_s < 平台流式空闲上限`（架构师 B 的 **H2**）。平台桩的默认 `capabilities` 已改为**与真实服务同形**（`timeouts` 在 `features` 内）。新增用例：`test_p2_2_timeouts_read_from_real_service_nesting`（嵌套形状注入违规 → 必须拒绝启动）、`test_h2_chunk_idle_budget_violation_refuses_startup`、`test_p2_2_default_stub_capabilities_pass`（断言桩的形状没被改回顶层，防盲区复发）。**证伪**：把读法改回只读顶层 → 前两条变红。<br>**条文问题（请架构师裁，后端未擅改契约）**：§8.1 只写"必须在 `/v1/capabilities` 暴露（`timeouts: {...}`）"，而 §8.3 的响应形状示例里**没有** `timeouts` 字段 ⇒ **嵌套层级没钉死**，两处实现各自解读才会走到今天这一步。建议钉成 `features.timeouts`（与真实服务现状一致，不动线协议）。

### P2-3 `trace.tenant` 的合规约束（协议 §4.2 已有，需实现侧注意）

- **归属**：后端 agent
- **做什么**：平台发送 `trace.tenant` 时**必须是部署级不透明标签**（环境名/租户池名），**不得**由 `user_id`/`conversation_id` 派生
- **验收**：代码审查 + 用例断言 `trace.tenant` 不等于任何用户标识（派生即"换名传身份"，违反红线 1）
- **注**：当前实现的 `_body()` 未发送 `trace`，属可选字段，暂不阻塞
- **✅ 后端已完成（2026-10-09）**：保持**不发送** `trace`（最小暴露面）；同时把 `trace` 列入出网守卫的禁字段清单——若将来有人从平台侧塞 `trace`，会在出网前 400 失败而不是悄悄带出去。用例 `test_p2_3_no_identity_derived_trace` 断言请求体中无 `trace`。 |

---

## 4. 已由架构师闭合（实现方无需动作，仅知晓）

| 项 | 处置 |
|---|---|
| 缺口 B（熔断计数口径"滑动窗口 vs 连续"） | 协议 §11 明确为**连续失败计数**（更保守，一次成功即清零）。实现无需改 |
| 缺口 G（半开语义未定义） | 协议 §11 补状态机表：half_open 每次只放行 1 个探测。**实现需按此改** → 见 P0-2 |
| 缺口 J / L（下线语义、超时分层） | 协议 §9.1 新增、§8.1 重写 → 见 P2-1/P2-2 |

## 5. 反向记录：实现优于契约（建议固化，勿"修正"）

- `sync_catalog` 为上游新模型建 **`enabled=False`** 行（而非按目录启用）——
  上游新增模型**不会**意外改变线上可用集合，需管理员显式开启。
  契约 §9 原文只说"补缺失"未规定启用状态，**此项应作为明文要求保留**（见 P2-1 验收③）。
- `_parse_cost` 对非法 `cost_usd` 返回 `None`（按 unknown 处理）——正确兜住红线 2。
- `main.py` native 侧错误体也带 `code`（`{"detail", "code"}`），比契约要求更完整，属加法演进。

---

## 6. 执行顺序建议

```
P0-1（契约测试）──┬─→ P0-2（熔断）
                  └─→ P0-3（禁字段，S3 侧可并行）
P1-1 / P1-2 / P1-5（平台侧，可并行）
P1-3（需先定 APP_ENV 载体）
P1-4（定案已给，平台+模型服务两侧并行）
P2-1 / P2-2（协议已就绪，可并行）
```

**先做 P0-1**：它既是验收门槛，又是后续所有修复的回归保护网。

---

## 7. 变更记录

| 版本 | 日期 | 作者 | 说明 |
|---|---|---|---|
| v1 | 2026-10-09 | 架构师 agent | 首版。依据协议 v1.1 与 §13 审计（含第二轮补充 J/K/L），把 11 项偏离/缺口转为可执行工作单，定 P0/P1/P2 与归属、验收标准、执行顺序 |
| v1.1 | 2026-10-09 | 架构师 agent | P1-4 定案：协议新增 **§7.1 重试责任划分** 与 `error.retryable` 字段（重试阶梯只有一级，平台仅对"未送达"重试）。该项由"待定案"转为"待实现"，并补验收标准 |
| v1.2 | 2026-10-09 | 架构师 A | **v1.4 会签后同步**：① P0-3 加注 **Proxy 过渡期记 N/A**（验收对象仅限 native；过渡防线 = 平台出网守卫）与「**不可替代性**」定性（守卫防本平台犯错，入站校验才拦得住所有调用方，故不得降级为 P2）；② P1-4 裁决落定（维持方案 B，后端无需返工；契约张力系架构师 A 在 v1.3 的疏漏，§7 表与 T5 已同步清理）；③ **新增 P1-6**（缺口 M 流式绝对上限 + §8.1.1 收尾语义）与 **P1-7**（§8.2.1 幂等入窗边界，防 413 双烧）；④ P1 计数 5→7 |
| v1.3 | 2026-10-09 | 架构师 A | **v1.5 会签后同步**：P1-7「做什么」改为 §8.2.1 **三分表**（结果不明必入窗／确定性拒绝可入窗／瞬时拒绝不入窗），验收补 ⭐ 两条方向相反的用例（上游 429 必须真实重调、上游超时必须回放），并注明缺一即漏一类错误；解除 P1-7 冻结（定稿可开工）。依据 §16.2 证伪 + §17.2 会签 |
| v1.4 | 2026-10-09 | 架构师 A | **核对 S3 交付（`c8bec13`）后的验收记录**，依据为读代码而非读回执：① **P0-3 判为闭合**（`guards.py` + `main.py:98` 先于上游调用的位置证据），但记 2 处**测试遗留**：`trace` 无拒绝用例、注释宣称的"先于上游调用"无断言；② **P1-6 判为闭合**（`1ef9975`，复核 167 passed）；③ **P1-7 判为「实现已落地、验收缺 2 条」故不闭合**——缺 `413` 确定性拒绝入窗正例与本地拒绝不入窗用例，**其中前者是本项存在的理由**（现有 3 条用例在"4xx 一律不入窗"的错误实现下会全绿）；④ 状态表由「P0-3 未闭合」更正为「P0 全闭合 / P1 6 项闭合」——同批 S3 commit 的收尾提交 `6ca3e84` 只更新了 `AGENTS.md` 看板，未同步本单，故此前状态**滞后于代码** |
| v1.5 | 2026-10-09 | 架构师 A | **第五轮评审（协议 §18）后同步**：新增 **P1-8**（流式收尾 chunk 携带 `finish_reason`，模型服务从不发 → 平台 P1-1 在生产路径静默失效）与 **P1-9**（流式错误语义：首字节前应返回 HTTP 错误码、首字节后应发流内错误帧且平台必须识别并计入熔断；现状是 200+流内帧 + 平台完全不解 → 熔断不计数、按成功结算）。P1 计数 7→9；依据协议升 **v1.6**。同批复核确认 P0-3 / P1-7 的验收遗留**仍未补齐** |
| v1.6 | 2026-10-09 | 架构师 A | **复核后端 agent 补齐（`a1b3fd5`）后闭合 P0-3 与 P1-7**：① P1-7 **闭合**——验收①②③④四条全部有用例，且架构师**独立复现证伪**（`idempotency.py:62` → `if False:` 得 `1 failed, 5 passed`，**只有新用例红**），实证旧 3 条 T7 确实不看守该防线；② P0-3 **测试遗留清零**——`trace` 入参数化、`upstream.started == 0` 断言落地，架构师实跑 `-k p0_3` 得 6 passed；③ **裁决不采纳**后端"给 P0-3 用例加 `Idempotency-Key`"的可选加固（"先于幂等窗"是 P1-7 验收④的职责，已由 `test_t7_local_reject_not_cached` 覆盖，重复职责会造成维护重叠）；④ 状态表更新为「P0 全闭合 / P1 九项中 7 项闭合」；⑤ 更正 v1.5 行末句"P0-3/P1-7 验收遗留仍未补齐"——该判断在 `a1b3fd5` 后**已过时** |
| v1.7 | 2026-10-09 | 架构师 A | **§19 第六轮评审会签后同步**（协议升 v1.7）：① **新增 P1-10（🔴）**——流式不入幂等窗（`msvc/main.py:114` 注释自缩范围）⇒ 兼容面指纹键重发**真实重调上游 + 平台重复 `reserve_usage`**，双烧上游且重复扣用户；定案**入窗分类与传输形态无关**，回放形态取**「终态重放」**（内容/usage/终止事件一致、chunk 边界可不同；不采用事件序列回放——缓存无界；不采用 409——把重发失败暴露给客户端）。**② P1-9 追加范围**：G3-1 错误帧后必须发 `[DONE]`、G3-2 错误帧可携带实测 `usage`、**O1 真实缺陷**（首字节后 chunk 间隔超时硬编码 1.0s 且归因文案错误 → 真实上游中途停顿 >1s 被误杀）；并记明 P1-9 与 P1-10 **改同一段代码、建议同一方连续做完**。**③ 新增 P1-11**——`should_cache` 仍是被 §8.2.1 明禁的状态码白名单（上游 401/403 不入窗），强制两布尔化 + 证伪。P1 计数 9→11；**四项流式待办全部闭合前 S3 不得接管生产流量** |
