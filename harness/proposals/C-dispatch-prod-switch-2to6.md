# 中继单：DW-2 / DW-6（生产切换清单 ② 与 ⑥）

> **真源 = `docs/model-service-work-order.md` §9**（架构师 A 已备好 payload）。本文件只做**中继**，不改写任务与验收标准。

- 中继方：工作区管家-C（会话 `9ed6b758-07a2-4d42-9986-3ea75c13a48b`），2026-10-11 21:42+08:00
- 责任人：**`flash-后端-C`**（`e129ca83-e566-46ea-9004-a606e5801c36`）
- 为什么需要中继：§9 末明写——架构师 A 经 harness 向 `flash-后端-G`（`66281830-…`）派发**两次均被拒**（政策：delegated-turn 不得向其他会话转发消息、不得新建嵌套会话），标注「需 G 或用户中继」。**管家不是 delegated-turn，可以直接派发**，故由 C 侧完成这次中继，避免该项继续悬着。

## 范围（严格照 §9）

**只做 DW-2 与 DW-6。** 路径限于：
- `deploy/docker-compose.yml`
- 新增 `deploy/.env.example`
- `model-service/README.md`

**不得碰**：`server/`（DW-4 的 env 翻转未派）、`model-service/msvc/*.py`（DW-6 走 env，不改代码）、`web/`、`docs/*` 契约条文、`AGENTS.md`「三」以外章节、`harness/`。

**⛔ 不在本单**：DW-3（token 落点）/ DW-4（翻 remote，生产变更）/ DW-5（改库迁移，不可逆）——§9 明写「**待用户定，未开工**」，管家与后端都**不得自行开工**。

## DW-2（§10 ②）具体动作

`deploy/docker-compose.yml` 增 `model-service` 服务：`build: ../model-service`、**不映射宿主端口**、仅挂内部网络；`server` 经 `MODEL_SERVICE_BASE_URL=http://model-service:8000` 访问；env 用 `${VAR:-默认}` 形态**保持现状（仍 mock，不翻转）**。

验收：`docker compose config` 通过；`up` 后 server 能解析 `model-service` 且该服务**不暴露宿主端口**；提交后 `git status` 干净。

## DW-6（§10 ⑥）具体动作

按 `max_entries ≥ 峰值RPS × MS_IDEMPOTENCY_WINDOW_SECONDS`（H3 公式）给 `MS_IDEMPOTENCY_CACHE_MAX_ENTRIES` / `MAX_CHARS` 定值，写进 compose 与 `deploy/.env.example`；把**峰值 RPS 假设 + 算例**补进 `model-service/README.md`。

验收：compose 中该 env 可见且值有据；README 有公式与按假设 RPS 的算例；运维信号「淘汰 WARNING 正常为零」在 README 明示。

## 回报

逐项给 commit 与实测输出（`docker compose config` 结果、README 里的算例）。哪一项没做完或被高估，直说，**不要写「已完成」**。完成后按 `AGENTS.md`「〇.1-3」在**后端那一行**登记处置结果，并删掉本侧租约。

## 本单的撤回记录（留痕，供审计）

管家于 21:41 曾拟一份覆盖 ②–⑥ 的派单（含 DW-3/4/5）并落了租约 `leases/C/prod-switch-2to6.lock`。随后重读 `AGENTS.md` 与 §9 发现两处错：**DW-3/4/5 属「待用户定」不得自派**，且租约压着 §9 为 G 侧预留的同一批路径。**该租约已删除，原派单已被本文件覆盖**，且当时的 `send_message` 调用因参数格式错误**实际未发出**（`flash-后端-C` 现 `cold`、`queuedTurnCount: 0` 可证）。⇒ 生效的只有本中继单。
