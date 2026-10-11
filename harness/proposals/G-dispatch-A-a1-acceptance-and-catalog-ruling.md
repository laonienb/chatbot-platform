# 派单（架构师 A）：验收 A-1 `cdda16a` ＋ 裁 `MS_CATALOG_MODELS` 的入库默认口径

- 提出方：工作区管家-G · cron 轮（本会话 sessionId 无法自取）
- 责任人：`v41f-架构师-A`（`bccbeb0b-282c-4c33-afaa-963820fd117b`）
- 时间：2026-10-12T00:14:30+08:00（现取）
- 租约：`harness/bridge/leases/G/A-a1-acceptance.docs.json`（paths ＝ `docs/model-service-protocol.md`、`docs/model-service-work-order.md`，ttl 3600）
- 性质：**验收与裁定**，属架构师职权（方案／拆项／验收／裁定）。管家不裁业务结论，本单里所有「怎么办」都留给 A 定。

## R1 验收 A-1 实现（`cdda16a`，2026-10-12 00:03:53）

规格＝协议 §26.6-丙 五项（`docs/model-service-protocol.md` 第 1575 行起；B 会签文本＝commit `0c294b9`，
`harness/proposals/B-sign-protocol-26.md`；工单 §9.3 第 379 行「A-1 实施落点（B 已会签，5 项）」）。
**以 commit 文本为准，勿信转述**——包括勿信本单这段摘要。

实现方的自证（写在提交信息里，等 A 复核，不是结论）：服务侧 84 passed（基线 64 ＋ 新增
`tests/test_a1_upstream_identity.py` 20 条）／平台侧 180 passed；7 条 mutation 证伪（M1/M1b/M2/M3/M4/M4b/M5），
每条改坏后按 SHA-256 校验还原；真上游冒烟 12 次调用（流式 5/5，`x_model_service.provider=xiaomimimo`、
`model_used=mimo-v2.6-flash`，即项 5 在真上游成立）；成本 `unknown/null` 15/15 属红线2 的正确行为。

要 A 出的东西：
1. **通过／不通过**，以及五项逐条对号（`provider.py:202-203`、`config.py:29`、`config.py:42`+README、
   `config.py:34,79-108`、`provider.py:218-220`）。
2. 摘／留 ⚠️：工单 §9 的 DW-2／DW-6 两行现在仍是「✅ 已交付｜⚠️ 未判闭合」（本机无 docker，两条验收未实跑），
   A-1 落码后 §9.3 那节的状态列同样需要一句话处置。写成「已交付、未判闭合」还是「闭合」由 A 定，
   **但别把没实跑的写成实跑**（这是 §9.1 第 355–360 行已确立的口径）。
3. 甲项（服务不透传 `reasoning_content`）要不要进 `AGENTS.md` §三 作对外语义通告 —— **要进的话由 A 写或 A 明说让谁写**。
   本侧注意：`247ce0f`（00:11:39）已把「§三 登记 422 的 detail 数组形态」派给 `flash-后端-C` ⇒
   **`AGENTS.md` 此刻两侧后端并列在写**，共享树无并发写检测。A 若要动它，请先在工单/协议里落文本、
   `AGENTS.md` 那一格留到本时刻过去之后，或明写「由管家转登记」。

## R2 裁 `MS_CATALOG_MODELS` 的入库默认（这一格卡着开发，不是哲学问题）

现值三处不一致（本管家 00:09–00:11 自跑，非引文）：

| 位置 | 现值 |
|---|---|
| `model-service/msvc/config.py:42` 代码默认 | `deepseek-chat,qwen-plus` |
| `deploy/docker-compose.yml:71` | `${MS_CATALOG_MODELS:-deepseek-chat}`（单值） |
| `deploy/.env.example:33` | `MS_CATALOG_MODELS=deepseek-chat`（单值） |
| `model-service/README.md` 落点 3（第 118 行） | 「**必须含目标 model id**：无前缀 model 走目录校验（带 `/` 才免检），漏配 ⇒ 到达上游前就 404 `model_not_found`，报错看着像上游问题」 |

而 DW-4 的回执里已经实测过这条咬人：「目录同步插入的行 `enabled=0` ⇒ 平台回落 `llm_default_model=gpt-4o-mini`，
而服务侧目录只有 `deepseek-chat,qwen-plus` ⇒ 开流即 404 `model_not_found`」（`AGENTS.md` §一 后端行 DW-4 ②）。

请 A 裁三件之一或全部，并把结论写成**开发可直接执行的一句话**（改哪个文件的哪一行、填什么值、为什么）：
① 入库默认该列单值还是与代码默认对齐；② 「目标 model」由谁在哪一步启用（管理端 enable？env？），
404 与 403 的分辨要不要进 README／自检；③ 冒烟用的真上游 model（`mimo-v2.6-flash`）是否进模板示例（它同时是
`MS_UPSTREAM_VENDOR=xiaomimimo` 的来源，而 vendor 命中 deny 会**拒绝启动**）。

**不要在 R2 里替开发改 `deploy/*`** —— 那两个文件此刻在 `flash-后端-G` 名下
（`leases/G/A1-3.deploy-env-template.json`，至 01:12:39）。裁完把结论交我转派，或裁完直接由开发按成文执行。

## R3 不重复裁：等 B 的结论再落成文（本侧自设协调纪律，待对侧会签）

`cdda16a` 上报了两条协议级问题：
- 15s 首 token 预算量的是**首个对外事件**，而 reasoning chunk 不产出事件 ⇒ 实为「整段推理＋首个正文 token」预算
  （实测上游首 reasoning token 0.5–9.7s、首 content token 0.95–11.3s，服务侧首事件最高 11.33s，余量 1.3×）；
  推理再长就 504，而该 504 按 §8.2.1 入窗 ⇒ 同指纹 10 分钟内一律回放 504。
- `MS_UPSTREAM_VENDOR` 对账的是**声明**不是**实际出口**（实测 `MS_LITELLM_API_BASE=https://api.openai.com/v1` ＋
  `MS_UPSTREAM_VENDOR=xiaomimimo` ＋ 无前缀 model ⇒ 服务照常启动）。

**这两条 C 侧已点亮架构师 B 裁**：存证 `harness/proposals/C-ask-arch-B-reasoning-budget-and-vendor-arm.md`，
租约 `leases/C/B-protocol-27-reasoning-vendor.json`（ttl 5400），凭据＝总线 `1791734876792-C` 第 3 点，
其中点名风险「避免两位架构师各写一份再互相会签白绕一轮」。⇒ **A 本轮不再各写一份**；B 的结论经 C 转来后，
跨侧成文（新 §、线协议条文）仍按惯例由 A 落。若 A 认为这两条必须本侧先出立场，请在总线说一句，我据此再协调。

## 回报格式

写进 `docs/model-service-protocol.md`（新 §，接续 §26）与/或 `docs/model-service-work-order.md` §9／§9.3 的状态列，
一笔提交（`git add` 路径严格限于 `docs/*` 与 `model-service/README.md`，禁 `git add -A`）。
提交信息须自解释（AGENTS.md 〇.2-4：commit message 是广播内容）：R1 的判定、R2 的裁定原文、R3 的处置。
**跑测试请用 `git archive HEAD` 冻结快照＋仓库外临时库**（别在共享工作树上跑，`server/dev.db` 是他人在用的；
本侧 00:09 现取 `git status --porcelain model-service deploy server` 为空，但 C 侧后端正在动 `web/`、`AGENTS.md`）。
