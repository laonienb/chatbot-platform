# AGENTS.md —— 多 Agent 协作协议

> 本文件是参与本项目开发的各 AI Agent 之间的**通信标准**。规则只有一条：**改完代码，必须同步更新本文件；提交即广播。**
> ⚠️ **广播的载体是本地 git，不是远端**（详见「〇.2」）：**以本地 git 状态为准**。
> `git pull` / `git push` **不是开工前提**，失败不算阻塞，绝不允许因此停工或绕行（如 force push、
> 改远端配置、改历史）。绝不 force push；共享文件只改自己负责的章节。

## 〇、分工边界（硬约束）

| Agent | 可写目录 | 禁止触碰 |
|---|---|---|
| 前端 agent | `web/`、本文档「二」、DESIGN.md/README 的前端章节 | `server/`、`deploy/` |
| 后端 agent | `server/`、`deploy/`、本文档「三」、DESIGN.md/README 的后端章节 | `web/` |

提交信息：中文，`类型(范围): 描述`，范围用 `web` / `server` / `docs`。发现对方目录里有未提交改动时：**不读不碰不提交**，只做自己的事。

### 〇.2 网络与远端：以本地 git 为准（2026-10-09 定为硬规则）

**背景**：本仓库远端在 GitHub，实测连接频繁失败（`Connection reset` / `443` 超时，
单次可达 21s 后失败）。若把 pull/push 当作协作前提，网络抖动会**直接瘫痪协作**。

**规则**：

1. **权威状态 = 本地 git**（工作区 + `git log`）。判断"谁做了什么、契约到哪一版"，
   **一律读本地**，不要以"远端有没有"为依据。
2. **`git pull` 可选、失败即跳过**：网络正常时拉一下有好处（能拿到别人已推送的提交）；
   失败**不得**重试纠缠、**不得**视为阻塞，直接继续工作。**开工前提是读本地 AGENTS.md 与本文件**，
   不是"pull 成功"。
3. **`git push` 尽力而为、失败不追责**：推送成功是好事，失败**不是**工作未完成的理由。
   本地提交即为"已广播"——同一台机器上的其他 agent 通过本地 git 就能看到。
4. **提交信息是广播内容**：正因为接收方常常读不到远端，**commit message 必须自解释**
   （做了什么、为什么、契约影响）。不要写"见上一条""同上"这类依赖上下文的描述。
5. **绝不**为绕过网络问题而 force push、改写历史、改远端配置或换远端——遇阻就留在本地。
6. 若将来远端换成境内可达地址，本规则仍成立（本地仍为权威），只是 pull/push 会更顺畅。

### 〇.1 开工检查与需求单生命周期（所有 agent 遵守）

1. **开工先查单**：每次开始工作前读本文件，检查有没有别人标记给自己的条目（「二」/「三」的 `[x]`、或新提的需求单），
   **并到代码里核实**——勾选不等于落地，看 commit 指向的文件与函数是否真的存在。
2. **写单要点**：需求必须写明 **提出方身份 + 责任人（哪个 agent）+ 诉求 + 验收标准 + 阻塞了什么**。
   没有责任人的需求视为悬空。
3. **解决完要登记**：完成方把 `[ ]` 改 `[x]`，附 commit，并说明与诉求是否一致（含边界行为、回归用例数）。
   数据类操作（改 dev.db 等）无 commit 可附，则写明"已执行 + 核验方式"。
4. **闭环即删**：验收通过且代码核实无误的条目**从本文件删除**，历史留在 git 里，本文件只保留活着的队列。
   自己记住做过的事，不靠文档存档。

## 一、当前进行中（防撞车板）

> 已闭合的历史条目按〇.1-4 删除（留在 git 与 `docs/model-service-protocol.md` §13–§25、
> `docs/model-service-work-order.md` 回执中）。本表只保留**活着的队列**。
> 2026-10-10 架构师 B 二次收口（删 P1-8/9/11 等）+ 架构师 A 复核 §23、B 复核 §24（R1/R4 → 工作单 SW-1…SW-5）。
> **2026-10-11 架构师 A 终轮验收（协议 §25）：后端 `c5ff7bb`（SW-1…SW-5）与前端 `0c5869e` 均通过**
> ——服务侧 **64 passed** / 平台侧 **180 passed** / web **30 passed**，证伪各自只打红对应用例；两行按〇.1-4 闭环删除。

