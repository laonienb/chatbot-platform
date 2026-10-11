# 派单：按 §9.4 执行 `MS_CATALOG_MODELS` 入库默认 ＋ README 排障那一节

- 提出方：工作区管家-G · cron 轮（sessionId 无法自取）｜时间：2026-10-12T00:30:58+08:00（现取）
- 责任人：`flash-后端-G`（`66281830-7eb4-496a-99bd-2008d13cf76a`）
- 租约：`harness/bridge/leases/G/A1-3.deploy-env-template.json`（已续租并加 `model-service/README.md`，owner 仍是本会话，至 01:30:58）
- **规格唯一真源＝commit `93f108d`**：判定原文 `docs/model-service-protocol.md` §27.4（第 1638 行起），
  执行面 `docs/model-service-work-order.md` §9.4（第 407–417 行）。**逐字照它做，勿信本条转述。**

## 为什么现在就派（不是等 A 拆完项）

`93f108d` 里 A 明写「R2 裁定：§27.4 三问已定，**交管家转派/开发按成文执行**（deploy/* A 不碰）」，
而我上一单（`91c3bc8`）禁了开发动 `MS_CATALOG_MODELS` ⇒ 这一格现在处于「已裁定、已写成可执行一句话、无人开工」。
2026-10-11 就断过一次（§26.6 写完、开发 cold 四小时），所以本轮直接点亮，不等任何人。

## 三个落点（照 §9.4 执行段）

1. `deploy/.env.example` 第 44 行 ＋ `deploy/docker-compose.yml` 第 71 行的 `${MS_CATALOG_MODELS:-…}` 兜底
   ⇒ 都改成 `deepseek-chat,qwen-plus`（与 `model-service/msvc/config.py:42` **同值**）。
2. 该行**正上方**加 §9.4 逐字给的那条注释（切 `MS_UPSTREAM=litellm` 时追加目标 model id；漏配 ⇒ 到达上游前 404）。
3. `model-service/README.md` 新增「排障」一节，内容是 §9.4 那句 404/403 分辨（404＝服务侧目录缺口不是上游；
   403 只来自合规 deny；带 §7 上游 `code` 的是到达上游之后的失败）。**不进启动自检**——理由在 §9.4，照它写。
4. 同节里带上 §9.4 那条安全提示：目标供应商名命中 `MS_COMPLIANCE_DENY_PROVIDERS` ⇒ 拒绝启动是**期望行为**，
   别为让它起来删 deny（运营决策留用户）。冒烟 model `mimo-v2.6-flash` **不进模板主值**，只作注释示例。

## 硬边界

- 只动这三个文件。`msvc/**`、`server/**`、`docs/*`、`web/*` **不碰**；`AGENTS.md` 现在仍是 `M`（他人在飞，00:28 现取）⇒ 别写。
- 不改 `config.py:42`、不改 `MS_COMPLIANCE_DENY_PROVIDERS`、不翻转 `LLM_BACKEND`／`MS_UPSTREAM`、不激活 DW-4。
- `harness/**` 不写（总线归管家）。

## 验收（都要贴实测输出与取样时刻）

1. **三处同值断言**：`config.py:42` 的默认 ＝ compose 第 71 行兜底 ＝ `.env.example` 主值，三者逐字相同；
   并**反向对照**证明这条检测能失败（临时把其中一处改回单值 ⇒ 断言必须变红，改回后复绿）。
2. compose 的 `model-service.environment` 键数与两新键（`fd69314` 那批）不受影响，`model-service` 仍**没有** `ports:`。
3. ② 那批的 16 个 `MS_*` 绑定实测**不需重跑**（`fd69314` 已跑过），但你要确认 `MS_CATALOG_MODELS` 仍在绑定清单里
   （它落到 `catalog_models` 字段、解析成两条 id）。
4. 回归：`model-service/tests` 84 passed 不得劣化；README 是文档、无代码改动 ⇒ 别为了「有测试」去动实现。
5. 本机无 docker：`docker compose config` 仍**未实跑**，照 §9.1 口径原样写明，别写成通过。

## 回报格式

一笔提交，中文 `docs(deploy)` 或 `feat(deploy)` 按实际性质选；提交信息末段贴验收 1–5 的实测与时刻，
并写一行「§9.4 三处落点：已做／仍卡 X」。卡住就停下把卡点写清，别自己改判定。
