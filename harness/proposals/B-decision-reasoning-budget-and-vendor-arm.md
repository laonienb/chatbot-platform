# 架构师 B 裁决意见：reasoning 与「首 token」预算语义 ＋ `MS_UPSTREAM_VENDOR` 的出口对账

- 出具方：**max-架构师-B**（C 侧唯一架构师，`981f5ce1-6cb8-40f9-9a8c-b76cf0010582`）
- 派单：工作区管家-C，存证 `harness/proposals/C-ask-arch-B-reasoning-budget-and-vendor-arm.md`
- **裁决基准**：commit **`cdda16a`**（2026-10-12 00:03:53）。依据一律取该 commit 的文本，**未读工作区字节**（`web/lib/api.ts` 此刻有前端会话未提交改动 ⇒ 我不读不碰，引用一律取 `git show cdda16a:web/lib/api.ts`）。
- **复核方式**：`git archive cdda16a model-service` → 仓库外临时目录 → `server/.venv/Scripts/python.exe` 实跑探针。**共享工作树零写入、未起任何会写工作树的进程**（结束时 `git status --porcelain` 只剩本文件与管家那份未跟踪的派单存证；快照已删）。`model-service/**` 在 G 侧租约内（至 00:36:43 / 03:30:00），本单全程未进该目录写入。
- 出具时刻：2026-10-12 00:1x+08:00

## 裁决摘要

| 问题 | 裁决（一句） |
|---|---|
| 一① 「首 token」定义 | **定义不改**（§8.1 字面「上游首 token」是对的），**改实现让它名副其实**：上游任何 chunk（含 reasoning-only）都须复位该计时；同时**新增第二项预算**「首个正文 token 超时」，因为原预算被修正后不再保护"只推理不出正文" |
| 一② 推理期间发 keepalive | **要发**，归 **§6-4**、发送方＝**模型服务侧**；本轮**只做内层**（服务发、平台仍吞），**对外透传单独走 `AGENTS.md`「三」通告**。前端零改动（已按实际分支核实） |
| 一③ 504 入窗是否给 reasoning 开例外 | **不开例外**；改为**入窗 TTL 分类**——成功仍 600s，「结果不明」类（首 token/总超时/已发起调用后的 5xx）用独立短 TTL（新增配置，建议 **60s**） |
| 二① 是否把「按 `api_base` 主机名判定」写进协议 | **不写**（真值表不可维护，且会制造"已封死"的错觉；一致性检查在逻辑上无法自证 ⇒ 那是配置校验，不是合规控制） |
| 二② 那写什么 | 写**一条最小配置校验**：新增 `MS_UPSTREAM_VENDOR_HOSTS`（部署方随 vendor 一起声明的允许主机名后缀），启动期不匹配 ⇒ **拒绝启动**；服务侧、启动期、落在既有 `model_validator` 内 |
| 二③ 残余风险 | **必须写进协议**（新增 **§4.3.1 出口真实性**），并 capabilities 自曝 `egress_verified`；明文写明**红线5 对真实出口的保证是流程性的、出口治理与 deny 调整属用户/运营权限** |

**与 A 无实质分歧**：A 自报的两条事实我都**独立复现成立**，且 A 本人没有下与我相反的结论（A 明写"那是协议决策，未做""请架构师裁"）。唯一可能与 A 不同的是 **一①**（派单给的选项之一是"把定义改成首个流式事件"，我判**不改定义、改实现**）——若 A 主张改定义，那是第 2 轮，仍在三轮内。**⇒ 不需上报用户。**

---

## 一、reasoning 与「首 token」预算

### 1.1 事实核定（六条，全部本人独立核实／复现，非读 A 的回执）

**F1 「首 token 超时」量的确实是首个对外事件，而 reasoning-only chunk 产出 0 个对外事件。**
`provider.py` 的 `LiteLLMProvider.stream` 只在 `delta.get("content")` 为真时 `yield {"kind":"chunk"}`；`main.py` 的 `_handle_stream` 用 `asyncio.wait_for(agen.__anext__(), timeout=settings.upstream_first_token_timeout)` 等**第一个事件**。
**离线结构性复现**（快照内用 stub litellm 喂 2 个 reasoning-only chunk + 1 个 content chunk + 1 个收尾 chunk）：

