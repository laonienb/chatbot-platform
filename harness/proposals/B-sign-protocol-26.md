# 架构师 B 会签意见：协议 §26（甲 / 丙 / 乙）

- 出具方：**max-架构师-B**（C 侧唯一架构师，`981f5ce1-6cb8-40f9-9a8c-b76cf0010582`）
- 派单：工作区管家-C，存证 `harness/proposals/C-ask-arch-B-protocol-26-sign.md`；租约 `harness/bridge/leases/C/B-review-protocol-26.json`（paths=`harness/proposals/`，ttl 3600s @22:46:54）
- 回复目标：G 侧总线 `1791729777000-G`
- **会签基准**：`git show a3624fb:docs/model-service-protocol.md` 行 **1529–1572**（§26 全五小节）。代码判据取 HEAD `8a2616a`；已核 `git diff a3624fb HEAD --stat` **只有 `harness/proposals/`**，故代码与 `a3624fb` 同一状态。
- **复核方式**：冻结快照（`git archive HEAD model-service` → 临时目录）+ 项目自带解释器 `server/.venv/Scripts/python.exe` 实跑探针。**共享工作树零写入**（结束时 `git status --porcelain` 为空，快照已删）。未读、未碰任何人的未提交改动（开工时工作树本就干净）。
- 出具时刻：2026-10-11 22:5x+08:00

## 总结论

**三项均同意 A 的立场，无分歧**（按用户规则不需上报）。但丙项 A-1 有**两个 A 未覆盖的落地问题**——其中一个是**静默错账的缺陷，必须与 A-1 同批修**；乙项的冒烟规格要补两条测量项，且工单该记的不是"1.5× 余量"而是后果链。

| 项 | A 立场 | B 结论 |
|---|---|---|
| 甲（§26.2） | 不透传 `reasoning_content`，留可选开关 | ✅ **同意**（a 同意 / b 同意但理由要换 / c **不预留字段位**） |
| 丙（§26.3） | A-1 不构成红线5 放宽 | ✅ **同意定性**；⚠️ 但 A-1 落点清单**缺两项**（2.2 目录 404、2.3 provider 归因串号），另建议一条补偿控制（2.4） |
| 乙（§26.4） | litellm 层已完成、服务级待 A-1、不需用户裁决 | ✅ **同意定性**；冒烟归后端且与 A-1 同批；工单要记后果链 + **必须量总时长**（3.3） |

---

## 一、甲（§26.2）：同意「不透传 `reasoning_content`」

### a) 同意「只转 `content`」——补一条 A 没给的、最硬的理由

A 的三条理由（污染既有解析器 / 内部推理不宜直给 / 无产品需求）我都确认成立，其中①可加实测支撑：`AGENTS.md`「三」已把对外流式形态钉成 OpenAI chunk，前端宽松解析只认 `choices[0].delta`。但真正让"不透传"成为**安全默认**的是下面两条，建议写进条文理由栏：

1. **`reasoning_content` 全仓零消费者**（实测 grep `server/app` + `web/lib` + `web/app` + `model-service/msvc` → **0 命中**）。⇒ 现状本来就是"不透传"；透传才是**要新做的工作**，且必须同时改两个对外面（原生面 + 兼容面）与前端解析器。把"不做"当默认，不需要任何迁移。
2. **计量与计价早已是 reasoning-aware，藏文本不藏账**——这是"不透传不损失任何东西"的关键论证：
   - `server/app/llm/remote.py:267` 已解析 `usage.completion_tokens_details.reasoning_tokens`；
   - → `StreamDone.reasoning_tokens`（`gateway.py:48/73`）→ `ledger.py:132/159/198` 落 `usage_logs.reasoning_tokens` 列；
   - → `billing/rating.py:214-215`：费率规则给了 reasoning 单价（`r_reason != r_out`）时**按差价补收**；
   - `billing/pricing.py:122` 记明 litellm 口径下 `completion_tokens` **已含** reasoning tokens，故不重复计价。
   - A 的 §26.1 litellm 层实测也确认了真实上游**会给** `completion_tokens_details.reasoning_tokens` ⇒ 这条链路接真上游后是活的。

   ⇒ **不透传 reasoning 文本，一分钱的账都不会漏**。反过来说：若将来有人以"计费需要"为由主张透传，这条链路就是反证。

