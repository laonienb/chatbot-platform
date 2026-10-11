# 派单（架构师 A）：把 B 的裁决 `ade58e6` 落成跨侧条文 ＋ 拆成开发可执行项 ＋ 验收两笔 deploy 交付

- 提出方：工作区管家-G · cron 轮（sessionId 无法自取）｜时间：2026-10-12T00:30:58+08:00（现取）
- 责任人：`v41f-架构师-A`（`bccbeb0b-282c-4c33-afaa-963820fd117b`）
- 租约：`harness/bridge/leases/G/A-a1-acceptance.docs.json`（已续租，paths 仍两份 docs，ttl 5400 ⇒ 至 01:25:58）
- 输入：`harness/proposals/B-decision-reasoning-budget-and-vendor-arm.md`（commit **`ade58e6`**，2026-10-12 00:18:30，
  架构师 B 裁「reasoning 预算语义」与「vendor 出口对账」，标题自述**与 A 无分歧、不需上报用户**）
- 你上一单的立场在协议 **§27.6**（R3 预置），本单要做的就是把它与 B 的结论**合成一条成文**。

## S1 落成文（协议新 §，接续 §27）

按 §27.6 的分工「B 结论经 C 转来后，跨侧成文（新 §／线协议条文）仍归 A」——B 的产物已经在 git 里（`ade58e6`），
不必等总线转。要落四点，**每点都要写明是采纳、修正还是驳回 B**（你与 B 无分歧不等于逐条照抄）：

1. **首 token 计时**：B 不采纳「改定义为首个流式事件」，改实现让「上游首 token 超时」名副其实
   （上游任何 chunk 含 reasoning-only 都复位计时），并**新增第二项预算**「首个正文 token 超时」
   （建议 code `upstream_reasoning_timeout`，含推理时长）；两项进 `capabilities.features.timeouts`，
   断言必须**缺键即失败**（B 引 §18.2-F1／P2-2 的教训）。
2. **心跳**：发，归 §6-4，发送方＝模型服务，本轮只做内层（服务发、平台仍吞）；对外透传另走 `AGENTS.md`「三」通告。
   B 的连带影响你俩都没提过，需要你先裁：服务一发心跳，平台「流式空闲上限 90s」永不触发 ⇒
   §8.1 **硬约束2** 的「逐项断言 < 90s」对标错了对象（B 建议改为「内层 < 绝对上限；若服务不发心跳则还须 < 空闲上限」，
   并把「是否发心跳」做成 capabilities 声明位）。
3. **入窗例外**：B **不给** reasoning 型上游开 §8.2.1 例外，改为 **TTL 分类**
   （新增 `MS_IDEMPOTENCY_ERROR_TTL_SECONDS`，建议 60s；成功仍 600s），并把「回放不可识别 ⇒ 每次回放 504 再计一次熔断
   （`cb_threshold=5`，同一条消息重发 5 次即全站降级）」这一条与本轮同批（改的是同一段 `_finish_window`）。
4. **vendor 对账**：B 明确**不把**「按 `api_base` 主机名判定 deny」写进协议（三条理由：真值表永不完整、
   假阴性比失明更危险、出口治理不属红线5 那一层），但写一条**最小配置校验**：新增 `MS_UPSTREAM_VENDOR_HOSTS`
   （谁声明谁举证），`upstream=litellm` 且该 env 非空而主机名不匹配 ⇒ 拒绝启动并打印 vendor／api_base／允许列表三者；
   未声明 ⇒ 只 WARNING 且文案须写「出口未经主机名校验」。落点＝同一个 `_reconcile_upstream_vendor_with_compliance`，
   不新增执行点、不做请求期判定。

## S2 拆项（这一条直接决定我下一轮能不能立刻派开发）

S1 落定后，把实现项写进 `docs/model-service-work-order.md`（§9 表新增行，编号自定，如 DW-8…／A-2…），每条带：
**文件:行**（如 `msvc/provider.py` 的计时复位点、`msvc/config.py` 的 `_reconcile_…`、`msvc/main.py:253/299` 的 TTL 两处、
`capabilities` 的 `features.timeouts` 键）、**验收判据**（含 mutation 证伪该怎么打红）、**是否依赖用户决策**。
⇒ 「已会签、不依赖用户决策」的那几条我会直接点亮 `flash-后端-G`，不再回头问你；依赖用户的（如「是否放宽 deny」、
「目标供应商实际是谁」）你标出来，我攒着上报。

## S3 验收两笔 deploy 交付（可等第二笔落地后一起做）

- `fd69314`：两个新 env 进 compose 与 `.env.example` ＋ DW-3 的 `MODEL_SERVICE_TOKEN` 正向占位。
  自报：compose 现 16 键、16 个 `MS_*` 全绑定（含拼错名反向对照）、三列差集只剩实现自述三项、零真凭证、
  **docker 未实跑**、回归 84/71 passed 且明写「两套用例没有一条读这两个文件 ⇒ 那两计数不验证本单改动本身」。
- 另一笔＝我刚派的 §9.4 执行（三处同值 ＋ README 排障一节），存证 `harness/proposals/G-dispatch-9-4-catalog-default-execution.md`，
  租约 `leases/G/A1-3.deploy-env-template.json` 至 01:30:58。
  ⇒ 你可以等它落地后一次验两笔，判「闭合／维持未判闭合」照 §9.1 口径（没实跑的别写成实跑）。

## 边界

- 写只限 `docs/model-service-protocol.md`、`docs/model-service-work-order.md`。
- **不碰 `deploy/*` 与 `model-service/README.md`**（此刻都在 `flash-后端-G` 名下至 01:30:58）；
  引用 README 措辞可以，改文件不行。`msvc/**`／`server/**`／`web/**` 不碰（架构师不写实现）。
- **不碰 `AGENTS.md`**：00:28 现取仍是 `M`（C 侧在写它那几节），你上一单也判了甲项不进「三」。
- 跑测试用 `git archive` 冻结快照＋仓库外临时库／`server/.venv` 解释器，**别在共享工作树跑、别碰 `server/dev.db`**。
- 一次提交（`git add` 只加那两份 docs，禁 `-A`）；提交信息自解释：S1 四条各自的采纳/修正/驳回、S2 的新增编号清单、S3 判定。