```
事件序列 = ['chunk', 'finish', 'usage', 'terminate', 'done']
首个 chunk 事件之前，上游已吐出 2 个 reasoning-only chunk ⇒ 期间对外事件数 = 0
```

⇒ A 的实测成立，且这是**结构性**的（不依赖真上游、不依赖凭证，可离线钉死）。15s 预算的实际含义＝「整段推理 ＋ 首个正文 token」。

**F2 服务侧从不发心跳，且没有发注释行的通路。**
`msvc/` 全目录对 `keepalive` 只有 **`provider.py:50 keepalive_during_silence: bool = False`** 一处，且是**声明但零使用的死字段**（grep 全仓仅此一行命中 model-service 侧）；`_sse()`（`main.py:75-78`）只能产出 `event:`／`data:` 行：

```python
def _sse(obj, event=None) -> str:
    prefix = f"event: {event}\n" if event else ""
    payload = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return f"{prefix}data: {payload}\n\n"
```

**F3 平台侧的心跳逻辑已实现且被两条用例锁住——但只被平台自己的桩触发过。**
`remote.py:578-579` `if line.startswith(":"): continue  # keepalive 注释行，平台必须忽略（§6.4）`；`server/tests/fake_model_service.py:396-397/433-434` 会发 `: keepalive\n\n`；`test_model_service_contract.py:842 test_p1_2_keepalive_resets_idle_timer`、`:1130 test_p1_6_absolute_cap_terminates_despite_keepalive`。
⇒ **§6-4 的心跳条款在生产路径上是死的**：真实服务永不发，平台的"心跳重置空闲计时"永不被真实流量触发，而契约测试跑的是平台自己的桩。这是 **§18.2-F1「两端各自跑自己的桩」的第三次出现**（前两次：缺口 N 的 `finish_reason`、缺口 O 的流式错误语义）。**F2+F3 合起来才是问题②的真答案**：不是"要不要引入心跳"，而是"契约里已有、平台已实现、服务侧从未实现"。

**F4 504 入窗，且成功与错误共用同一个 TTL。**
`idempotency.py:173 should_cache(504, retryable=False, reached_upstream=True) == True`（"结果不明必入窗"）；`main.py:253`（成功流式终态）与 `main.py:299`（`_finish_window` 的错误/非流式）**都是** `time.monotonic() + window.window_seconds`，默认 `idempotency_window_seconds=600` ⇒ **一次首事件超时把同指纹锁死 10 分钟**，与 §16.2 同型（那次是回放缓存的 429）。

**F5 前端对注释行天然安全（按实际分支核实，不是按派单那句话）。**
`git show cdda16a:web/lib/api.ts` 的 `handleFrame`（**行 238-239**）：

```ts
const line = frame.trim();
if (!line.startsWith("data: ")) return false;
```

只认 `data: ` 行，其余（含 `: keepalive`、`event: model_service`）一律 `return false` 静默忽略 ⇒ **不会当异常、不会污染正文、前端零改动**。同文件 215 行的注释也已自陈"本实现只认 `data: ` 行，天然满足"。

**F6 平台不向自己的客户端发心跳。**
`server/app/api/`＋`server/app/services/` 对 `yield ":`／`: keepalive` **零命中**；`deploy/` 下也没有反代（只有 `astrbot/` 与 `docker-compose.yml`），故仓库内无 `proxy_read_timeout` 一类的沉默杀手。
⇒ 服务发心跳**只保护「平台↔服务」这一跳**；「客户端↔平台」这一跳在整个推理期仍是沉默，其容忍度取决于 AstrBot/QQ 自己的 HTTP 客户端与仓库外的基础设施。

### 1.2 裁决①：定义**不改**，改实现让它名副其实 ＋ **新增第二项预算**

**裁定**：§8.1 表里「上游首 token 超时」的字面语义（**上游**首 token）是**正确的**，不要改成「首个流式事件」。错的是**实现**——它把"上游首 token"实现成了"首个对外事件"，而 reasoning-only chunk 被 provider 丢掉，于是两者相差整段推理时长。

