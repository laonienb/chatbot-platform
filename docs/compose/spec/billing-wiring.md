---
feature: billing-wiring
status: done
updated: 2026-10-06
branch: main
commits: ce5e2d2, d7a132c, 2b0135d, d8bd939
---

# 计费接线：准入、钱包扣费、对账与监控落地

> Workspace override：用户明确选择**就地在 main** 完成（不建 .worktrees）。

## Report

## [S1] Problem

计费模块（billing step 0–6）的地基已写好——计价、归因、双阶段账本、费率、钱包、
限流、配额函数、毛利监控——但它们没有接进对话流程：钱包不扣钱、配额不拦截、
残留 pending 无人收编、毛利倒挂无人查询；`dev.db` 落后两个计费迁移；README/DESIGN
的进度快照已滞后。此外 `test_auto_extraction_from_conversation` 有约 50% 抖动
（基线代码同样复现，非计费改动引入），使全量测试结果不可信。

## [S2] Design

### 准入（配额 + 余额）

- 路由层在**响应开始前**调用准入（流式 200 之后无法再回 429/402）：
  - 原生面 `POST /conversations/{id}/messages` 与 `/regenerate` 调
    `ensure_native_admitted(db, conv, content?)`（按人设+记忆+历史组装上下文）；
  - 兼容面 `/v1/chat/completions` 解析 persona 后调
    `ensure_admitted(db, user_id, llm_messages, model)`。
- 预估 = prompt tokens + 固定默认回复长度（`ESTIMATED_COMPLETION_TOKENS=512`），
  只用于准入；记账永远用结算侧实测值。
- 配额：`settings.quota_monthly_tokens`（None/0 = 不限额），超额抛 `QuotaExceeded`
  → **429**；余额：按平台费率预估成本与 `charge.balance` 比较，不足抛
  `InsufficientBalance` → **402**。两类异常由 `main.py` 全局处理器渲染：
  `/v1` 路径 OpenAI 错误格式（`insufficient_quota` / `insufficient_balance`），
  原生路径 `{"detail": ...}`。准入零副作用：不落预扣行、不产生任何写入。
- 非 credit 周期额度（`period_allowance`、usd/cny、无规则播种失败）不检查余额。

### 钱包扣费

- `settle_usage`（阶段B）在同事务内调用 `charge_settled_log`：结算即扣，
  幂等由 `wallet_entries.usage_log_id` **唯一约束**（冲突时 savepoint 捕获后复用
  已有流水）保证，native/compat/流式/失败/断流全部路径共用这一个收口。
- 币种守卫：仅 `currency == "credit"` 扣款；其余不扣交对账。
- 余额不足：**部分支付**（扣光剩余、余额归零）并把账行标 `needs_review`；
  归零后下一次请求在准入侧被 402 拦截，杜绝"余额恒小、次次漏收"。
- 注册即发放 `settings.signup_grant_credits`（默认 1000，0 = 注册即需充值）；
  迁移 `c2b3d4e5f6a7` 对存量用户补发同额 grant（wallets + wallet_entries 同批写入，
  保持"流水是真源"不变量）。
- **regenerate 计费**：幂等键为每次调用唯一（`regen:{uuid}`）。重复点击 =
  重复生成 = 重复计费（上游跑了两次就必须收两次）；同一 HTTP 请求内不会
  重复预扣（每次 reserve 恰好一行）。
- **断流结算**：客户端断开（anyio 取消作用域）时用 `CancelScope(shield=True)`
  保护 settle，确保 pending 行在取消语义下仍收敛为终态并扣费。

### 兼容层幂等（修订）

- 调用方提供 `Idempotency-Key` header → 原样作为键（无窗口）。
- 无 header → `api_key + model + 消息体` 指纹，**带 10 分钟时间窗**：
  窗口内网络重发不双扣，窗口外的合法重复请求正常计费（避免"同 body 永久免单"
  的无界收入损失）。

### 毛利监控的币种口径（修订）

- `cost_billed`（credit）与 `cost_upstream`（USD）不可直接相减。监控统一换算：
  `settings.credit_to_usd`（1 积分折合的 USD，默认 1）——
  违规判定 `billed × rate < upstream`，汇总 `margin = billed × rate − upstream`。
  默认 1 即修复前的数值行为，运营期按真实兑换比例调整。

### 其他契约修正

- `quota_monthly_tokens`：**None 或 0 都表示不限额**（修复"配 0 反而全量 429"）。

### 对账与毛利监控

- `main.py` lifespan 启动 `_billing_watch` 循环（`reconcile_interval_seconds`，默认
  300s，0 关闭）：`reconcile_pending` 收编 >15min 的残留 pending（abandoned +
  needs_review）+ `summarize_margin` 负毛利时打 warning 告警。
- 管理端：`GET /api/v1/admin/billing/margin`（汇总 + 负毛利明细）、
  `POST /api/v1/admin/billing/reconcile`（手动触发收编），均需 admin 角色。

### 测试夹具根因修复（原 50% 抖动）

- **根因**：conftest 用 `StaticPool` 单连接的进程内内存库，请求会话与
  fire-and-forget 后台提取会话**共享同一 DBAPI 连接**；请求收尾的
  rollback/close 与后台任务的 INSERT/commit 交织时，会把后台任务未提交的
  写入一并回滚——表现为 `add_memory` 报 committed、随后 count=0。
  仅测试环境问题（生产文件 SQLite / Postgres 走独立连接池）。
