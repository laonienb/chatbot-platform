# DW-5 迁移方案：provider 凭证下沉 + `llm_models.api_key` / `api_base` 废弃

> **状态：仅方案，未执行。** 用户 2026-10-11 决定「单独立项，暂不动」（凭据见
> `harness/proposals/C-decision-dw3-dw4-dw5.md`）。本文不含迁移脚本、不动库、不改代码。
> 落笔者：后端（`flash-后端-C`）。执行人与开工条件见 §6。

## 0. 为什么这件事必须单独排期（不是"顺手删两列"）

两列今天在**两条不同链路**上的地位不一样，实测/读码确认：

| 链路 | 两列是否还是真源 | 证据 |
|---|---|---|
| `LLM_BACKEND=litellm`（现网默认之一） | **是**。`resolve_chat_model` → `lookup_model_credentials` 取的 `(api_base, api_key)` 直传给 litellm 后端 | `app/services/chat.py:120-128, 147, 411` |
| `LLM_BACKEND=remote`（模型服务） | **否**。`RemoteBackend` 按协议 §9 **刻意忽略** `api_base`/`api_key` 入参（`# noqa: ARG002`） | `app/llm/remote.py:428-430, 459-460` |

⇒ **硬前置**：只有当生产已经跑在 `remote` 上（DW-4 的终态）之后，这两列才是"只写不读的死列"。
现在删列 = 直接把 `litellm` 链路打挂。这是本方案与 DW-4 的唯一强依赖，也是它该单独立项的原因。

## 1. 备份点（三层，缺一不算可回滚）

1. **结构快照**：`alembic current` 的输出存档（预期单头 `e5f6a7b8c9d0`，链见
   `alembic/versions/`）。迁移脚本的 `down_revision` 必须钉在这个值上，不并行开第二个头。
2. **数据快照**：
   - PostgreSQL：`pg_dump -Fc --table=llm_models <db> > backup/llm_models.<日期>.dump`，
     并另存一份 `copy (select model, provider_hint, api_base, (api_key is not null) as has_key
     from llm_models) to csv`——**只存"哪几行有 key"，不存 key 明文**（明文进备份文件就等于
     把凭证从库里搬到磁盘上，等于没迁）。
   - SQLite（现 dev.db 形态）：停应用后整库文件复制 `copy server\dev.db backup\dev.<日期>.db`。
     ⚠️ 不停进程的 `cp` 会得到半写事务的坏副本（WAL 未合并）。
3. **映射表**（真正不可再生的一份）：`模型行 → 新真源位置`（模型服务的 env 名 / 密钥管理条目 ID）。
   这张表只记"哪个 provider 的哪份凭证接管了哪一行"，同样不落明文。它是 §5 回滚时唯一能重建
   双侧一致性的东西。

## 2. upgrade 正向步骤（拆成可分别回退的四步）

1. **U1 盘点**：只读列出 `api_key is not null` 或 `api_base is not null` 的行 + 各行的
   `enabled`/`is_default`；同时确认生产 `LLM_BACKEND` 已是 `remote`（否则停在这里）。
2. **U2 凭证落位**：把每个 provider 的凭证写进模型服务侧真源（`MS_LITELLM_API_KEY` /
   `MS_LITELLM_API_BASE`，或密钥管理服务），**一对多**（一个 provider 凭证可服务多行）。
   落位后先只做一次**服务侧**直连冒烟（不经平台），验 200 + 真实 usage。
3. **U3 读侧改造（代码，先发版）**：`lookup_model_credentials` 停读两列并保留返回签名
   （返回 `(None, None)` 或直接把 `resolve_chat_model` 的两项删掉，连带 `chat.py:266-267/443-444`
   的传参）；admin schema 去掉 `api_key`/`api_base` 写入字段、`has_key` 改为恒 `false` 或直接删除
   （`app/schemas/llm_model.py:12-13, 20-21, 46-47`）。**这一步上生产并稳定一个观察窗后才进 U4。**
4. **U4 结构迁移**：`op.drop_column('llm_models', 'api_key')` + `('api_base')`，放在**单独一个
   revision** 里（不与其它改动混版本），以便 downgrade 只回退这一件事。
   列本身 nullable，无索引/约束牵连（`app/models/llm_model.py:23-24`），因此 drop 是廉价操作——
   廉价不等于可逆，见 §4。

## 3. downgrade 反向步骤（分界在 U4）