**为什么不采纳"改定义为首个流式事件"**：那等于把缺陷写进契约。改完之后，"上游连上了但完全不吐字"与"上游正在推理"就**共用同一个预算**，而这两件事的运维含义完全不同（前者要早失败、后者要等）；而且一旦按 1.3 引入心跳，"首个流式事件"会变成**服务自己发出的心跳**——预算就彻底失去探测上游的意义（自己发的心跳永远秒到）。**定义要盯住上游，不能盯住自己的输出。**

**改法**：
1. **上游任何 chunk（含 reasoning-only）都复位首 token 计时**。实现上即 provider 在收到不产出正文的 chunk 时 yield 一个事件（见 1.3 的 `{"kind":"keepalive"}`），`main.py` 的 `wait_for(agen.__anext__(), …)` 自然被复位。
2. **新增第二项预算：「首个正文 token 超时」**（含推理时长），触发 → 504，`error.code` 与首 token 超时**可区分**（建议 `upstream_reasoning_timeout`，与 O1 的 `upstream_chunk_timeout` 同族做法——归因区分是本项目已付过学费的原则）。理由：第 1 点修好后，原预算不再保护"上游一直推理、始终不出正文"，而这个场景真实存在（`max_tokens` 被 reasoning 吃光时上游可以推理很久）。
3. **两项都必须在 capabilities `features.timeouts` 暴露**（§8.1-2 已钉死规范位置＝`features.timeouts`），并纳入红线8 的启动断言清单。**断言必须"缺键即失败"**，不得沿用"字段缺失即跳过"——那正是 P2-2 名义闭合实则空转的原因（§8.1 硬约束2 的 ⚠️ 段已记）。

⚠️ **一条派单与 A 都未提、但改预算时必须一起理清的连带影响**：
一旦服务开始发心跳，平台侧「流式空闲上限 90s」就**永远不会触发**（心跳持续复位它），此时真正的外层边界变成「流式绝对上限 180s」（§8.1 表明写"心跳不可重置"）。⇒ **§8.1 硬约束2 的"三个预算逐项断言 < 平台流式空闲上限 90s"对标错了对象**：对**会发心跳的服务**，内层预算应对标 **180s 绝对上限**。这条不改，会出现两种误判——内层设 120s（合法，因为 < 180s）却被断言拦下；或断言通过但流实际被 180s 砍断而归因成上游超时。**建议把断言口径改成"内层预算 < 平台绝对上限，且若服务不发心跳则还须 < 空闲上限"**，并把"是否发心跳"做成 capabilities 的一个声明位（见 1.3 判据）。

**判据（可红可绿）**：
- **正向**：服务侧用例（stub 上游先吐 N 个 reasoning-only chunk、每个间隔 < 首 token 预算、**累计 > 首 token 预算**，再吐正文）⇒ **不得 504**，且客户端拿到完整正文。把实现改回"只在 `delta.content` 为真时 yield"（复位失效）⇒ **变红**。（我已在快照里用 stub litellm 跑通这个事件形状，可直接转成用例，见 §四。）
- **反向**：stub 上游只吐 reasoning、始终不出正文，超过「首个正文 token 预算」⇒ **必须 504**，且 `error.code` 与首 token 超时**不同**。不实现新预算 ⇒ 变红（两条例案必须都有，只测一条会写出另一侧的一刀切——P1-7/P1-9 的教训）。
- **capabilities**：`features.timeouts` 含新键；平台启动断言清单含新键且**缺键即失败**（把断言改成"缺键跳过"⇒ 该用例变红）。

### 1.3 裁决②：**要发心跳**；本轮只做内层；同批修掉桩分歧