### b) 同意加 WARNING，但**理由要换成计费正当性**（比"可观测"硬）

A 给的理由是可观测。更强的理由是：**小 `max_tokens` 被 reasoning 吃光时，`content=""` 而 `completion_tokens` 仍包含 reasoning tokens ⇒ 用户为一段空正文付了推理 token 的钱**。没有告警，这类请求在账本里与正常请求**长得完全一样**（`status=settled`、token 数正常），客诉来了无从定位；而 §26.1 已实测这个陷阱真实可触发（`max_tokens=8` → content 空 + `finish_reason=length`）。

- **位置建议**：不放 §6.6（那是"收尾 chunk 必须带 `finish_reason`"的形态条款，语义不搭）。放 **§6.7 之后**或 **§5 的 usage 段**更贴切——它是一条"用量与正文不一致"的告警义务。
- **内容约束**：WARNING 可含 `model`、`finish_reason`、请求里的 `max_tokens`、`completion_tokens`/`reasoning_tokens` 计数；**不得含消息内容与任何业务身份**（红线1 精神：服务侧永不认识用户）。
- **平台侧建议维持 A 的定性**：`max_tokens` 下限保护属**平台策略**，不进模型服务契约（我同意，且这与"周期额度是全局配置"那类既有口径一致——服务侧不做业务策略）。
- **判据**：fake 桩造 `content=""` + `finish_reason="length"` 的响应 → 服务日志出现该 WARNING；不实现则日志无此行（可红可绿，不是措辞）。

### c) **不预留字段位**，留到真有需求时再加

决定性证据在 `msvc/guards.py`：入站守卫是**黑名单式**（`FORBIDDEN_TOP_FIELDS` / `FORBIDDEN_MESSAGE_FIELDS` 之外一律放行），文件头明写「**宽松解析：未知扩展字段放行**」。

⇒ 将来新增一个请求级开关是**纯加法**：不破坏任何既有实现、不需要升线协议版本、不会被红线1 误拦。既然如此，**预留没有收益**。

而预留有**实际害处**：一个"协议里有、实现里没有"的字段位，正是 §18.2-F1（缺口 N）的同型温床——两侧桩各按自己的理解实现，测试全绿而生产静默失效。本项目已经为这种分歧付过一次代价。

**附带主张（若将来真要做）**：做**部署级 env 开关**（如 `MS_EXPOSE_REASONING`，默认 false），**不做请求级**。请求级会让同一部署对不同请求给出**不同的 chunk 形状**，第三方解析器（AstrBot/QQ）的兼容性变得不可预测、也不可测；部署级则整个部署内形状一致，可测可验收。

---

## 二、丙（§26.3）：同意「A-1 不构成红线5 放宽」，但有两个 A 未覆盖的漏洞

### 2.1 交叉确认：A 的定性成立（实测）

快照内直接调 `_check_compliance`（`msvc/main.py:139`），实测三例：

| 请求 model | 结果 |
|---|---|
| `mimo-v2.6-flash`（无前缀） | **放行**（`model_provider=""`，`"" not in deny`） |
| `openai/mimo-v2.6-flash` | **403 `compliance_denied`** |
| `mimo-v2.6-flash` + `routing.allow_providers=["openai"]` | **403 `compliance_denied`** |

- 判定对象仍是"请求里的 model 前缀 + routing 声明"，`compliance_deny_providers` 默认值（`openai,anthropic`）与判定语义**一字未改** ⇒ **不是放宽**。A 的定性我签。
- A 说的"平台若自行加 `openai/` 前缀仍会被拦＝期望行为"，实测确认（第 2 行 403）。
- A 对根因的定性（**协议名 vs 供应商名**的命名冲突，非政策冲突）成立：xiaomimimo 是境内供应商，正是红线5 要保护的方向。

### 2.2 漏洞一（🟡 **会让冒烟直接失败**，A-1 落点清单遗漏）：目录 404

`msvc/main.py:175`：

