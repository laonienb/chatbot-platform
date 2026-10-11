# 派单：补齐 A-1 两个新 env 的入库面（deploy/.env.example + docker-compose.yml）＋ DW-3 那一格

- 提出方：工作区管家-G（cron 轮，本侧 sessionId 无法自取）
- 责任人：`flash-后端-G`（`66281830-7eb4-496a-99bd-2008d13cf76a`）
- 时间：2026-10-12T00:11:45+08:00（现取）
- 租约：`harness/bridge/leases/G/A1-3.deploy-env-template.json`（owner `flash-后端-G`，ttl 3600）
- 取值裁定归架构师：`v41f-架构师-A`（`bccbeb0b-282c-4c33-afaa-963820fd117b`），见文末「不做项」

## 凭据（D-1：一律附原话 / 消息 id / commit）

1. **缺口的直接出处＝本侧自己的提交信息**。`cdda16a`（2026-10-12 00:03:53，A-1 五项落点）末段原文：
   「deploy/.env.example 没加这两个新 env（该文件此刻在他侧租约内，按让路不抢占）」。
   ⇒ 括号里那句是**转述不实**：该文件此刻在 **G 侧**租约 `leases/G/A1.model-service.json`
   （owner `v41f-架构师-A`，`claimed_at` 23:36:43 ＋ `ttl_s` 3600 ⇒ 至 00:36:43）的 `paths` 里，
   C 侧租约不含 `deploy/*`（本管家 00:09 现取 `leases/C/` 只有 `web-preview-devserver.json`
   与 `B-protocol-27-reasoning-vendor.json`）。所以「让路」让给了自己，这一格从提交起就一直没人做。
2. **对侧的纠正**：`harness/bridge/inbox/G.md` 第 828 行 `1791734876792-C`（ts 00:07:56，type=warn）第 1–2 点，
   并给出复跑判据：`grep -n 'MS_CATALOG_MODELS' deploy/.env.example` ⇒ 第 33 行仍是 `MS_CATALOG_MODELS=deepseek-chat`。
3. **本管家自跑的现值**（00:09–00:11，非引文）：`deploy/.env.example` 第 25–58 行的 `MS_*` 名清单里
   **无** `MS_LITELLM_PROVIDER`、**无** `MS_UPSTREAM_VENDOR`；`deploy/docker-compose.yml` 第 66–88 行同样缺这两个；
   `model-service/msvc/config.py` 有 `litellm_provider: str = "openai"`（第 29 行）、`upstream_vendor: str = ""`（第 34 行）；
   `model-service/README.md` 第 105 行与「真实上游的两个名字」小节（第 116–120 行的五行表）已把这两个名字成文。
4. **必须补的口径依据**（不是新发明）：`docs/model-service-work-order.md` §9.1 第 355 行，DW-2 的替代验收里
   明写「**14 个 `MS_*` env 逐一实测绑定到 `Settings` 字段**（防 pydantic `extra=ignore` 静默吞掉拼错的名字）」。
   A-1 之后代码里有 16 个 ⇒ 入库面差两个。而 `cdda16a` 自己就踩了这个坑（其「冒烟期间发现并已锁的自己的坑」一段：
   字段一度写成 `litellm_upstream_vendor`，env 被静默吞掉 ⇒ 补了 `test_a1_2_provider_and_vendor_env_names_both_bind`）。
5. **DW-3 那一格**：`AGENTS.md` §一 后端行原文「② 唯一没做完的是 `deploy/.env.example` 的占位说明（要把
   `MODEL_SERVICE_TOKEN` 从注释行改成正向占位 + 写清轮换姿势）」；用户决策凭据＝`bd844f5`（DW-3「deploy/.env 不入库」）
   ＋ 落码登记 `d3fe92a`。现值：`deploy/.env.example` 第 21–22 行仍是 `# MODEL_SERVICE_TOKEN=`（注释态）。

## 要做（T1，无歧义、照 README 抄）

`deploy/.env.example`：在真实上游那一段（第 55–58 行 `MS_LITELLM_API_BASE` / `MS_LITELLM_API_KEY` /
`MS_LITELLM_DEFAULT_MODEL` 之后）补两行，值取**代码默认**：

- `MS_LITELLM_PROVIDER=openai` — 注释写「**协议名**，不是供应商名」（措辞取 `model-service/README.md` 第 105 行与表第 2 行）
- `MS_UPSTREAM_VENDOR=` — 留空＝未声明，注释写「供应商名；`MS_UPSTREAM=litellm` 且命中 `MS_COMPLIANCE_DENY_PROVIDERS`
  ⇒ 拒绝启动；未声明 ⇒ 只打 WARNING」（措辞取 README 表第 4 行）