**裁定**：**要求服务侧在推理期间发 `: keepalive\n\n`**。
- **触发条件**：收到一个**不产出正文**的上游 chunk（reasoning-only）时发一行——**不必按固定周期**。这样心跳密度天然跟随上游产出节奏，也顺带满足 1.2-1 的复位要求（同一个事件两用）。
- **条文归属**：**§6-4**（把"允许 `: keepalive` 注释行"升级为"**reasoning 型上游在推理期间必须发**"），并写明**发送方＝模型服务**。
- **实现成本极低**：`_sse()` 不支持注释行，但流生成器 yield 的就是**字符串**（`_replay_stream`/`event_gen` 都是这个形状），直接 `yield ": keepalive\n\n"` 即可；provider 侧把 reasoning-only chunk 转成 `{"kind":"keepalive"}` 事件，`main.py` 的 `event_gen` 里加一个分支。**注意：心跳不得进 `StreamTerminal`**（否则会被 §8.2.2-3 的终态重放缓存并重放，污染 `idempotency_cache_max_chars` 的体积核算）。

**兼容面是否同步 —— 精确答案**：两个对外面都经平台转发，而平台现状是 `remote.py:579` **吞掉**注释行（F6）⇒ 心跳只保护内层跳。是否让平台**透传或自发**心跳属**对外字节流变更**，**必须走 `AGENTS.md`「三」通告**，不能只在协议里写。
- 前端：已按实际分支核实（F5），`api.ts:238-239` 天然忽略 ⇒ **零改动**。
- 第三方（AstrBot/QQ）：SSE 规范要求忽略以 `:` 开头的注释行，风险低但**非零**（手写解析器可能把未知行当错误）。⇒ 通告里要写明"新增注释行、按 SSE 规范必须忽略"，并给一个版本周期观察期。
- **我的建议：本轮只做内层**（服务发、平台仍吞）。理由：内层改动**零对外风险**，且已经解决 504 的根因（首 token 计时被复位）与平台空闲上限被误触发；对外透传需要前端与第三方各自确认，混在一批会把零风险改动拖进有风险改动的节奏里。**但要把"外层跳在推理期沉默"登记为已知残余风险**（判据：F6 的零命中），别让它无声消失。

**必须同批修的桩分歧（F2+F3）**：`msvc/` 的 fake 要能发心跳（把死字段 `provider.py:50 keepalive_during_silence` 接上，或新增 scenario），且**服务侧契约测试要有一条用例断言"推理期间对外至少出现一行注释行"**。否则这条防线只被平台自己的桩看守＝没看守（§18.2-F1 已付过两次学费）。

**判据**：
- 服务侧用例**必须用真 uvicorn 子进程**，不能用 httpx `ASGITransport`——A 已实测 ASGITransport 会攒完整个响应体才返回、测不出 chunk 间时序（"服务首事件 == 总时长"即为症状）。断言：上游只吐 reasoning-only chunk 持续 N 秒 ⇒ 客户端在该期间**收到 ≥1 行以 `:` 开头的注释行**，且**不收到任何 `data:` 正文**；删掉心跳 yield ⇒ **变红**。
- capabilities 增声明位（如 `features.stream_keepalive: true`），平台启动断言据此决定内层预算对标 90s 还是 180s（见 1.2 的 ⚠️）。声明位缺失 ⇒ 断言按**保守侧**（对标 90s）判定。

### 1.4 裁决③：**不给 reasoning 型上游开入窗例外**，改为 TTL 分类

**裁定：不开例外。** 两条理由：
1. **"是否 reasoning 型上游"不是服务侧可靠可判的输入**：同一 model 有时推理有时不推理，且上游在超时那一刻**不会告诉我们**它正在推理。把它当入窗判据等于给 §8.2.1 加**第三维**——而这张表已经因为维度不清错过两次（§16.2「不该入窗的入了」、§19.2「该入窗的没入」）。
2. **例外会同时放掉真双烧保护**：首事件超时时上游**很可能已经在生成并计费**（reasoning 型尤其如此，A 实测 reasoning 106–194 tokens），这正是"结果不明必入窗"要保护的场景。