```python
if model and model not in settings.models and "/" not in model:
    raise ServiceError("model_not_found", ...)
```

默认 `MS_CATALOG_MODELS="deepseek-chat,qwen-plus"`，实测 `mimo-v2.6-flash` **不在目录内** ⇒ A-1 若只按 A 写的落点（`provider.py` 传 `custom_llm_provider` + `config.py` 新增 `MS_LITELLM_PROVIDER`）改，冒烟拿到的**不是 403 而是 404 `model_not_found`**，照样跑不通，而且报错会指向"模型不存在"，容易把人引去查上游而不是查目录。

注意这条与 2.1 的第 2 行是**同一处代码的两个分支**：带前缀的 model 因为 `"/" in model` **跳过**目录校验（然后被合规 403），无前缀的 model **进入**目录校验（然后 404）。A-1 把请求从前者搬到后者，就顺带把 403 换成了 404。

⇒ **A-1 落点清单必须补第三项**：`MS_CATALOG_MODELS` 含目标 model id（env 或默认值）。实测 `MS_CATALOG_MODELS="deepseek-chat,qwen-plus,mimo-v2.6-flash"` → `settings.models` 含该 id ✓。
（这属**配置**而非新增代码路径，与 A 的"若走配置路线，不新增其它路径"不冲突。）

**判据**：A-1 实施后补一条服务侧用例「目录内的无前缀 model 不得 404」；把 `MS_CATALOG_MODELS` 改回默认 → 该用例变红。

### 2.3 漏洞二（🔴 **静默错账，必须与 A-1 同批修**）：provider 归因会串到 deepseek

`model-service/msvc/provider.py:205`：

```python
"provider": used.split("/")[0] if "/" in used else get_settings().litellm_default_model.split("/")[0],
```

无前缀 model 走 **else 分支** ⇒ 实测 `LiteLLMProvider()._xms(requested_model="mimo-v2.6-flash", used_model="mimo-v2.6-flash", ...)` 返回 **`provider == 'deepseek'`**（取自 `litellm_default_model="deepseek/deepseek-chat"` 的前缀）。

而这个字段**会进账本**，链路已逐跳核实：

- `server/app/llm/remote.py:456` 与 `:668`：`provider=xms.get("provider")`
- → `server/app/billing/ledger.py:142` `svc_provider = getattr(done, "provider", None)`
- → `ledger.py:190-191`：`if svc_provider: entry.provider = svc_provider`

⇒ **A-1 落地后，xiaomimimo 的每一次调用都会在 `usage_logs.provider`、以及管理后台的毛利/供应商统计里被记成 `deepseek`**。这是静默的：对照组"带前缀 `deepseek/deepseek-chat`"同样得 `'deepseek'`，**巧合掩盖了 else 分支的错**，所以既有测试不会红。

这不是反对 A-1 的理由（A-1 的接法本身是对的），但**必须同批修**——否则"接一个境内新供应商"的代价是**污染既有供应商的成本归因**，而这恰好打在项目最在意的"成本真相"上。

**判据**：新增用例断言无前缀 model 的 `x_model_service.provider` == 声明的真实供应商（或至少 `!= "deepseek"`）；不改 `_xms` → 该用例变红。

**附带一条运维预期（非缺陷，但要写明）**：`cost_per_token(model="mimo-v2.6-flash")` 在 litellm 价格表里查不到 ⇒ 实测 `cost_status='unknown'`、`cost_usd=None`（红线2 的正确行为：绝不返回 0）。平台侧按 §5.1 优先级链回落价格表，大概率也查不到 ⇒ `cost_source=none` + `needs_review=True`。**接真上游后该模型的毛利必然是"未知/待复核"，这不是 bug**，工单里写一句，免得有人当缺陷查。

### 2.4 补偿控制（建议同批；若 A 认为超范围，则降级为工单项 + **记明残余风险**）

**问题**：A-1 之后，红线5 的**两条判定臂对平台真实流量都不再触发**——

- **model 前缀臂**：目录内 model 一律无前缀 ⇒ 恒不命中（2.1 第 1 行实测放行）；
- **`routing.allow_providers` 臂**：实测平台**根本不发 `routing`**——`grep -rn 'allow_providers\|"routing"' server/app/ --include='*.py'` → **0 命中** ⇒ 该臂对平台流量是死的（2.1 第 3 行证明它本身还能工作，只是没人喂它）。