- **U4 之前**（含 U3 代码回滚）：`alembic downgrade -1` 不需要（尚未做结构变更），回滚 = 回退代码版本。
- **U4 之后**：本方案提供 `downgrade()` 重建两列（`sa.String(length=512), nullable=True`），
  但**列内容不可恢复**——它只把形状还回来。因此：
  1. `alembic downgrade -1` 重建空列；
  2. 从 §1.2 的 CSV 按 `has_key` 名单，配合 §1.3 映射表，把"哪些行需要凭证"重播为运维待办；
     明文只能回到各真源去取（这正是当初要把凭证搬出数据库的理由，回滚路径不会绕过它）；
  3. 若要求"连数据一起回到迁移前"，**唯一手段是 §1.2 的库快照整表恢复**（`pg_restore --table=llm_models`
     或替换 SQLite 文件），并接受恢复点之后该表的人工改动丢失。
- 明确的措辞纪律：**U4 之后不存在"无损回滚"**。文档、工单与提交信息里都不要写"可随时回退"。

## 4. 过渡期的两列读写策略（关键：禁止双写）

| 时期 | 读 | 写 | 说明 |
|---|---|---|---|
| T0（现在） | 平台读列 | admin 表单写列 | 现状，不动 |
| T1（U2 后、U3 前） | 平台读列（仅 `litellm` 用得到） | **停止接受新写入**：admin 表单标只读/灰化，后端 PUT 忽略这两字段并记一条 INFO | 只停写、不改读 ⇒ 任一时间点都只有一份真源在生效 |
| T2（U3 后） | 不读 | 不写（列成死列） | 观察窗：看 `upstream_auth_failed`/401 与 `cost_status:"unknown"` 是否上升 |
| T3（U4 后） | 列已不存在 | — | 唯一真源＝模型服务 env / 密钥管理 |

- **为什么禁止双写**：双写会产生"库里旧 key 与 service 新 key 不一致"的第三种状态，而读侧一旦命中
  旧值，故障表现是上游 401——排查时没人怀疑一个"已经迁走"的列。
- **明文不许出现在**：日志、提交信息、错误响应、admin 回显（现在 `has_key` 已经不回显，保持）。
- **另一条连带影响**：`sync_catalog` 从不启用同步来的行（协议 §9.1 / P2-1），所以迁完凭证后模型可用与否
  仍取决于运营在管理端启用——迁移完成的定义**包含**这一条运营核验，不能只看库结构。

## 5. 失败判据与回滚触发（可观测、有阈值）

| 信号 | 采集口径 | 触发线 | 动作 |
|---|---|---|---|
| 上游鉴权失败 | 平台侧 `code=upstream_auth_failed` 计数（§7 已标运维告警） | 5 分钟内 > 0 次，或相对基线上升 | 立即回滚 U3 代码（读侧恢复），不进 U4 |
| 熔断打开 | `app.llm.remote` 熔断状态（§11，阈值 `MODEL_SERVICE_CB_THRESHOLD`） | 由 closed 变 open | 同上，并按 §8.4 降级路径处理在服请求 |
| 成本口径劣化 | `usage_logs.cost_status='unknown'` / `metering_source != 'provider'` 占比 | 相对 U2 前基线上升 > 5 个百分点 | 说明 provider/价格表没接上，先修 U2 落位，不进 U3 |
| 目录/凭证错配 | 404 `model_not_found` 计数 | 出现即查 | 核对 `MS_CATALOG_MODELS` 与启用状态 |
| 端到端可用性 | 一次受控流式（内容 + `[DONE]` + usage 落地） | 失败即算失败 | 阻断下一步 |

- **观察窗长度**：T2 至少覆盖一个完整的重发/轮转周期（服务间 token 若同时在轮换，两个变量不能同窗改）。
- **U4 的执行窗口**：低峰、且**已确认 T2 全绿**；执行前再拍一次 §1.2 快照（U4 自己也要有回滚点）。

## 6. 开工条件与责任人（现在都不满足）

1. DW-4 已在**生产**翻到 `remote` + `native`（用户决定的是"先本地翻，生产不动" ⇒ **这一条未满足**）。
2. 有真实 provider 凭证可用（本机没有；DW-5 的动机之一就是不再散落，但落位动作需要人工持有）。
3. 用户单独立项并指定执行人；架构师确认 §4 的读写策略是否需要升格成契约条文
   （若升格，动的是 `docs/model-service-protocol.md`，不归实现方写）。
4. 迁移脚本、`downgrade` 实现与执行**都不属于本文范围**；本文只回答"怎么做才不丢数据、能否回滚"。

## 7. 本方案自身的取证边界

- §0 表里两行是**读码**结论（附文件行号），不是运行时观测。
- §1.2 SQLite 那句（WAL 半写副本）与 §5 各阈值的**数值**是按本项目既有口径给的起点，
  未做现网校准；开工前应由执行人用一次真实演练校准，别把 5 个百分点当真理。
- 迁移链单头（`e5f6a7b8c9d0`）是 2026-10-11 的 `alembic/versions/` 实测；若届时有并行迁移，
  本方案的 `down_revision` 需要重挂。