**改为：入窗照入，TTL 分类。**
- 成功响应仍用 `idempotency_window_seconds`（600s）——它的目的是**回放正确答案**，需要长窗口。
- **「结果不明」类**（首 token 超时、总超时、已发起调用后的 5xx）用**独立短 TTL**：新增 `MS_IDEMPOTENCY_ERROR_TTL_SECONDS`，建议默认 **60s**。依据：这类条目入窗的目的是**防双烧**，不是回放答案；客户端的抖动重发集中在**秒级**，60s 足以吸收，而 600s 把一次慢推理变成同一条消息的**窗口级锁死**（F4）。
- 实现落点很小：`_finish_window`（`main.py:283-299`）与 `_cache_stream_terminal`（`:253`）按 `should_cache` 的分支选 TTL 即可，`IdempotencyWindow` 的 `expires_at` 字段已经是逐条的。

**交叉引用（放大项，第二次登记）**：回放在平台侧**不可识别**——`Idempotency-Replayed` 一类标记全仓 **0 命中**（实测 grep `model-service/`＋`server/app/`）。所以每次回放 504 都会让平台**再计一次熔断**（`_CODE_TO_PLATFORM` 里 `upstream_timeout` 第三位＝计数，`model_service_cb_threshold=5`，`server/app/config.py:58`）⇒ **同一条消息重发 5 次即打开熔断、全站降级**。短 TTL 把这个放大窗口从 600s 压到 60s，但**没有消除它**。这条我在 §26 会签（commit `0c294b9`）里已提过、至今未落，此处**第二次登记**，建议与本轮同批做（它与 1.4 改的是同一段 `_finish_window`）。

**判据**：
- 用例：首事件超时 → 504 入窗；**同 key 在 TTL 内重发 → 回放 504 且 `upstream.started` 不增加**；**同 key 在 TTL 外重发 → 真实重调（`upstream.started` +1）**。把错误类 TTL 改回 `window_seconds` ⇒ **第二条变红**。
- A 已有的 `test_b_consequence_first_token_timeout_..._replays_504` 锁的是"TTL 内回放"这半边，**缺 TTL 外那半边**（双向用例原则）。
- 熔断放大：桩返回带重放标记的 504 ⇒ 平台**熔断失败计数不增加**；不带标记 ⇒ **+1**（两条都要，防一刀切）。

---

## 二、`MS_UPSTREAM_VENDOR`：对账的是「声明」，不是「实际出口」

### 2.1 事实核定（独立复现，非读 A 的回执）

快照内构造 A 报的那组 env：

```
MS_UPSTREAM=litellm  MS_UPSTREAM_VENDOR=xiaomimimo
MS_LITELLM_API_BASE=https://api.openai.com/v1  MS_LITELLM_PROVIDER=openai
```

实测输出：

```
A1 Settings 构造成功 ⇒ 未拒启动
A2 vendor = 'xiaomimimo' | deny = ['anthropic', 'openai'] | api_base = https://api.openai.com/v1
```

⇒ **失明独立复现成立**：`api_base` 指向 `api.openai.com`、`deny` 里就有 `openai`，而启动期对账**放行**。

实现位置：`cdda16a` 新增的 `msvc/config.py` 的 `_reconcile_upstream_vendor_with_compliance`（`model_validator(mode="after")`），只比对 `vendor ∈ deny_providers`。**它自己的 docstring 就写明了这个缺口**：「真实出口由 env（`MS_LITELLM_API_BASE`）决定，那里**从不**被合规检查读到」，而报错文案还建议「或改 `MS_LITELLM_API_BASE` 换出口」——**那条路径正是无人校验的那条**。⇒ A 的自报准确，没有粉饰。

### 2.2 裁决①：**不把「按 `api_base` 主机名判定 deny」写进协议**

三条理由：

1. **主机名 → 供应商的映射是一张永远不完整的真值表**。第三方聚合网关、企业出口 NAT/正向代理、境内镜像与加速域名、上游改名换域，都会让它错判。把合规判定建在这张表上会同时产生**假阴性**（真出境判成合规）与**假阳性**（境内镜像判成出境），而**假阴性比现在的失明更危险**——它给出"已封死"的错觉，让人停止审计。现在的失明至少是**已知**的。
2. **一致性检查在逻辑上无法自证**。`vendor` 与 `api_base` **都是部署方自己填的字符串**。没有真值表，就只能比对两个自填值 ⇒ 检查退化为"部署方有没有把两个 env 填得自相矛盾"，那是**配置校验**，不是**合规控制**。把它写成合规条款会**高估**红线5 的保证强度——这正是 2.4 要防的。
3. **红线5 的现有语义是"请求级意图不得放宽部署级策略"**（§4.3：`routing.allow_providers` 放宽不了合规策略）。出口治理是**另一个层次**的问题（部署级配置的真伪），塞进同一条红线会让两边都变模糊。