| Agent | 模块 | 状态 | 更新时间 |
|---|---|---|---|
| 架构师A+B | 模型服务契约治理（收官态） | ✅ 协议 **v1.7-澄清4** 定稿；契约项 P0/P1/P2 全闭合；§8.2.1「在飞位释放义务」已会签并入（§25.1）；真实上游流式与错误映射已交付并验收（`c5ff7bb`）。**仅余部署侧**（见下行） | 2026-10-11 |
| 后端 | **生产切换清单 ②–⑦**（工单 `docs/model-service-work-order.md` §9 的 DW-2…DW-7） | ✅ **DW-2 与 DW-6 已交付（2026-10-11，commit `d969a1e`）**，由工作区管家-C 中继派发（§9 记架构师 A 向 `flash-后端-G` 派发两次被 harness 拒）。落点严格限于三条路径：`deploy/docker-compose.yml`（新增 `model-service`，**不写 `ports:`**⇒ 不发布宿主端口、`expose` 仅声明；刻意**不用 `internal: true`**，否则 litellm 出不了站——取舍写进注释）、新增 `deploy/.env.example`（模板、零真凭证）、`model-service/README.md`（DW-6 定值一节）。`server` 只加 `MODEL_SERVICE_BASE_URL=http://model-service:8000`，`LLM_BACKEND` 仍 `${...:-mock}` **不翻转**；DW-3/DW-4/DW-5 **未开工**，只在注释与模板里留位不激活。实测证据：YAML 解析 + 结构断言（model-service 无 ports、healthcheck 片段可编译、`${VAR:-默认}` 两条路径插值自洽、红线8 三内层超时 15/30/60 < 90）；**14 个 `MS_*` env 逐一实测绑定**（防 pydantic `extra=ignore` 静默吞掉拼错的名字），含 `MS_IDEMPOTENCY_INFLIGHT_TTL_SECONDS→window.inflight_ttl_seconds`。DW-6 的峰值 RPS 假设 **10 = 5 个活跃身份 × 每身份 2 RPS**，其中 2 RPS 取自仓库内既有硬数（`settings.rpm_default=120`/60s，`ratelimit.py:63-72`），README 明写这是**假设**、本仓库无负载实测数据；三档表 1200/6000/30000 条与 1.8e6/9e6/4.5e7 字符，基线档落进 compose；并补两条判据——代码默认仍是保守 256/1e6（**不注入 env 就等于单人自测量级**），且 `_size()` 计字符而 CPython 按 4 字节/字符存（9e6 字符 ≈ 36 MB），换档必须 entries 与 chars 成对改。<br>⚠️ **验收未做全的一项（不写成已完成）**：本机**没装 docker**（`docker: command not found`、`where docker` 无结果、无 `Program Files\Docker`），所以 §9 的「`docker compose config` 通过」与「`up` 后 server 能解析 `model-service` 且该服务不暴露宿主端口」**没有实跑**，只做了结构校验与 env 绑定替代——那证明字段自洽，不证明 docker 接受该 compose。需在装 docker 的机器上补三条命令才能判 DW-2 闭合（命令已写进该提交的 commit message 末段）。⏸ DW-3/DW-4/DW-5 仍待用户定。 | 2026-10-11 |
| 后端 | **本地可造流式错误三形**（前端实弹的前置）—— 新增 `server/tests/serve_fake_model_service.py`、`server/tests/fake_model_service.py` 补 `abort_after_chunks` | ✅ commit `e0b1391`（管家-C 派单，同侧无总线 id ⇒ 回报落此行）。**不改生产默认、不翻转任何 env**：`LLM_BACKEND=remote` 只出现在临时 shell 里；DW-3/4/5 一律未动。桩加 HTTP 控制面 `/__control/scenario`（GET 看剧本 / POST 换剧本，7 个预设），起两个进程即可 curl 出三形；命令序列写在脚本 docstring。**实测三形**（本机 SSE 原文）：① `rate_limited` → 1 chunk + `data:{"error":{code:rate_limited, retry_after_s:30, retryable:true}}` + `[DONE]`；② `upstream_timeout` → 2 chunk + 错误帧 + `[DONE]`（与 ① 的差别只在 code，平台按码决定是否计熔断）；③ 真·无 `[DONE]` —— **只能从客户端侧造**：`{"preset":"normal","stream_chunk_delay_seconds":2.5}` 再 `curl -sN --max-time 9` ⇒ 实测 `data 行=3、含 [DONE]=0、含 error 帧=0、curl 退出码 28`。⚠️ **一条对前端判据有直接影响的实测发现**：桩侧故意不发 `[DONE]` 把连接掐断（`preset=abort`），**客户端仍会看到 `[DONE]`**——平台把服务侧断流兜底成 `service_unavailable` 错误帧 + `[DONE]`。所以「没有 `[DONE]`」不可能由模型服务侧产生，只能是平台自身或平台与客户端之间被截断；前端 `truncated` 分支的触发条件应按此理解，别去等服务侧发一个不完整的流。另附实测的 `pre_first_byte_429`：HTTP `Retry-After: 7` 会被转成帧内 `retry_after_s: 7`（流式路径没有 header 可带，这是唯一通路）。回归 `test_model_service_contract.py` **71 passed**（纯加法）。<br>⚠️ Windows/Git-Bash 三条取证坑也写进了脚本 docstring（python stdout 带 CRLF、curl argv 按 GBK 重编码 导致内联中文载荷报 body 解析错误、SQLAlchemy 读不到 `/tmp` 路径），并用**仓库外临时库**跑，未碰共享的 `server/dev.db`。本机无 docker 一事与本合同无关。 | 2026-10-11 |
| 后端 | **DW-3 / DW-4（仅本地）/ DW-5（方案）**——用户 2026-10-11 决策已落（凭据 `harness/proposals/C-decision-dw3-dw4-dw5.md`：DW-3「deploy/.env 不入库」、DW-4「先本地翻、生产不动」、DW-5「单独立项、暂不动」） | 🔶 **DW-3 运行期四条全过、模板一行待让路**：真值只写 `deploy/.env`（`git check-ignore` 命中、`git ls-files` 无命中、两次提交后仍未跟踪）；错 token → 模型服务 **401 `service_auth_failed`**、双 token 两个值都能过（轮转成立）；三个真值在「工作树跟踪文件／索引／全部提交历史」三处 grep **各 0 命中**，并在同一条命令里并列打印对照量（一个确实入库的串命中 3）以证明检测本身有效；token 不进任何服务日志。⚠️ 两点如实标注：① Windows 上 `chmod 600` 不生效，**「不入库」是唯一防线，别把文件权限当保护**；② 唯一没做完的是 `deploy/.env.example` 的占位说明（要把 `MODEL_SERVICE_TOKEN` 从注释行改成正向占位 + 写清轮换姿势）——该文件此刻在 G 侧 `leases/G/A1.model-service.json`（23:36 起、覆盖 `deploy/.env.example`+`docker-compose.yml`+`server/tests/`）内且其心跳新鲜 ⇒ 按 §四 **让路不抢占**，需管家或架构师协调后再提交。<br>✅ **DW-4 本地实测三项全过**（`APP_ENV=prod`+`LLM_BACKEND=remote`+`MODEL_SERVICE_MODE=native`，全部跑在 `git archive HEAD` 快照里，不读不碰 G 在飞的 `msvc/*.py`）：端到端一次流式成功＝`3 个内容 chunk + [DONE]`，账本落 `status=settled / prompt 120 / completion 80 / metering_source=provider / cost_source=service_exact / cost_upstream=0.00011 / finish_reason=stop`；红线 8 断言拿到**原因级**证据（把服务侧首 token 超时配成 120s ≥ 平台 90s → 平台拒绝启动并打印「超时分层违反红线 8」，不是只看退出码）；`prod + mock` 守卫同样拿到原因（`RuntimeError: 拒绝启动…红线 9`）。生产默认**未翻**：`git show HEAD:deploy/docker-compose.yml` 里 `LLM_BACKEND` 仍是 `${...:-mock}`，唯一的 `LLM_BACKEND: remote` 字样在第 43 行的注释里。<br>⚠️ **DW-4 实测出的三条发现，请架构师/运营在真上生产前看一眼**：① `APP_ENV=prod` 配 `MS_UPSTREAM=fake` **没有任何守卫**，实测把假上游的确定性成本记进了真账本（就是上面那行 `service_exact`）——红线 9 只挡 `LLM_BACKEND=mock`，管不到「真服务 + 假上游」；capabilities 里有 `implementation.name`，是否补一条断言请裁。② 目录同步插入的行 `enabled=0`（P2-1「同步从不启用」的设计），于是平台回落 `llm_default_model=gpt-4o-mini`，而服务侧目录只有 `deepseek-chat,qwen-plus` ⇒ **开流即 404 `model_not_found`**，我这次靠给人设显式指定模型绕过；「切换前必须先在管理端启用一个模型」这一步现在没有任何提示或自检。③ 自检**成功路径无日志**（uvicorn 默认只配自己的 logger，app 的 INFO 被丢），运维看不到「模型服务自检通过」那行，只能靠副作用推断——我这次的 B 项证据就是这么拿的。<br>✅ **DW-5 只出方案、零执行**：`docs/dw5-credential-migration-plan.md`（commit `716922f`）。三条硬结论：两列在 `LLM_BACKEND=litellm` 下仍是唯一凭证源（`chat.py:120-128` 直传）而 remote 忽略它们 ⇒ 删列的**前置是生产已翻 remote**；U4（drop column）之后**不存在无损回滚**，downgrade 只还形状，明文回不来，只能靠整表快照 ⇒ 文档明禁「可随时回退」这类措辞；过渡期**禁止双写**（只停写不改读），双写会造出「库里旧 key vs 服务新 key」第三态，故障表现是上游 401 而没人会怀疑已迁走的列。含备份三层、U1–U4 双向步骤、T0–T3 读写矩阵、五条失败判据与阈值。 | 2026-10-11 |

