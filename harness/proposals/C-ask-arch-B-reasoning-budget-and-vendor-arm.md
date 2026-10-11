# 派单：请 `max-架构师-B` 裁两条协议问题（A-1 落地后暴露）

- 派发方：工作区管家-C（cron 轮），2026-10-12 00:06+08:00
- 责任人：**`max-架构师-B`**（`981f5ce1-6cb8-40f9-9a8c-b76cf0010582`）＝本 harness 唯一架构师
- 依据（一律以 commit 文本为准，不要读工作区字节）：**`cdda16a`（2026-10-12 00:03:53，`feat(model-service): A-1 五项落点＋服务级真上游冒烟已跑`）**；上游会签链＝协议 §26.6（`35fa0e0`）＋B 你自己的 §26 会签（`0c294b9`）
- 为什么由管家派、为什么不问用户：用户 2026-10-11 定的规则是先由两位架构师商量决策，只有十分重大或三轮不决才上报。这两条都是**协议语义**问题，属你与 A 的权限，不是用户决策项。

## 问题一：reasoning 算不算「首 token」？要不要在推理期间发 keepalive？

A 的实测（`cdda16a` 提交信息末段，可自行复跑）：`MS_UPSTREAM_FIRST_TOKEN_TIMEOUT=15` 量的实际是**首个对外事件**，而 reasoning chunk 不产出对外事件 ⇒ 这 15 秒变成「整段推理 + 首个正文 token」的预算。本次上游首 reasoning token 0.5–9.7s、首 content token 0.95–11.3s、服务侧首事件最高 **11.33s**（余量仅 **1.3×**）。推理再长就会 504，而该 504 按 §8.2.1 入窗 ⇒ **同指纹 10 分钟内一律回放 504**。

要裁的：
1. 「首 token」定义要不要改成**首个流式事件**（含 reasoning/keepalive），还是维持「首个正文 token」而把预算语义改名？
2. 是否要求在推理期间发 **SSE 注释行/keepalive**（`: keepalive\n\n`）来复位空闲计时器？若要，它属于对外契约的哪一节、兼容面（`/v1/chat/completions`）是否同步、前端与第三方解析器会不会把它当异常（现网前端按「非 `choices[0].delta` 行」宽松放行——请按 `web/lib/api.ts` 的实际分支核，别按我这句话）。
3. 504 入窗这条要不要给 **reasoning 型上游**开例外（否则一次长推理会锁死同一条消息 10 分钟）？这是 §8.2.1 的语义，改与不改都要给判据。

## 问题二：`MS_UPSTREAM_VENDOR` 对账的是「声明」还是「实际出口」？要不要按 api_base 主机名判定？

A 自己实测并报出（同一条提交信息）：`MS_LITELLM_API_BASE=https://api.openai.com/v1` ＋ `MS_UPSTREAM_VENDOR=xiaomimimo` ＋ 无前缀 model ⇒ 服务**照常启动**、`/v1/models` 正常。⇒ 第 4 项（`MS_UPSTREAM_VENDOR`＋启动断言）对账的是**供应商声明**，不是**真实出口**；红线5 在 A-1 之后对真实流量失明这条**没有被这条断言封死**。要封死需按 `api_base` 的**主机名**判定——A 明写「那是协议决策，未做」。

要裁的：
1. 是否把「按 `api_base` 主机名判定」写进协议（deny 名单加主机名维度，或 vendor 与 host 必须一致）？
2. 若写，落在哪一节、由服务侧还是平台侧执行、启动期还是请求期？给出可红可绿的判据（例：`MS_LITELLM_API_BASE=https://api.openai.com/v1` ＋ `MS_UPSTREAM_VENDOR=xiaomimimo` ⇒ 期望拒启动）。
3. 若不写，就把「A-1 让红线5 对真实出口失明」这条**已知残余风险**写进协议/工单并显式留给用户——不能只留在提交信息里。

## 硬约束

- **只读**：不得改 `model-service/**`、`server/**`、`web/**`、`deploy/**`、`docs/*`、`AGENTS.md`。产出写进 **`harness/proposals/`** 本侧目录（建议 `B-decision-reasoning-budget-and-vendor-arm.md`），跨侧转达由管家落总线。
- `model-service/**` 此刻在 G 侧租约内（`leases/G/A1.model-service.json` 至 00:36:43、`A-1.model-service.json` 至 03:30:00），**不要起会写工作树的进程**（跑测试/起服务都会污染）；要复跑请用 `git archive` 冻结快照＋仓库外临时库，按 [[frozen-snapshot-peer-review]] 的证伪法。
- 不与 A 抢文：协议孤本由 A 起草提交。你出的是**裁决意见**，是否成文由 A 落 §，或经总线双侧同意的措辞由起草方落。
- 结论若与 A 分歧、或三轮定不下来 ⇒ 交用户；管家不代裁、不催结论。

## 回报

两项各给：结论一句 ＋ 依据（文件:行 或 commit）＋ 判据（哪条用例改坏必红）。若你判定「不必改协议、只补运维预期/文档」，也直说，那是有效结论。完成后把产物路径回报，我转 G 侧。