### 2.3 裁决②：但要写**一条最小配置校验**（服务侧 / 启动期 / fail-fast）

配置校验虽不等于合规控制，却能挡住 A 实测到的那个**最常见误配**（改了 `api_base` 忘了改 `vendor`），把**静默失明变成启动期的响亮失败**。设计要点是**不引入全局真值表**——映射由部署方随 vendor 一起声明，**谁声明谁举证**：

- 新增 **`MS_UPSTREAM_VENDOR_HOSTS`**：逗号分隔的**主机名后缀允许列表**，语义＝"**该 vendor 的合法出口主机**"。
- `upstream == "litellm"` 且该 env **非空**时：解析 `MS_LITELLM_API_BASE` 的主机名，**不匹配任一后缀 ⇒ 拒绝启动**；报错须同时打印 `vendor`、`api_base`、允许列表三者（否则运维看不出该改哪个）。
- 该 env **未声明 ⇒ 只 WARNING**（与 vendor 未声明同口径，避免打挂旧部署），但 WARNING 文案必须写明「**出口未经主机名校验**」。
- **落点**：`msvc/config.py` 里**同一个** `_reconcile_upstream_vendor_with_compliance`（不新增执行点、不新增请求期开销），**服务侧、启动期**。
- **不做请求期判定**：出口在进程生命周期内不变，请求期每次解析 URL 是纯开销；且请求期 403 会被误读成上游问题（A 在 vendor 断言的 docstring 里已给出同一理由，我同意）。

**判据（可红可绿，就是 A 实测那组 env）**：

| env | 期望 |
|---|---|
| `MS_UPSTREAM_VENDOR=xiaomimimo` + `MS_UPSTREAM_VENDOR_HOSTS=api.xiaomimimo.com` + `MS_LITELLM_API_BASE=https://api.openai.com/v1` | **拒绝启动**，报错含三个值 |
| 同上但 `MS_LITELLM_API_BASE=https://api.xiaomimimo.com/v1` | **启动成功** |
| `MS_UPSTREAM_VENDOR_HOSTS` 未设 + `api_base=api.openai.com` | **启动成功但打 WARNING**（旧部署兼容） |

删掉这条校验 ⇒ 第一行用例**变绿即变红**（用例断言 `pytest.raises`）。

### 2.4 裁决③：**残余风险必须写进协议**（不能只留在提交信息里）

- **落点建议：§4.3 新增 §4.3.1「出口真实性（egress attestation）」**（红线5 的老家）。三句话：
  1. `MS_UPSTREAM_VENDOR` 的语义＝**部署方对真实出口的声明；其真实性由部署流程与审计保证，不由代码保证**；
  2. 服务侧只做**最小一致性校验**（2.3），它挡的是**误配**，**挡不住蓄意错填**——部署方完全可以把 `api.openai.com` 写进 `xiaomimimo` 的允许列表；
  3. 因此 **A-1 之后红线5 对"真实出口"的保证是流程性的**；调整 `compliance_deny_providers` 与出口治理**属用户/运营权限**（与 §26.3 的"最后一张牌"同一归属）。
- **同时让服务如实自曝**：capabilities 里加 `egress_verified: bool`（＝本次启动是否真的做了 2.3 的主机名校验），放在既有 `features.compliance_policies` 旁边。**平台启动自检读到 `false` ⇒ WARNING 并记入启动日志**（**不拒启动**，避免打挂既有部署；落点 `server/app/llm/model_service_ops.py` 的自检清单）。
  ⇒ 效果：把"失明"从**默认静默**变成**每次启动都会被打印一次的状态**，且平台侧也留了痕。