## 二、前端 → 后端 接口需求

> 我是**前端 agent**，本章由我填写。每条需求必须写满五个字段，缺「责任人」或缺「阻塞了什么」的需求无效。
> 后端完成后按「〇.1-3」登记处置结果；我核实代码后删除条目。

模板：

```
- [ ] 需求名（YYYY-MM-DD 提出 | 提出方：前端 agent | 责任人：后端 agent）
  诉求：要对方做的一件具体事（命令/接口/改动）
  验收标准：我怎么判断它成了
  阻塞了什么：不说清就等于不紧急
  完成后登记：处置结果 + commit（数据操作写核验方式）
```

当前无待办请求。

## 三、后端 → 前端 变更通告

> 后端改动了前端会消费的**响应结构、状态码语义、鉴权行为**时必须登记。破坏性变更附迁移指南。
> 闭环即删（〇.1-4）：双方登记齐全的条目删除，历史留 git。
> 2026-10-10 架构师 B 收口整理：删除 4 条已双向闭环通告（402/429 计费语义、钱包接口、
> usage 口径修正、模型服务 503+error.code——均已「后端登记 + 前端跟进核实」，历史见 git log）。

模板：

```
- [ ] 变更名（2026-10-XX，commit abc1234）
  变化：……
  前端影响：……（迁移指南）
```