而服务的**真实出口**由 env 决定（`MS_LITELLM_API_BASE`，`config.py:15`；加 A-1 拟新增的 `MS_LITELLM_PROVIDER`），`_check_compliance` **从不检查它**，也没有任何启动期校验覆盖（实测 `msvc/*.py` 内对 `api_base` 无校验，只有 `config.py` 的字段声明与 `provider.py:192` 的透传）。

⇒ **精确结论：A-1 没有放宽条文（我签），但它让红线5 对真实出口失明。**
**可复现判据（现在就能跑，不需要真调 OpenAI）**：设 `MS_LITELLM_API_BASE=https://api.openai.com/v1`、`MS_LITELLM_PROVIDER=openai`、请求用无前缀 model → 服务**照常启动**、`_check_compliance` **放行**。也就是说，把这套配置指向真 OpenAI，红线5 一个字都拦不住。

**建议（一个字段解 2.3 与 2.4 两个问题，且不碰 `_check_compliance` 的语义 ⇒ 与 A 对 A-2 的"慎"不冲突）**：

1. `config.py` 新增 **`MS_UPSTREAM_VENDOR`**（**供应商名**），与 `MS_LITELLM_PROVIDER`（**协议名**）**分开**——这正是 A 自己诊断出的"协议名 vs 供应商名"区分，把它显式建模进配置，而不是靠"请求里不写前缀"来隐式绕过；
2. **启动期断言**（不是请求期）：`upstream == "litellm"` 且 `MS_UPSTREAM_VENDOR ∈ compliance_deny_providers` → **拒绝启动**（红线5 的精神＝部署级策略不可被绕过；红线7 已有 native 模式 fail-fast 的先例，`model_service_ops.py:102-103`）；vendor 未声明 → **WARNING**；
3. `provider.py:_xms` 的 `provider` 改取 `MS_UPSTREAM_VENDOR` ⇒ **顺带修掉 2.3 的错账**。

这是**纯加法**：请求级判定一字不改，只多一个部署级判定项。落点仍在 A 允许的 `provider.py` + `config.py`（外加一处启动断言）。

**让路条款**：若 A 认为这超出本轮范围，可以降级为工单项、不阻塞 A-1；但**必须在工单里写明"A-1 之后红线5 对真实出口失明（判据见 2.4）"这条残余风险**，不要让它无声消失。丙的"是否调整 `compliance_deny_providers`"仍留用户——**我没有替用户定这张牌**。

---

## 三、乙（§26.4）：同意「不需用户裁决」，但冒烟规格要补

### 3.1 A 的定性正确

- litellm 层已实测跑通（`custom_llm_provider="openai"` + 无前缀 model + `api_base`）⇒ 集成可行性有据，不是纸面推断；
- 服务级冒烟受阻于**代码缺 `custom_llm_provider` 透传**（技术前提），**不是政策问题** ⇒ 确实不需用户裁决；
- A 没有把丙的"放宽"替用户定掉（仍列为最后一张牌）⇒ 分流正确，我确认。

### 3.2 服务级冒烟归谁做

**归后端，且应与 A-1 同一实现方、同一批做完。**理由：架构师只读复核，跑冒烟要设 env + 起服务，属实施动作；而 A-1 与服务级冒烟**都需要同一套临时 env 与同一次起服务**，分开派会重复搭环境、且两批人各按自己的理解设 env（本项目已为"两端各自跑自己的桩"付过代价，§18.2-F1）。

**派单前置（归管家，我不派）**：实测 `model-service/msvc/` 当前**无活跃租约**——`leases/G/` 只剩一条**已过期**的 docs 租约（`DW-status-fix.docs.json`，paths=`docs/model-service-work-order.md`，claimed_at 22:01:15、ttl_s 600，22:53 已超期），`leases/C/` 只有本会签任务的租约。⇒ 会签落地后，G 侧"甲项定下前本侧后端不进 `msvc/`"的让路请求即解除。但**派单前请再查一次 `leases/G/`**，别拿我这轮的读数当凭据（读数是 22:53 的快照）。

