# 用户决策落地：DW-3 / DW-4（本地版）/ DW-5

- 登记方：工作区管家-C（`9ed6b758-07a2-4d42-9986-3ea75c13a48b`），2026-10-11 23:28+08:00
- **凭据**：用户在 C 侧会话内经选项直接选定（AskUserQuestion 回执原文）：
  - DW-3 → 「**deploy/.env，不入库**（推荐）」
  - DW-4 → 「**先本地翻，生产不动**（推荐）」
  - DW-5 → 「**单独立项，暂不动**（推荐）」
- ⇒ `docs/model-service-work-order.md` §9 里那三个「⏸ 待用户定」的前置条件已解除前两项；DW-5 仍是「不执行」，只允许出方案。

## DW-3：token 生成与注入（可开工）

- 生成 ≥32 字节随机 token；真实值**只写 `deploy/.env`**，该文件已被 `.gitignore` 的 `.env`/`.env.*` 排除，**不得入库**。
- 提交的是 `deploy/.env.example` 模板（占位符，零真凭证）。
- 两处注入：模型服务 `MS_SERVICE_TOKENS`、平台 `MODEL_SERVICE_TOKEN`（协议 §3 静态 Bearer）。
- **验收（照 §9，别降口径）**：`git check-ignore deploy/.env` 命中；`git grep <token明文>` 无命中；错 token 时被模型服务拒绝（401）；token 不出现在任何日志与提交信息里。

## DW-4：**仅本地**翻到 remote/native（可开工，但有硬边界）

- 在本地 `deploy/.env`（不入库）里设 `LLM_BACKEND=remote` + `MODEL_SERVICE_MODE=native` + `APP_ENV=prod`，验证端到端一次真上游流式成功。
- ⚠️ **提交进 git 的默认值必须保持 mock**：任何 compose/env 模板里 `${LLM_BACKEND:-mock}` 这类默认形态**不得翻转**，`docker-compose.yml` 已入库的那份不改默认。
- 启动自检要真跑：协议版本、内层超时 < 外层（红线 8）、模型目录同步。
- `APP_ENV=prod` 下 `mock` 被拒启动的守卫需实测生效一次。
- **验收**：本地一次真上游流式成功的完整证据（SSE 原文片段 + usage 落地）；`git status` 显示 `.env` 未被跟踪；生产默认未变（`git show <commit>:deploy/docker-compose.yml` 里仍是不翻转形态）。

## DW-5：**不执行迁移**，只出方案

- 产出物＝一份迁移方案（可写在 `docs/model-service-work-order.md` 里属于后端的章节，或工单回复给我），内容至少包含：备份点、`upgrade` 与 `downgrade` 双向步骤、两列在过渡期的读写策略、失败回滚判据。
- **不得动库、不得提交迁移脚本**。「单独立项」是用户原话。

## 顺序建议

先做完你手上那条「本地可复现三种流式错误帧」——DW-4 的本地验证正好要用它。然后 DW-3，再 DW-4，DW-5 只出文档。

## 边界照旧

不动 `web/`（前端持租约 `leases/C/web-livecheck.json`）、不动 `harness/`、不改协议契约条文（有疑义归架构师）。完成后按 `AGENTS.md`「〇.1-3」在**后端那一行**登记，并按「〇.1-4」闭环。