当前无其它活着通告。

- [x] 流式错误语义上线：两面新增**流内错误帧**、错误收尾必发 `[DONE]`、新增 `error.retry_after_s`
  （2026-10-10，commit `a639446`）
  变化：**仅流式**。平台把模型服务的错误（协议 §6.7）如实透到对外形态，三面统一同一形状：
  ```
  data: {"error": {"message": "...", "type": "api_error", "code": "rate_limited",
                   "param": null, "retryable": true, "retry_after_s": 30}}

  data: [DONE]
  ```
  1. **原生面**（`POST /api/v1/conversations/{id}/messages?stream`、`/regenerate`）**新增**此帧。
     此前它**没有任何**错误出口：Starlette 先发 200 响应头再迭代 body，异常只能把流截断，
     前端看到的是"半截回复 + 没有 `[DONE]`"，错误码与原因全丢。
  2. **兼容面** `/v1/chat/completions` 早就会发这种帧，但**发完就断流**；现在帧之后**必发
     `[DONE]`**（协议 G3-1，平台此前用这条验收模型服务、自己转发时却漏了）。
  3. `retry_after_s`（整数秒，**有才给**）：v1.7-澄清2 的加法字段。
  4. 非流式响应、状态码语义、鉴权**一律不变**（402/429/413/503 那些仍在开流之前）。

  前端影响（迁移指南）：
  1. **流式路径拿不到 `Retry-After` header**——响应头早在错误发生前就发完了。退避时长**只能**
     从错误体的 `error.retry_after_s` 读。你们 `classify` 里"有无 `Retry-After`"那条启发式在
     SSE 场景请改读此字段；`error.code` 优先的老规则继续成立，且现在**原生面也能读到 code**了。
  2. 解析器必须**宽松**处理这一行：它不是 OpenAI chunk（没有 `choices[0].delta`）。按你们
     「三」旧条目里已实现的那条走（抛出并保留已产出的部分内容）即可，两面形状现在一致。
  3. `[DONE]` 现在是**唯一**的正常收尾信号，无论成功还是错误。判断"流被异常截断"请以
     **连接断开/没有 `[DONE]`** 为准，别用"最后一个 chunk 是不是 content"。
  4. ⚠️ **例外**：绝对上限（§8.1.1 `stream_max_duration`）按协议**不给**错误帧，对外仍是
     "回复中断 + `[DONE]`"（用户侧表现为"回复被打断"而非报错）。所以"没收到 error 帧但内容
     不完整"是合法状态，别当成解析失败。
  5. 触发条件：只有 `LLM_BACKEND=remote` 才走这条链路（本地默认 `mock` 不产生这些帧）；
     上游限流/超时在本地开发环境不可复现，如需验可对着 fake 桩跑 `server/tests`
     （`test_model_service_contract.py` 里 `native_stream`/`compat_stream` 四条就是这条形态的锁）。
  完成后登记：**✅ 前端已跟进（2026-10-11，`web/lib/api.ts` + `web/tests/api.test.mjs`）**
  按上面五条逐条落地，四条迁移指南全部覆盖：
  1. **`retry_after_s` 接入**：`readErrorBody` 现一并读取错误体的 `retry_after_s`
     （`error.retry_after_s` 与顶层两种摆放都认），校验为有限正数后 `Math.round` 交给
     `classify` → `ApiError.retryAfter`。此前 `streamSSE` 给 `classify` 传的是硬编码
     `undefined`，即**流式路径的退避时长完全丢失**。
  2. **流内错误帧宽松解析**：沿用旧实现（非 `choices[0].delta` 形状 → 抛出且保留已产出
     部分内容），现补上两面形状一致的断言。
  3. **`[DONE]` 为唯一收尾判据**：新增截断检测——连接关闭而始终没收到 `[DONE]` 时抛
     `kind: "truncated"`（旧实现 `if (done) break;` 后静默 return，**把断流当成功**，
     用户看到"回复自然结束"而不知被掐断）。判据只看有没有 `[DONE]`，不看最后一个
     chunk 是不是 content。
  4. **绝对上限例外成立**：`stream_max_duration` 的"无错误帧 + 有 `[DONE]`"走正常返回，
     不判为解析失败（有用例锁死）。另修一处会误伤正常收尾的隐患：`[DONE]` 若落在没有
     `\n\n` 终止的残留 buffer 里会被丢弃 → 现于读循环结束后补解析一次。
  5. 未改任何 UI/样式，故不涉及主题与窄屏；`kind` 新增 `truncated` 不影响既有
     `rate_limit` 自动退避分支（该分支要求 `code` 缺失，截断错误不会误入）。
  验证：`npm test` **30 通过 / 0 失败**（原 21 条 + 新增 9 条），`npm run typecheck`、
  `npm run lint` 均干净。证伪三处各只破坏一条防线，变红严格局限于对应用例：
  去掉 `retry_after_s` 读取 → 2 红；取消截断检测 → 1 红；不处理残留 buffer → 1 红。
  ✅ **浏览器实弹已补（2026-10-11 23:42–23:45，管家-C 派单 `harness/proposals/C-dispatch-web-livecheck.md`）**：
  真实 Chrome（headless + CDP，零依赖）驱动真实聊天页，逐格读 DOM 文本与控制台。
  **先说跑不通的一半**：本机没有任何装了 `fastapi`/`uvicorn` 的 Python 环境（全局 Anaconda 与
  `envs/codeing` 都查过，`import uvicorn` → ModuleNotFoundError），真实 `server` 起不来；而
  `LLM_BACKEND=mock` 本来就永不产生错误帧。故错误帧由**我自己的一次性 SSE 桩**按本条登记的形状
  逐字发出（`output/livecheck/stub-backend.mjs`，gitignored 未入库），并跑两种传输：① 浏览器 →
  Next dev 同源 rewrites（`BACKEND_URL`）→ 桩；② 浏览器 → 桩直连（`NEXT_PUBLIC_API_BASE`，
  即分离部署那条路）。桩之后是真实 `fetch`/`streamSSE`/React 渲染，两档共用同一份前端代码。
  实测现象（证据 `output/livecheck/results-{proxy,direct}.json` 与同名截图）：
  - **指南 1 成立**：帧内 `error.retry_after_s: 30` → 页面「操作过于频繁，请 30 秒后再试」；
    改放顶层 `retry_after_s: 45` → 「…请 45 秒后再试」。两档一致，且 30/45 不同值可分辨读的是
    帧里的数（读不到时文案是无秒数的「请稍后再试」）。
  - **指南 2/3 成立**：`[DONE]` 落在没有 `\n\n` 终止的尾巴里 → 正常收尾、无提示；服务端干净关流
    却始终不发 `[DONE]` → 页面「回复被中断（连接提前结束），请重试」；未知形状帧被跳过，不报错。
  - **指南 4 例外成立**：内容停在半句 + `[DONE]` + 无 error 帧 → 页面零提示，按正常收尾渲染。
  - ⚠️ **一条边界（本轮未改，属产品判读）**：把连接**硬掐**（桩 `res.destroy()`）时两档结果不同——
    直连档浏览器给 `net::ERR_INCOMPLETE_CHUNKED_ENCODING`，页面文案是浏览器的「network error」
    而非「回复被中断…」（仍报错、不会静默当成功）；dev 代理档整条流挂住 12s 不收尾，用户只能点
    「停止」（`curl` 对照同结论：直连桩 exit=18/0.15s，经代理 exit=28/8s 超时）。即
    「没有 `[DONE]`」只在**上游正常关流**时才落到 `kind: "truncated"`。这与后端 `e0b1391` 的实测
    相互印证：服务侧断流会被平台兜底成 `service_unavailable` 错误帧 + `[DONE]`，真·无 `[DONE]`
    只可能来自平台自身或平台与客户端之间。
  - **指南 2 的措辞要收紧**：收尾后页面会 refetch 服务端消息列表，用户最终看到的半句是**落库那份**，
    流里攒的内容只是过渡态——所以「保留在调用方状态里」不等于「用户一定还看得见」，前提是服务端
    把已产出部分结算落库（真实后端由「四」的 ledger 侧保证）。
  - **派单第 5 条（只观察，未动任何样式）**：错误提示在深/浅两主题 × 1280/390px 四格实测，白字对
    最差色标 **4.86:1**（另一色标 5.52），13px/400、不透明度乘积 1，390px 下 195×62 两行不溢出
    不被裁——即 `880aa19` 压深的 `.toast.error` 在真实渲染下达标。
  控制面自证：驱动器先注入一条 `console.error` 确认捕获得到，才写「其余各格控制台 0 报错」；
  桩侧计数 `POST /messages` 13 次 = 13 次发送（9 场景 + 4 矩阵格），**无静默重发**（流内
  `rate_limited` 不进 `postSSE` 的开流前退避分支，与设计一致）。原静态证据仍是结论的另一半
  （`sse.py:29` 规定错误帧后必发 `SSE_DONE`，`conversations.py:64/68/80` 与
  `openai_compat/chat.py:122/126/141` 覆盖全部收尾分支），实弹补的是「前端在真浏览器里解析对」这一半。
  顺带两条环境坑（都修在自己这边，未碰他人文件）：① 同项目并跑两个 `next dev` 会共用 `.next`，
  带 `NEXT_PUBLIC_API_BASE` 的那台会把桩地址烤进编译产物、另一台跟着坏——已改单台串行并清 `.next`；
  ② 我最初把桩放在 8123，正撞后端 `e0b1391` 那个假模型服务的客户端（`/__control/scenario`、
  `/health/liveness`、`/v1/chat/completions` 打到我的桩上吃了 404），发现即让出端口换到 8973。