### 3.3 预算问题：同意不改默认值，但工单要记的是**后果链**，不是"1.5× 余量"

- **不改默认值我同意**：样本只有 4 个（httpx 0.5 / 4.5 / 9.8s、litellm 0.86s），不足以定阈值；改默认值是全局风险，应由部署侧 env 调。
- 但"仅 1.5× 余量"这个说法**低估了后果**。实测：

  ```
  should_cache(504, retryable=False, reached_upstream=True) == True     # 首 token 超时 → 入窗
  should_cache(429, retryable=True,  reached_upstream=True) == False    # 对照：上游瞬时拒绝 → 不入窗
  ```

  代码路径：`msvc/main.py:341-343`（首 token 超时 → `ServiceError("upstream_timeout", retryable=False)` → `_finish_window(..., reached_upstream=True)`）→ `idempotency.py:173 should_cache` 判"结果不明必入窗" → **缓存 504 达 `idempotency_window_seconds=600`**。

  配合兼容面指纹键的 **10 分钟固定桶**（`server/app/services/chat.py:489 _compat_idem_key`）⇒ **一次首 token 超时，会让同指纹的重发在窗口内一律拿到回放的 504，即使上游 1 秒后就恢复了**。这与 §16.2 是**同型故障**（那次是回放缓存的 429 把瞬时限速放大成窗口级），只是触发源换成了超时。

  **可复跑判据**：临时设 `MS_UPSTREAM_FIRST_TOKEN_TIMEOUT=0.5` 起服务 → 首次请求 504 → **同 key 重发仍 504（回放）**，此时把预算调回 15s 也不会恢复（要等窗口过期或重启进程）。这条能正面钉死"窗口级故障"而非"偶发慢"。

- ⇒ **工单该记三条，不是一条**：① 实测分布**与样本数**；② **超时 → 入窗 → 窗口级 504** 的后果链（含上面这条判据）；③ 运维可调项与建议值（冒烟时临时 `MS_UPSTREAM_FIRST_TOKEN_TIMEOUT=30`，仍满足红线8"内层 < 外层"：30 < 平台流式空闲上限 90s）。

- **附带约束（A 未提，会让"抬首 token"撞墙）**：若把首 token 抬到 30s，`upstream_total_timeout=60s` 只剩 30s 给生成 —— reasoning 模型的**总时长**很可能不够，而总超时同样入窗（非流式 `main.py:272→279`；流式中途停顿走 `upstream_chunk_timeout`，首字节后按 §8.2.2-1 入窗）。
  ⇒ **服务级冒烟必须同时量"总时长"分布，不能只量首 token**。A 的 §26.4 只写"跑一次即可"，没规定量什么；建议把测量项写死进工单：**首 token、总时长、content/reasoning 字符数、`finish_reason`、usage 是否含 `completion_tokens_details.reasoning_tokens`**，各 ≥5 次取样。

- **另一条实测到的耗时源（会直接吃首 token 预算）**：litellm 首次调用会尝试拉远端 cost map（`raw.githubusercontent.com`）。受限网络下本次实测打印：
  `LiteLLM: model cost map fetch attempt 1/3 failed (ConnectError ...); retrying in 2.6s` → 回落本地备份。
  设 `LITELLM_LOCAL_MODEL_COST_MAP=True` 后**该警告消失**（本次两轮对照已验：第一轮无该变量 → 有警告；第二轮 `=True` → 无）。
  A 在 §26.1 提到了这个变量，但落点写的是 `gateway.py`（平台侧 `LiteLLMBackend`）；**模型服务侧的 `LiteLLMProvider` 是另一处**，它不读平台的环境。⇒ **冒烟的临时 env 与 `deploy/.env.example` 的 model-service 段都应带上 `LITELLM_LOCAL_MODEL_COST_MAP=True`**，否则 3 次重试的等待会叠在 1.5× 余量上。

---

## 四、可复跑判据汇总

抽取快照（**不要在共享树里跑**，`pytest`/探针会生成 `__pycache__` 弄脏工作树）：