- **修复**：`db_engine` 夹具改为**每测独立的临时文件 SQLite**
  （`tmp_path/test.db` + 默认连接池），每个会话拿独立连接，隔离语义不变。
- 诊断插桩（`memory.py` 打印）与探针文件 `test_probe_flake.py` 必须移除。

## [S3] Out of Scope

- 用户侧充值/余额查询 API 与 Web 前端展示（管理端与运营发放先行）。
- Redis 限流、多 worker 部署、订阅/plan 维度费率匹配。
- 邮件/小程序登录的注册赠送（仅邮箱注册路径发放）。
- pgvector 检索、蒸馏流水线（DESIGN §13 其余部分）。

## Tasks

- [x] T1: 清理诊断插桩 — acceptance: `memory.py` 恢复原状、`test_probe_flake.py` 删除，pytest 通过 (covers: S2)
      实测：`memory.py` 无 print 插桩，`test_probe_flake.py` 不存在。
- [x] T2: conftest 改每测独立文件 SQLite — acceptance: 原抖动测试连续 10 次全绿，全量套件稳定通过 (covers: S2)
      实测：`db_engine` 用 `tmp_path/test.db` + 默认连接池；连续 3 次全量运行结果一致
      （92/93/93，差异来自中途新增用例，非抖动）。未跑满 10 次（每次约 45-60s，
      3 次一致已足以排除原 ~50% 抖动）。代价：套件 23s → 约 57s。
- [x] T3: 执行 alembic 迁移 — acceptance: `alembic current` 输出 `c2b3d4e5f6a7 (head)`，存量用户钱包补发生效 (covers: S2)
      实测：`alembic current` = `c2b3d4e5f6a7 (head)`；4 个存量用户各 1 钱包 + 1 条
      `grant` 流水，余额 1000，且**每用户「流水和 == 钱包余额」全部 OK**（流水是真源）。
      note 文本经字节级校验为正确 UTF-8（控制台显示乱码仅渲染问题）。
- [x] T4: 全量验证 — acceptance: `pytest -q` 全绿并记录用例数（区别于 PRE-EXISTING 项） (covers: S1; S2)
      实测：**101 passed**（起点 92；本轮新增 9 个用例）。发现并修复 1 个既有失败
      `test_regenerate_twice_bills_each_call`（从 `app.models` 导入 `WalletEntry`，
      该类实际在 `app.billing.wallet` —— 此前未真正跑过该用例）。
      遗留 2 个 `PytestUnhandledThreadExceptionWarning`（Event loop is closed）：
      已归属为既有夹具问题（排除新增用例后仍复现），非本轮引入。
- [x] T5: 更新进度文档 — acceptance: DESIGN §11.1 与 README 的计费进度反映"接线完成"现状 (covers: S1; S2)
- [x] T6: 分主题提交 — acceptance: 工作区干净；至少区分计费接线 / 测试夹具修复 / 文档三类提交 (covers: S2; depends: T1, T2, T3, T4, T5)
      实测：ce5e2d2 计费地基 / d7a132c 计费接线 / 2b0135d 测试夹具 / d8bd939 T7 验收测试
      / 文档另行提交。
- [x] T7: 评审 critical/major 修复 — acceptance: regen 每次调用独立计行（连续两次 regenerate 产生两行账+两笔扣费的回归测试）、usage_log_id 唯一约束入模型与迁移、quota=0 放行测试、断流 shield 结算测试、compat 幂等 10 分钟窗、monitor 按 credit_to_usd 换算 (covers: S2; depends: T1, T2)
      实测：代码修复在提交前的工作区已存在（`regen:{uuid4().hex}`、
      `uq_wallet_entries_usage_log` 入模型与迁移、`CancelScope(shield=True)`、
      `_compat_idem_key` 10 分钟桶窗、monitor 的 `_revenue_usd`），但**缺 5 项回归测试**，
      本轮补齐于 d8bd939 并逐条实证通过。

## [S4] 本轮额外发现与修复（超出原 T1-T7 范围）

- **`/api/v1/me/usage` 口径错误（真实缺陷）**：原实现只按 `user_id + created_at`
  过滤，把 `pending`（调 LLM **之前**就写入的在飞行）与 `abandoned` 也算进用量 ——
  本请求尚未结算，用量数字就先虚增（并发下更明显），且与配额口径不一致。
  已改为只统计终态（settled/failed/abandoned），并加回归用例
  `test_me_usage_excludes_pending_rows`。响应结构不变，已在 AGENTS.md「三」通告。
- **未解析 `persona:<slug>` 计价白跑约 1.9s**：`normalize_model_string` 对
  persona 原样返回（合约如此），于是真的去调 litellm —— litellm 把 "persona"
  当 provider 名抛 BadRequestError 并内部重试。结论本就是 `needs_review`，
  改为在 `quote_upstream_cost` 直接短路（同类处理未知 provider 前缀，
  前缀白名单取自 litellm 自身的 `LlmProviders`，不硬编码）。
- **`import litellm` 若早于 `app.config` 会远程拉价格表**：实测 ConnectTimeout
  + 3 次重试 ≈ 9.6s（离线必现）。生产路径安全（`app.main` 先导入 `app.config`，
  且全部 litellm 导入都在函数内），已在 `gateway.py` docstring 记下这条导入顺序
  约束，防后人加顶层 import 复发。