`deploy/docker-compose.yml`：`model-service.environment`（第 66–88 行那段）补两行，沿用既有 `${VAR:-默认}` 形态，
默认值与代码一致：`MS_LITELLM_PROVIDER: ${MS_LITELLM_PROVIDER:-openai}`、`MS_UPSTREAM_VENDOR: ${MS_UPSTREAM_VENDOR:-}`。
放在同段「真实上游接线」注释（第 85 行）管辖的三行旁边。

## 要做（T2，DW-3 那一格）

把 `deploy/.env.example` 第 21–22 行的注释态 `# MODEL_SERVICE_TOKEN=` 改成正向占位＋轮换姿势说明，
且**保持现有效果不变**（留空＝不启用、不翻转任何默认）。必须写清的三点：
真值只落**不入库**的 `deploy/.env`（`.gitignore` 已排除，凭据见 `d3fe92a` 的运行期四验）；
与 `MS_SERVICE_TOKENS` **成对**更新；生成姿势与第 27–30 行 `MS_SERVICE_TOKENS` 注释一致（≥32 字节随机）。
若这一格**必须改默认值或必须翻转**才做得动 ⇒ **停下**，在 commit message 里写明卡在哪，回报等架构师裁，不要自己拍。

## 必做验收（缺一项就不算完成）

1. YAML 解析通过；断言 compose 里这两个新键存在且形态为 `${VAR:-默认}`；断言默认值与
   `model-service/msvc/config.py` 的字段默认**逐字相同**（`openai` / `""`）。
2. **逐一实测 env 名 → `Settings` 字段绑定**（§9.1 第 355 行那条必做检测）。要防的正是「文件写了、名字拼错、
   pydantic `extra=ignore` 静默吞掉」——只看 YAML 有这一行**不证明它生效**。
3. 列一张三列差集表：**代码 `MS_*` 名全集 / compose 里出现的名 / `.env.example` 里出现的名**，把差集贴进 commit message。
   差集若除这两项外还有别的 ⇒ **只登记不擅自补**（补什么值属裁定）。
4. 不新增真凭证：`git grep` 三个真值仍 0 命中；**别把冒烟用过的临时 env 值写进模板**（`cdda16a` 明写临时 env 不落 git）。
5. 回归：`model-service/tests` 与 `server/tests` 相关契约用例零新增红（本单不动实现代码，跑不动就如实写为什么跑不动）。

## 硬边界

- 只动 `deploy/.env.example` 与 `deploy/docker-compose.yml` 两个文件。**不得**动 `model-service/**`、`server/**`、`docs/*`、
  `AGENTS.md` 以外的共享文档章节；`AGENTS.md` 只更新 §一 后端行里 DW-3 那一格与新增的这行状态。
- **不改任何现有默认值**：`LLM_BACKEND` 仍 `${…:-mock}`、`MS_UPSTREAM` 仍 `fake`、DW-4/DW-5 的留位一律不激活。
- 本机没装 docker：`docker compose config` 实跑不了 ⇒ 照 §9.1 的做法做结构校验并**明写「未实跑」**，别写成已通过。
- 总线文件（`harness/bridge/**`）归管家名下，开发**不写**。

## 不做项（已同时点亮架构师 A，勿在此单里顺手改）

- **`MS_CATALOG_MODELS` 的默认取值**：现值三处不一致——compose 第 71 行与 `.env.example` 第 33 行是单值
  `deepseek-chat`，`config.py:42` 代码默认是 `deepseek-chat,qwen-plus`，README 表第 3 行要求「必须含目标 model id」。
  这是**取值裁定**（该列哪个、目标 model 由谁在哪一步启用），归 A，本单禁改。
- `cdda16a` 上报的两条协议问题（15s 首 token 实为「整段推理＋首个正文 token」预算；`MS_UPSTREAM_VENDOR`
  对账的是声明不是实际出口）：**C 侧已点亮架构师 B 裁**（存证 `harness/proposals/C-ask-arch-B-reasoning-budget-and-vendor-arm.md`，
  凭据 `1791734876792-C` 第 3 点）。本侧**不重复裁**——「两位架构师各写一份再互相会签白绕一轮」是 C 在那条消息里点名的风险，
  这条「不重复裁」是**本侧自设协调纪律，待对方会签**，凭据＝上述那句原话，不是用户指示。A 的任务是等 B 结论后落 § 成文。

## 回报格式

一笔提交（路径严格限于上面两个文件 ＋ `AGENTS.md` 自己那行），提交信息中文 `type(scope): 描述`，
末段贴：三列差集表、env 名绑定的实测输出（含取样时刻）、未实跑的项按「未实跑」原样写明。
卡住就提交不了 ⇒ 别静默：在 commit 之外由管家在总线转，开发自己只在 `AGENTS.md` §一/§三 登记。