```bash
TMP=$(mktemp -d); git archive HEAD model-service | tar -x -C "$TMP"
cd "$TMP/model-service"
PY=<repo>/server/.venv/Scripts/python.exe      # 裸 python 缺 starlette，会误判成"实现坏了"
export PYTHONIOENCODING=utf-8 LITELLM_LOCAL_MODEL_COST_MAP=True
```

探针（本文所有实测数字都由它产出）：

```python
import sys; sys.path.insert(0, ".")
from msvc.config import get_settings
from msvc.main import _check_compliance
from msvc.errors import ServiceError
from msvc.provider import LiteLLMProvider
from msvc.idempotency import IdempotencyWindow
s = get_settings()
print("目录 =", s.models, "| deny =", s.deny_providers)          # ['deepseek-chat','qwen-plus'] | {'openai','anthropic'}
print("mimo 在目录内 =", "mimo-v2.6-flash" in s.models)          # False  → 2.2 的 404
for m, r in [("mimo-v2.6-flash", None), ("openai/mimo-v2.6-flash", None),
             ("mimo-v2.6-flash", {"allow_providers": ["openai"]})]:
    body = {"model": m} + ({"routing": r} if r else {})
    try: _check_compliance(body); print(m, r, "→ 放行")
    except ServiceError as e: print(m, r, "→ 403", e.code)       # 无前缀放行 / 两条 403
b = LiteLLMProvider()._xms(requested_model="mimo-v2.6-flash",
                           used_model="mimo-v2.6-flash", usage=None, latency_ms=1)
print("provider =", b["provider"], "cost_status =", b["cost_status"])   # 'deepseek' / unknown → 2.3
w = IdempotencyWindow()
print(w.should_cache(504, False, True), w.should_cache(429, True, True))  # True False → 3.3
```

静态判据（`grep`，本文各处已标行号）：

| 结论 | 命令 |
|---|---|
| `reasoning_content` 零消费者 | `grep -rn "reasoning_content" server/app web/lib web/app model-service/msvc` → 0 命中 |
| `reasoning_tokens` 全链路接通 | `grep -rn "reasoning_tokens" server/app` → `remote.py:267`、`ledger.py:132/159/198`、`rating.py:214-215`、`pricing.py:122` |
| 入站守卫是黑名单式（甲-c） | `model-service/msvc/guards.py` 文件头「未知扩展字段放行」+ `FORBIDDEN_TOP_FIELDS` |
| 平台不发 routing（2.4） | `grep -rn 'allow_providers\|"routing"' server/app --include='*.py'` → 0 命中 |
| provider 进账本（2.3） | `remote.py:456/668` → `ledger.py:142/190-191` |
| 无启动期 api_base 校验（2.4） | `grep -rn "api_base" model-service/msvc/*.py` → 只有 `config.py:15` 声明与 `provider.py:192` 透传 |

---

## 五、后续（供管家转 G）

1. **无分歧** ⇒ 按用户规则（两位架构师商量定）**不需上报用户**；丙的"是否调整 `compliance_deny_providers`"仍留作用户的最后一张牌，我没有替他定。
2. **A-1 可以放行给后端**，但实施单的落点清单要从 A 写的 2 项改成 **4 项**：
   `provider.py`（传 `custom_llm_provider`）＋ `config.py`（新增 `MS_LITELLM_PROVIDER`）＋ **`MS_CATALOG_MODELS` 含目标 model（2.2）** ＋ **`_xms` 的 provider 归因（2.3，缺陷，必须修）**。
3. **2.4 的补偿控制请 A 表个态**：同批做 / 降级为工单项 + 记残余风险。这是本意见里唯一需要 A 回话的点；若 A 选"降级"，我签，不进第三轮。
4. **服务级冒烟与 A-1 同批、归后端**，测量项按 3.3 写死（含总时长），临时 env 带 `LITELLM_LOCAL_MODEL_COST_MAP=True`，**不改任何默认值、不落盘**。
5. 本意见**未改任何线协议条文**，也未编辑 `docs/model-service-protocol.md`（孤本由 G 侧 A 起草提交）。若 A 采纳，条文改动由 A 落 §26 之后的会签章节。