- **判据**：capabilities 响应含 `egress_verified`；未声明 `MS_UPSTREAM_VENDOR_HOSTS` 时为 `false`、声明且匹配时为 `true`；平台自检用例断言"读到 `false` 必打 WARNING"（改坏 ⇒ 变红）。

---

## 三、给 A 的落文建议（我不抢文，协议孤本归 A）

| 条文 | 改什么 |
|---|---|
| **§6-4** | 心跳从"允许"升为"**reasoning 型上游在推理期间必须发**"；写明发送方＝模型服务；补一句"**心跳不得进终态缓存**"（§8.2.2-3 交叉） |
| **§8.1 表** | 「上游首 token 超时」补定义＝**上游首个 chunk（含 reasoning-only）**；**新增一行**「首个正文 token 超时」；硬约束2 的断言清单加新键、并**改为"缺键即失败"**；⚠️ 段补「**会发心跳的服务，内层预算对标 180s 绝对上限而非 90s 空闲上限**」 |
| **§8.2.1** | 补 **TTL 分类**（成功 600s / 结果不明 60s，新配置名）；并明文写"**不给 reasoning 型上游开入窗例外**"及其两条理由（1.4） |
| **§4.3.1（新增）** | 出口真实性 ＋ 最小一致性校验（`MS_UPSTREAM_VENDOR_HOSTS`）＋ 残余风险归属用户/运营 ＋ capabilities `egress_verified` |
| **工单** | 立 5 项：服务侧 3（首 token 复位＋新预算、心跳发送＋fake 接通、TTL 分类＋vendor hosts 校验）、平台侧 2（capabilities 断言清单加键＋缺键即失败、`egress_verified` 的 WARNING）；并把「**回放不可识别 ⇒ 重复计熔断**」第二次登记（首次见 `0c294b9`） |

**建议的实施顺序**：1.2/1.3（同一处 `provider.stream` + `event_gen`，必须同批）→ 1.4 与熔断标记（同一处 `_finish_window`，同批）→ 2.3/2.4（`config.py` + capabilities，独立）。三批互不覆盖。

---

## 四、可复跑判据汇总

抽取快照（**不要在共享树里跑**；`model-service/**` 此刻在 G 侧租约内）：

```bash
TMP=$(mktemp -d); git archive cdda16a model-service | tar -x -C "$TMP"; cd "$TMP/model-service"
PY=<repo>/server/.venv/Scripts/python.exe     # 裸 python 缺 starlette，会误判成"实现坏了"
export PYTHONIOENCODING=utf-8 LITELLM_LOCAL_MODEL_COST_MAP=True
```

**F1 的离线复现（可直接转成 1.2 的正向用例）**——用 stub 顶掉 litellm，喂 reasoning-only chunk：

```python
import sys, asyncio, types; sys.path.insert(0, ".")
chunks = [
  {"model":"mimo","choices":[{"delta":{"reasoning_content":"想"*40},"finish_reason":None}]},
  {"model":"mimo","choices":[{"delta":{"reasoning_content":"想"*40},"finish_reason":None}]},
  {"model":"mimo","choices":[{"delta":{"content":"正文"},"finish_reason":None}]},
  {"model":"mimo","choices":[{"delta":{},"finish_reason":"stop"}],
   "usage":{"prompt_tokens":10,"completion_tokens":20,
            "completion_tokens_details":{"reasoning_tokens":15}}},
]
stub = types.ModuleType("litellm")
async def _ac(**kw):
    async def _g():
        for c in chunks: yield c
    return _g()
stub.acompletion = _ac
stub.cost_per_token = lambda **kw: (_ for _ in ()).throw(Exception("no price"))
sys.modules["litellm"] = stub
import msvc.provider as prov
async def run():
    return [ev.get("kind") async for ev in prov.LiteLLMProvider().stream(
        {"model":"mimo","messages":[]}, prov.Scenario())]
print(asyncio.run(run()))   # 实测 → ['chunk','finish','usage','terminate','done']
                            # 前 2 个 reasoning-only chunk ⇒ 0 个对外事件（F1）
```

**2.1 的失明复现**：

