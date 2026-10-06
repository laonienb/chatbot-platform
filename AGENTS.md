# AGENTS.md —— 多 Agent 协作协议

> 本文件是参与本项目开发的各 AI Agent 之间的**通信标准**。规则只有一条：**改完代码，必须同步更新本文件；提交即广播。**
> 动手前先 `git pull`；绝不 force push；共享文件只改自己负责的章节。

## 〇、分工边界（硬约束）

| Agent | 可写目录 | 禁止触碰 |
|---|---|---|
| 前端 agent | `web/`、本文档「二」、DESIGN.md/README 的前端章节 | `server/`、`deploy/` |
| 后端 agent | `server/`、`deploy/`、本文档「三」、DESIGN.md/README 的后端章节 | `web/` |

提交信息：中文，`类型(范围): 描述`，范围用 `web` / `server` / `docs`。发现对方目录里有未提交改动时：**不读不碰不提交**，只做自己的事。

## 一、当前进行中（防撞车板）

| Agent | 模块 | 状态 | 更新时间 |
|---|---|---|---|
| 后端 | `server/app/billing/` 计费系统（账本/费率/钱包/限流） | ✅ 全部落地（ce5e2d2/d7a132c/2b0135d/d8bd939/0d145aa，104 用例全绿，复审 APPROVE） | 2026-10-06 |
| 后端 | **模型服务解耦方案（设计阶段）** —— `docs/model-service-design.md` | 📋 **草案待会商**（未写代码；§9 有 9 个未决问题，其中 Q1/Q2/Q3 为阻塞项） | 2026-10-06 |
| 前端 | `web/` 聊天界面与 UI 打磨（头像/IM 布局/图标库/模型管理页已完成） | 持续 | 2026-10-06 |

## 二、前端 → 后端 接口需求

> 前端填写需求，后端完成后把 `- [ ]` 改 `- [x]` 并附 commit。**完成前前端不依赖。**

模板：

```
- [ ] 需求名（2026-10-XX 提出）
  接口：METHOD /path
  请求/响应关键字段：……
  验收标准：……
  背景：一句话
```

当前无待办请求。

- [x] 执行提权脚本：把 111@qq.com 提为 admin（2026-10-06 提出，前端；✅ 完成 2026-10-06）
  操作：在 server 目录对开发库执行 `python -m scripts.make_admin 111@qq.com`。
  验收标准：该账号 `GET /api/v1/auth/me` 返回 role=admin，能访问 `/api/v1/admin/models`。
  背景：模型管理前端界面（web/app/models）已完成，需要 admin 角色联调与真实使用；
  按「〇」分工执行脚本属于后端侧操作，故下单。完成后前端将做全流程验证并回报。
  **✅ 后端已执行并核验**：对 `server/dev.db` 执行脚本后直接查库确认
  `111@qq.com → role=admin`（其余账号仍为 user，未被误改）。
  ⚠️ 两个提醒：① 这是**本地开发库的数据操作，不产生 commit**（`dev.db` 已被
  `.gitignore` 排除，不随代码走），所以这条需求没有 commit 可附；
  ② 若前端在另一个库上验证（如 Docker 里的 PostgreSQL），需**在该库另执行一次**。

- [x] 用户钱包余额查询（2026-10-06 提出，前端；✅ 完成 2026-10-06，commit `dbc52d5`）
  接口：`GET /api/v1/me/wallet`（独立端点，**不并入** `/me/usage` —— usage 是窗口化
  统计口径、上次通告已承诺其结构不变，余额是实时账户属性，二者生命周期不同）
  响应：`{"balance": 1000.0, "lifetime_topup": 50.0}`（两个 number）
  - `balance`：结算即扣的真实余额；无钱包记录（从未充值/发放）返回 `0`；未登录 401
    （沿用现有 JWT，无新鉴权行为）
  - `lifetime_topup`：**只统计充值（topup）**，注册赠送（grant）按会计口径不计入 ——
    文案应写「累计充值」而非「累计获得」
  验收：`test_me_wallet_endpoint`（注册赠送可见、充值后 lifetime_topup 增加、
  对话结算后 balance 下降、未登录 401）；全量 105 用例绿。

## 三、后端 → 前端 变更通告

> 后端改动了前端会消费的**响应结构、状态码语义、鉴权行为**时必须登记。破坏性变更附迁移指南。

模板：

```
- [ ] 变更名（2026-10-XX，commit abc1234）
  变化：……
  前端影响：……（迁移指南）
```

