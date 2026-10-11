# 派单：RB-1…RB-6（B 裁决的落地项，A 已拆到 文件:行 ＋ mutation 判据）

- 提出方：工作区管家-G · cron 轮｜时间：2026-10-12T00:39:11+08:00（现取）
- 责任人：`flash-后端-G`（`66281830-7eb4-496a-99bd-2008d13cf76a`）
- 租约：`harness/bridge/leases/G/RB1-6.model-service-server.json`（owner 本会话，`claimed_at` 00:40:47＋ttl 7200 ⇒ **至 02:10:47**；ttl 取两小时的依据写在租约 `ttl_reason` 里）
- **规格唯一真源＝commit `30834b2`**：拆项 ＝ `docs/model-service-work-order.md` **§9.5**（第 424–443 行，RB-1…RB-7 表）；
  成文 ＝ `docs/model-service-protocol.md` **§28**（条文已就地在 §4.3.1／§6-4／§7／§8.1／§8.2.1 落笔）；
  上游裁决原文 ＝ `harness/proposals/B-decision-reasoning-budget-and-vendor-arm.md`（`ade58e6`）。
  **逐字照 §9.5 做，勿信本条转述；本条只补「批次划分／边界／回报格式」这三样 §9.5 里没有的东西。**

## 为什么不等 C 侧或等用户

§9.5 第 428 行 A 已判：**RB-1…RB-6 实施均不依赖用户决策**，责任人＝后端 agent；RB-7（对外透传心跳 ＋ 「三」通告）**依赖用户/产品**，本轮不做。
第 443 行另列了五项「攒着上报用户的」，明写「**不阻塞 RB-1…RB-6**」。⇒ 这是「已会签、不依赖用户、无人开工」的空闲活，直接派。

## 批次划分（照 §9.5 第 441 行的实施顺序，三批互不覆盖 ⇒ 三笔提交）

1. **批 1 ＝ RB-1 ＋ RB-2 ＋ RB-5**（A 标「必须同批」：RB-1/RB-2 同在 `provider.stream` ＋ `event_gen`，RB-5 同在 `_finish_window`）。
2. **批 2 ＝ RB-3 ＋ RB-4**（capabilities 声明位 ＋ 平台侧红线 8 断言，独立）。
3. **批 3 ＝ RB-6**（`config.py` 的 `_reconcile_upstream_vendor_with_compliance` ＋ `egress_verified`，独立）。

⇒ 每批一笔提交，**批边界就是停笔点**：任一批做不完或判据打不红，**停在边界上**、把卡在哪一行写进那一笔的提交信息末段，别把三批压成一笔、也别为了「完成」去改 §9.5 的判据。

## 三条最容易翻车的地方（§9.5 里有，这里只点名，不重述判据）

- **RB-2 的平台侧三条契约用例（def 842／1130／1458）现在跑的是平台自己的桩**：只证「平台能吞注释行」，不证「真实服务会发」。
  A 裁的是**必须改用真服务或对拍**，且要有一条正向用例证「真实服务也发」。mutation 判据第 434 行写得很清楚——
  留回桩上仍然全绿 ＝ 那条防线根本没看守。这是 §18.2-F1「两端各跑自家桩」的第三次，别做成第四次。
- **用例必须真 uvicorn 子进程**，不得用 httpx `ASGITransport`（A 实测它攒完整个响应体才返回，测不出 chunk 间时序）。
- **心跳不得进 `StreamTerminal`**（`_cache_stream_terminal` `main.py:244-253`），否则被 §8.2.2-3 终态重放缓存污染体积核算。

## 租约与写边界

- 本租约覆盖：`model-service/msvc/`、`model-service/tests/`、`server/app/llm/`、`server/tests/test_model_service_contract.py`。
- **不碰**：`deploy/*` 与 `model-service/README.md`（另份租约至 01:30:58）、`docs/*`（A 名下，此刻仍是 `M`）、
  `web/**`（C 侧前端名下）、`server/dev.db`（共享库，别动）、`harness/**`（总线归管家）。
- **`AGENTS.md` 仍挂着无租约覆盖的未提交改动**（C 侧两次报备 `1791736152729-C` 第 5 点／`1791736560371-C`；本侧 00:35:54 复现 `M`、mtime 00:26:46）⇒ **本轮一律不写它**，RB-7 那格「三」通告属未做待办。
- 若某条判据要求动 `server/app/config.py` 这类**不在租约内**的文件 ⇒ **停下回报**，别顺手扩大 paths。
- 不改任何入库默认值／不翻转 `LLM_BACKEND`／`MS_UPSTREAM`／不激活 DW-4；新增 env（`MS_UPSTREAM_FIRST_CONTENT_TIMEOUT`、
  `MS_IDEMPOTENCY_ERROR_TTL_SECONDS`、`MS_UPSTREAM_VENDOR_HOSTS`）按 §9.5 给默认值，**入库面（compose/`.env.example`）本轮不补**——那要另开一单，写进回报即可。

## 回报格式

三笔提交（或停在某个批边界），中文 `feat(model-service)`／`test(server)` 按实际性质选。每笔末段贴：
① 该批正向用例的输出；② **每条 mutation 打红的确切计数与红点文件名**（§9.5 每条都写了「mutation＝… ⇒ 红」，
判据打不红就等于没实现）；③ 取样时刻；④ 若用了 `git archive` 冻结快照／仓库外临时库，把命令写出来。
测试一律别在共享工作树跑脏 `server/dev.db`（A 自己的验收就是用 `git archive` ＋ `server/.venv` 解释器跑的，照它做）。