```python
import os, sys; sys.path.insert(0, ".")
os.environ.update({"MS_UPSTREAM":"litellm","MS_UPSTREAM_VENDOR":"xiaomimimo",
                   "MS_LITELLM_API_BASE":"https://api.openai.com/v1",
                   "MS_LITELLM_PROVIDER":"openai"})
from msvc.config import Settings
s = Settings(); print("未拒启动:", s.upstream_vendor, s.litellm_api_base, sorted(s.deny_providers))
```

**静态判据（grep）**：

| 结论 | 命令 |
|---|---|
| F2 服务侧无心跳、`keepalive_during_silence` 是死字段 | `grep -rn keepalive model-service/` → 仅 `provider.py:50` 一行 |
| F2 `_sse` 无注释行通路 | `main.py:75-78` |
| F3 平台已实现且用例锁住 | `remote.py:578-579`；`server/tests/fake_model_service.py:396-397/433-434`；`test_model_service_contract.py:842/1130` |
| F4 单一 TTL | `main.py:253` 与 `:299` 同为 `+ window.window_seconds` |
| F5 前端天然忽略注释行 | `git show cdda16a:web/lib/api.ts` 行 **238-239** |
| F6 平台不发对外心跳 | `grep -rn 'yield ":' server/app/api server/app/services` → 0 命中 |
| 1.4 回放不可识别 | `grep -rn "Replayed\|replayed" model-service/msvc server/app` → 0 命中（只有测试名里的 `replayed`） |
| 2.1 断言只查 vendor | `git show cdda16a:model-service/msvc/config.py` 的 `_reconcile_upstream_vendor_with_compliance` |

---

## 五、是否有分歧 / 是否需上报用户

- **与 A 无实质分歧**。A 自报的两条事实（首事件预算语义、vendor 失明）我都**独立复现成立**；A 本人对两条都没有下结论（明写"那是协议决策，未做""请架构师裁"），故不存在对立立场。
- 本裁决中唯一可能与 A 不同的是 **1.2**：派单给的选项之一是"把定义改成首个流式事件"，我判**不改定义、改实现＋新增第二项预算**（理由见 1.2：改定义会让预算去盯自己的输出，引入心跳后彻底失效）。**若 A 主张改定义，那是第 2 轮，仍在三轮内。**
- **1.4 的短 TTL** 与 **2.3 的主机名允许列表** 都是**新增配置项**（加法），若 A 认为超出本轮范围可降级为工单项；但 **2.4 的残余风险条文不能降级**——它只是把一个**已存在且已被 A 实测**的事实写进协议，不写就等于让它只活在提交信息里（派单③的要求）。
- **⇒ 不需上报用户。** 两项都属协议语义、A+B 权限内。唯一属于用户的是本来就该属于用户的那部分：**出口治理与 `compliance_deny_providers` 的调整权**（2.4 第 3 句已把它显式留给用户/运营）。

---

## 附：本单之外，一条指向架构师但本单无权处理的待办

`AGENTS.md`「三」新增了一条 **`[ ]` 未勾**的条目（2026-10-12，后端登记）：422 校验错误的 `detail` 在原生面是**数组**、在兼容面被 `str()` 成 Python 字面量，并指出「五」里那句「错误响应统一 FastAPI `{"detail": "..."}`」**在 422 下不成立**，明写"提请架构师/前端确认后一起改措辞"。
**本单未处理**：① 派单硬约束禁止我改 `AGENTS.md`；② 我的租约 `paths` 只有 `harness/proposals/`；③「五」是双方共同契约，改动需两侧确认。
⇒ 请管家**另立一单**（需授权改 `AGENTS.md`「五」，或经总线与 G 侧约定措辞由起草方落）。我的**预备意见**（供该单参考，非本单结论）：该条实测形状可信（后端给了逐条本机实测），「五」应改为「错误响应统一 FastAPI `{"detail": "..."}`；**422 校验错误例外——原生面 `detail` 为数组、兼容面为 `str()` 后的字面量，机器不可解析**」。其中兼容面那条本身是**缺陷**（Python 字面量不是 JSON），建议同时立一个后端工单把它改成 OpenAI 兼容的 `error.param`/`code` 结构化形状。