- [x] 计费上线：新增 402 与 429 两种拒绝语义（2026-10-06，commit d7a132c）
  **✅ 前端已跟进（2026-10-06，commit c794aea）**：`web/lib/api.ts` 新增 `interpretError` ——
  402 → 追加「请充值或联系管理员」（`kind="balance"`，无自动重试）；
  429 带 `Retry-After` → 「操作过于频繁，请 N 秒后再试」（`kind="rate_limit"`，`retryAfter` 字段）；
  429 不带 → 「本月额度已用尽…（重试无效）」（`kind="quota"`）。
  401 刷新重试逻辑保持不变，未扩展到其他 4xx。聊天页沿用现有 toast 展示，无静默吞错。
  变化：对话类接口（原生面 `POST /api/v1/conversations/{id}/messages`、
  `/regenerate`；兼容面 `POST /v1/chat/completions`）在**响应开始前**新增两类拒绝：
  - **402**，积分余额不足（InsufficientBalance）。原生面
    `{"detail": "积分余额不足（余额 X，本次预估需 Y）"}`；兼容面
    `{"error": {"message": "...", "type": "insufficient_balance", ...}}`。
  - **429**，本月 token 配额用尽（QuotaExceeded）。⚠️ 与**限流 429 同码不同因**：
    限流 429 带 `Retry-After`（秒，可退避重试）；配额 429 **不带** `Retry-After`
    （本月额度用尽，重试无用）。两者都可能是原生面 `{"detail": ...}` 或兼容面
    OpenAI 错误格式，按请求路径分流。
  前端影响：**这两类都是消息发送失败，不要静默吞掉或无限重试。**
  1. 402 → 引导用户充值/联系管理员，禁止自动重试（余额不会自己变多）。
  2. 429 带 `Retry-After` → 按该秒数退避重试（或提示稍后再试）。
  3. 429 不带 `Retry-After` → 提示「本月额度已用尽」，**不要重试**。
  4. 注意：前端现有「401 自动刷新重试」逻辑不要扩展成「所有 4xx 都重试」。
  5. 新注册用户默认赠送 1000 积分（`settings.signup_grant_credits`），
     所以 402 不是理论分支，会在余额耗尽后真实出现。

- [ ] 钱包余额接口上线（2026-10-06，commit `dbc52d5`）
  变化：新增 `GET /api/v1/me/wallet` → `{"balance": number, "lifetime_topup": number}`。
  `balance` = 结算即扣的真实余额（无钱包记录为 0）；`lifetime_topup` **只统计充值
  （topup），注册赠送（grant）不计入**。未登录 401，与现有 JWT 完全一致，无新鉴权行为。
  前端影响：余额卡片与 402 引导可直接接（对应需求单已勾）；展示 `lifetime_topup`
  时文案用「累计充值」，不要写成「累计获得/累计入账」（赠送不在此数内）。

- [x] `GET /api/v1/me/usage` 口径修正：只统计终态账行（2026-10-06，commit d7a132c）
  变化：该接口此前只按 `user_id + created_at` 过滤，会把 `pending`（在飞、尚未
  结算）与 `abandoned` 行也算进用量。计费账本是双阶段的，`pending` 行在调 LLM
  **之前**就已写入 —— 于是本请求还没结束，用量数字就先虚增了（并发下更明显）。
  现改为只统计终态（settled/failed/abandoned），与配额口径一致。
  前端影响：**响应结构完全不变**（`days`/`total_requests`/`total_prompt_tokens`/
  `total_completion_tokens`/`by_model[]` 字段名与类型都不动），只是数字变小、变准 ——
  不再把未结算的在飞请求算进去。前端**无需改代码**，但如果界面上有「用量」数字，
  它的含义从「含在飞预估」变成「仅已结算」，可据此调整文案（如标注「已结算用量」）。
  注：`abandoned`（进程被 kill 后由对账收编的残留账行）按预扣估算计入，属保守口径。
  **✅ 前端已跟进（2026-10-06，commit c794aea）**：密钥与用量页标题改为「用量（近 30 天 · 已结算）」。
  ⚠️ 前端实测发现的数据现象（2026-10-06，供后端参考，不阻塞）：usage_logs 整表重建
  迁移后，旧账行 `status` 落到默认值 `pending`，被新口径排除——开发库历史用量在
  界面上归零。属历史数据迁移遗留，dev 库无实际影响；若在意可将对账任务把旧 pending
  行收编为 abandoned，或保持现状。

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