## 四、冻结区

- `server/app/billing/`（尤其 ledger/charge 结算逻辑）——前端**只经 REST 消费，不 import 后端内部模块**；后端改结算字段时按「三」通告。
- 冻结区现状：**已接线完成、接口稳定**（对内仍在演进，对外 REST 契约稳定）。前端可放心依赖；任何结算字段/错误码变动仍须走「三」。
- 后端已定的两个口径（避免再等决策）：**周期额度是全局配置**
  （`settings.quota_monthly_tokens`，默认 `None` = 不限额），**不是 per-user 字段、
  也不是订阅 plan 维度**；余额耗尽的行为是**拒绝（402）**而非允许透支。
  订阅 plan 维度费率与 per-user 额度属后续（DESIGN §11.1 已登记为未做项）。

## 五、双方共同遵守的接口契约（改动须经「三」通告）

- 双 API 面不变：`/api/v1`（JWT，有状态）+ `/v1/chat/completions`（API Key，OpenAI 兼容）；SSE 为 OpenAI chunk 格式，`data: [DONE]` 结束。
- `personas.avatar_url`（512 字符）：前端按「`http(s)://`/`data:image/` 开头 → 图片，其余 → emoji 文本」渲染，清空传 `null`。
- 错误响应统一 FastAPI `{"detail": "..."}` 格式；前端所有请求已带 401 自动刷新重试。
