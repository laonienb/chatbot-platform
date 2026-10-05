# 聊天机器人平台设计方案（API 优先）

> 版本：v0.1（2026-10-05）
> 状态：设计稿，待评审

## 1. 设计原则

1. **API 优先**：平台所有能力通过 API 暴露。Web、App、小程序、第三方机器人框架（AstrBot/LangBot）都只是 API 的客户端，客户端可以随时增减，核心不变。
2. **大脑与四肢分离**：平台只做"大脑"——账号、人设、会话编排、计费。IM 协议接入（QQ/微信的登录与收发消息）全部交给现成框架（AstrBot + NapCatQQ 等），平台不碰任何协议层，规避风控维护成本。
3. **OpenAI 兼容**：平台对外提供 OpenAI 兼容端点，让自己可以"被当成一个模型"接入任何现有工具——这是接入层零开发的根本手段。
4. **身份统一**：平台账号（users 表）是唯一身份源。QQ 号、微信号、小程序 openid 等外部身份通过绑定挂到平台账号上，同一人在所有端共享人设与记忆。

## 2. 总体架构

```
                       ┌──────────────────────────────────────┐
   第一方客户端          │              平台服务（大脑）           │
   ┌──────────┐        │  ┌────────────┐  ┌─────────────────┐  │
   │ Web 前端  │──JWT──▶│  │ 原生 REST   │  │  业务层           │  │
   │ App      │        │  │ /api/v1    │  │  账号·人设·会话    │  │
   │ 小程序    │        │  └────────────┘  │  配额·计费·管理    │  │
   └──────────┘        │  ┌────────────┐  └────────┬────────┘  │
                       │  │ OpenAI 兼容 │           ▼           │
   第三方机器人框架       │  │ /v1        │  ┌─────────────────┐  │
   ┌──────────┐──APIKey▶│  └────────────┘  │ LLM 网关 LiteLLM │  │
   │ AstrBot  │        │                   └───────┬─────────┘  │
   │ NapCatQQ │        └───────────────────────────┼────────────┘
   └────┬─────┘                                    ▼
        │                                    OpenAI / Claude /
   QQ 用户、微信群…                              Gemini / DeepSeek…
```

## 3. 客户端接入矩阵

| 客户端 | 认证 | API 面 | 流式方案 |
|---|---|---|---|
| Web 前端 | JWT（邮箱/手机登录） | 原生 REST | SSE |
| App（iOS/Android） | JWT | 原生 REST | SSE |
| 微信小程序 | `wx.login` code 换 JWT | 原生 REST | `wx.request` + `enableChunked` |
| AstrBot / LangBot 等 | API Key | OpenAI 兼容 `/v1` | 一次性返回（AstrBot 自己管会话） |
| 其他 OpenAI 生态工具 | API Key | OpenAI 兼容 `/v1` | SSE（同 OpenAI 格式） |

## 4. 认证体系

两类凭证、一个身份模型：

- **JWT**（第一方客户端）：短期 access token（15min）+ refresh token（30d）。签发方支持可插拔的登录方式：邮箱密码、手机验证码、微信 OAuth（网站/App）、小程序 `code2session`。
- **API Key**（机器客户端）：`sk-` 前缀，服务端只存哈希。可配置：作用域（允许的模型/人设）、速率限制、配额上限、过期时间。给 AstrBot 用的 key 就是一个普通 API Key。

**身份绑定**（identities 表）：`user_id + provider + provider_uid` 唯一。
绑定流程（后期自建 IM 适配器或 AstrBot 插件实现）：用户在 Web 生成 8 位绑定码（10 分钟有效）→ 在 QQ/微信私聊机器人发送绑定码 → 服务端校验后把 `qq:12345` 挂到该用户账号上。

## 5. API 设计

API 版本策略：OpenAI 兼容面固定 `/v1/*`（跟随 OpenAI 规范）；平台自有 API 用 `/api/v1/*`。

### 5.1 OpenAI 兼容层（平台"变成一个模型"）

```
POST /v1/chat/completions
Authorization: Bearer sk-xxx
{
  "model": "persona:libai",        // 人设即模型名
  "messages": [ {"role":"user","content":"用李白的口吻写一首诗"} ],
  "stream": false,
  "user": "qq:12345"               // 来源身份，用于用量归因（OpenAI 规范字段）
}
```

设计要点：

- **无状态模式**：调用方（AstrBot）自己维护多轮历史并全量传 `messages`，平台不做会话存储（只记用量日志）。AstrBot 本来就是自己管历史的，天然契合。
- **人设即模型名**：`model` 解析顺序：
  1. `persona:<slug>` → 查 personas 表，注入人设 system prompt；
  2. 其他值 → 原样作为真实模型名传给 LiteLLM（`gpt-4o`、`deepseek-chat`…）。
- **兜底注入**：客户端不便改 `model` 时，支持 header `X-Persona: <slug>` 覆盖人设选择。
- **平台侧注入的内容**：人设 system prompt（拼接在用户消息最前）、后期加入长期记忆/知识库检索结果。平台是人格的唯一真源——AstrBot 侧的"人格情景"配置留空，避免两层 system prompt 冲突。
- 返回与错误格式完全遵循 OpenAI 规范；`usage` 记入 usage_logs 并记账到 API Key。

### 5.2 平台原生 API（第一方客户端）

认证类：

```
POST /api/v1/auth/register            # 邮箱+密码
POST /api/v1/auth/login               # → access_token + refresh_token
POST /api/v1/auth/refresh
POST /api/v1/auth/wechat-miniprogram  # {code} → 服务端 code2session → 建身份+发 JWT
POST /api/v1/auth/bind-codes          # 生成 IM 绑定码
```

人设类（核心差异化功能）：

```
GET    /api/v1/personas               # 我的 + 可见的公共人设
POST   /api/v1/personas               # 创建
PATCH  /api/v1/personas/{id}
DELETE /api/v1/personas/{id}
POST   /api/v1/personas/{id}/publish  # 发布到公共市场（进入审核态）
GET    /api/v1/personas/market        # 公共市场（搜索/标签/排序）
POST   /api/v1/personas/{id}/fork     # 复制他人人设为自己的私有副本
```

会话类：

```
POST   /api/v1/conversations                # {persona_id, title?}
GET    /api/v1/conversations                # 我的会话列表
DELETE /api/v1/conversations/{id}
PATCH  /api/v1/conversations/{id}           # 改标题/置顶
POST   /api/v1/conversations/{id}/messages  # {content, stream:true} → SSE
GET    /api/v1/conversations/{id}/messages  # 翻历史
```

用量与管理：

```
GET  /api/v1/me/usage                       # 我的 token 消耗
GET  /api/v1/me/keys                        # 管理 API Key（生成/吊销）
GET  /api/v1/admin/users                    # 管理员：用户管理
GET  /api/v1/admin/stats                    # 管理员：全站用量/成本
```

**流式协议**：SSE，数据块格式直接复用 OpenAI 的 chunk 格式（`data: {"choices":[{"delta":{"content":"..."}}]}\n\n`，以 `data: [DONE]` 结束）。第一方客户端与兼容层同构，客户端解析代码可复用；小程序用 `enableChunked: true` + `onChunkReceived` 消费同一格式。

**限流**：Redis 令牌桶，三个维度分别限：per API Key、per 用户、per 模型。超限返回 429 + `Retry-After`。

## 6. 数据模型（PostgreSQL）

```sql
-- 平台账号（唯一身份源）
users(
  id uuid PK, email text UNIQUE, password_hash text,
  display_name text, role text DEFAULT 'user',      -- user / admin
  status text DEFAULT 'active', created_at timestamptz
);

-- 外部身份绑定：一个用户可绑多个 QQ/微信/小程序身份
identities(
  id uuid PK, user_id FK→users,
  provider text,            -- qq / wechat / wecom / miniprogram / web
  provider_uid text,        -- QQ号 / openid
  meta jsonb,
  UNIQUE(provider, provider_uid)
);

-- API Key（给 AstrBot 等机器客户端）
api_keys(
  id uuid PK, user_id FK→users, name text,
  key_hash text, key_prefix text,          -- 只存哈希，前缀用于展示识别
  model_whitelist text[],                  -- 可用模型/人设范围
  rpm_limit int, daily_token_limit bigint,
  expires_at timestamptz, last_used_at timestamptz, revoked bool
);

-- 人设（核心表）
personas(
  id uuid PK, owner_id FK→users,
  slug text UNIQUE,                        -- 用于 model="persona:<slug>"
  name text, avatar_url text,
  system_prompt text,                      -- 人设正文
  model text,                              -- 底层模型偏好
  temperature real, top_p real, max_tokens int,
  opening_message text,                    -- 开场白
  visibility text DEFAULT 'private',       -- private / public(市场)
  status text DEFAULT 'active',            -- public 需过审核态
  forked_from uuid NULL, tags text[],
  created_at timestamptz
);

-- 会话（有状态模式，第一方客户端用）
conversations(
  id uuid PK, user_id FK→users, persona_id FK→personas,
  channel text DEFAULT 'web',              -- web / qq / wechat / miniprogram
  title text, pinned bool DEFAULT false,
  last_message_at timestamptz, created_at timestamptz
);

messages(
  id uuid PK, conversation_id FK→conversations,
  role text,                               -- user / assistant / system
  content text,
  model text, prompt_tokens int, completion_tokens int,
  created_at timestamptz
);

-- IM 会话路由策略（为后期自建 IM 适配器/AstrBot 插件预留）
im_bindings(
  id uuid PK,
  platform text, platform_chat_id text,    -- 群号 / 私聊身份
  persona_id FK→personas,
  trigger_mode text DEFAULT 'at',          -- at / always / wake_word
  wake_word text, enabled bool DEFAULT true,
  UNIQUE(platform, platform_chat_id)
);

-- 用量账本（计费与配额的唯一依据）
usage_logs(
  id uuid PK, user_id FK→users, api_key_id FK→api_keys NULL,
  conversation_id FK→conversations NULL,   -- 兼容层调用为空
  model text, persona_id uuid NULL,
  prompt_tokens int, completion_tokens int, cost numeric,
  created_at timestamptz
);
```

预留：知识库用 `pgvector` 扩展（`documents`/`chunks` 表，M4 阶段加）；长期记忆 `memories(user_id, persona_id, content, embedding)`。

## 7. 关键流程

### 7.1 Web 聊天（有状态）

```
用户发消息 → POST /conversations/{id}/messages {stream:true}
→ 服务端载入会话历史 → 组装 [persona.system_prompt + 历史 + 新消息]
→ LiteLLM 调模型 → SSE 逐块返回 → 落库 messages + usage_logs
```

### 7.2 QQ 消息（经 AstrBot，无状态）

```
QQ消息 → NapCatQQ(OneBot v11) → AstrBot 判断触发(被@/唤醒词)
→ AstrBot 取自己维护的上下文 → POST /v1/chat/completions
  (model="persona:xxx", user="qq:12345")
→ 平台注入人设 → LiteLLM → 返回 → AstrBot 发回 QQ
```

AstrBot 侧每个群/会话用哪个 persona，由 AstrBot 的会话配置或插件决定（群绑定策略在平台侧是 im_bindings，AstrBot 插件可读它来决定 model 名）。

### 7.3 小程序登录

```
wx.login() 取 code → POST /auth/wechat-miniprogram {code}
→ 服务端 code2session 拿 openid → 查/建 identity → 查/建 user
→ 签发 JWT
```

## 8. 技术选型

| 层 | 选型 | 理由 |
|---|---|---|
| 后端 | Python 3.12 + FastAPI + SQLAlchemy + Alembic | IM/LLM 生态最全；async 原生支持 SSE 流式 |
| LLM 网关 | LiteLLM | 多模型路由/重试/降级/成本核算，不自己造 |
| 数据库 | PostgreSQL + pgvector | 业务数据与向量（知识库/记忆）一库搞定 |
| 缓存/限流/队列 | Redis | 令牌桶限流、会话缓存、绑定码 TTL |
| 前端 | Next.js + React | Web 与后续小程序生态近；SSE 消费简单 |
| 文件存储 | MinIO（自托管）/ S3 | 头像、知识库文档 |
| 部署 | Docker Compose | 单机起步够用，服务边界已按可拆分设计 |

## 9. 仓库结构（monorepo）

```
chatbot-platform/
├── server/
│   ├── app/
│   │   ├── api/
│   │   │   ├── native/        # /api/v1：auth, personas, conversations, admin
│   │   │   └── openai_compat/ # /v1/chat/completions
│   │   ├── core/              # 配置、安全(JWT/APIKey)、依赖注入、限流
│   │   ├── models/            # SQLAlchemy 模型
│   │   ├── services/          # persona / conversation / usage / binding 业务逻辑
│   │   └── llm/               # LiteLLM 封装、persona 注入、上下文组装
│   ├── alembic/               # 迁移
│   └── tests/
├── web/                       # Next.js 前端
├── deploy/
│   ├── docker-compose.yml     # server, web, postgres, redis, litellm, napcat, astrbot
│   └── astrbot/               # AstrBot 配置（provider 指向平台 /v1）
└── docs/                      # API 文档、部署手册
```

## 10. 部署拓扑与合规注意

单机 Compose 起步，服务：`server`（8000）、`web`（Nginx 托管静态+反代）、`postgres`、`redis`、`litellm`、`napcat`（QQ 协议端）、`astrbot`。

小程序/微信侧的现实约束（影响部署而非架构）：

1. 小程序 request 合法域名要求 **HTTPS + 已备案域名**，上线前需要准备域名与证书；
2. 个人主体小程序无法使用部分类目，涉及 UGC 需评估类目与内容安全审核义务；
3. QQ 个人号接 NapCat 有封号风险——**必须用专用小号**并限频；微信侧合规路径是企业微信应用/认证服务号。

## 11. 里程碑

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| M0 骨架（1 周） | 项目脚手架、users/auth、personas CRUD、非流式对话 | 能注册登录、建人设、发一条消息拿到回复 |
| M1 Web 可用（1-2 周） | SSE 流式、会话管理、LiteLLM 多模型、usage 记账 | 日常可用的 Web 聊天界面 |
| M2 机器人接入（1 周） | `/v1/chat/completions`、API Key 管理、AstrBot+NapCat 部署文档 | QQ 群里 @机器人 以指定人设回复 |
| M3 平台化（2-4 周） | 人设市场、配额与限流、管理后台、小程序端 | 每个用户有私有人设库并可发布分享 |
| M4 增强 | RAG 知识库(pgvector)、长期记忆、工具调用、IM 绑定码打通身份 | 人设可挂知识库；QQ 身份与 Web 账号打通 |

### 11.1 实际进度快照（2026-10-05）

实际推进与原规划有交叉（多块内容提前落地），当前状态：

| 已完成 | 说明 |
|---|---|
| M0 + M1 全部 | 注册登录/人设/会话/SSE 流式（双 API 面）/Web 聊天界面/用量接口 |
| M3 的人设市场 | 发布/可见性/搜索/分类标签/fork（缺审核态） |
| M4 的长期记忆 | §13.1 已实现：对话后自动提取 → 人设级记忆库 → 注入上下文 → Web 记忆管理 |
| 聊天增强 | 会话级模型切换、llm_models 模型注册表（管理员 API，支持逐模型独立端点凭证）、停止/重新生成、Markdown 渲染、消息翻页、局域网访问（同源代理） |
| UI 精致化 | 深色主题样式系统（渐变/毛玻璃/统一动效） |

进行中：**计费与计量**（billing 模块）—— token 归因（persona/记忆/历史/输入拆分）、usage_logs 双阶段账本（pending→settled 状态机、幂等键、价格快照、实测/估算计量口径）、费率规则（upstream/retail 双口径）、充值钱包。模型与服务层已落，chat 链路已接入 reserve/settle；剩余：wallets/rating_rules 建表迁移、兼容层接入、计费测试与对账任务。

未开始（按建议优先级）：模型管理前端界面（需 admin 角色）→ **聊天记录蒸馏流水线**（§13.2，用户核心需求，需先接真实 LLM）→ M2 机器人接入（AstrBot+NapCat 部署文档）→ M3 剩余（配额限流/管理后台/小程序）→ M4 剩余（RAG/工具调用）→ PostgreSQL 生产切换。

## 12. 风险与对策

| 风险 | 对策 |
|---|---|
| NapCat 封号 | 专用小号 + 限频 + 关注协议端更新；合规退路是 QQ 开放平台官方 API |
| 个人微信协议封号 | 不做个人微信协议接入；走企业微信/服务号 |
| 兼容层被滥用 | API Key 作用域 + 模型白名单 + 配额硬顶 + 限流 |
| 上下文成本失控 | 上下文窗口策略（超长截断/滚动摘要，参考 AstrBot 的压缩机制）；usage 硬顶 |
| 人设内容合规 | 市场发布走审核态；系统级内容安全过滤预留 |

---

## 13. 长期记忆与聊天记录蒸馏（v0.2 增补，2026-10-05）

> 背景：用户需要「从微信/QQ 导出的聊天记录蒸馏出一个人物」（数字分身）。
> 结论：蒸馏与长期记忆共用同一套底层设施 —— 蒸馏只是记忆的"批量导入来源"之一，
> 产物 = 1 个 persona（风格）+ N 条 memories（事实），使用完全复用现有聊天链路。

### 13.1 记忆模型（长期记忆，M4 提前实现）

```sql
memories(
  id uuid PK,
  user_id uuid FK→users,           -- 记忆归属用户
  persona_id uuid FK→personas,     -- 归属人设（同一用户每个人设独立记忆）
  content text,                     -- 事实本身（"用户养了只猫叫橘子"）
  category text,                    -- basic/preference/relationship/event/opinion/style
  source text,                      -- chat(对话中自动提取) / import(蒸馏导入) / manual(手填)
  source_ref jsonb NULL,            -- 来源定位（原句摘录/聊天记录位置）——蒸馏与审计用
  confidence real DEFAULT 1.0,     -- 重复次数/置信度，去重合并时用
  embedding vector(1536) NULL,     -- M4 启用 pgvector 后补
  created_at timestamptz
);
```

**注入策略（记忆的使用方式）**：会话组装上下文时，在 system prompt 末尾追加
`[你已知的关于用户的事实]` 段 —— 记忆 ≤30 条全量注入；超过后按当前对话检索
top-k（M4 上 pgvector 向量检索，此前用关键词/最近优先降级）。

**持续学习**：每轮对话后由 LLM 判断是否有值得记住的新事实（廉价模型、低频触发），
写入 memories；用户可在界面查看/编辑/删除（隐私可控，不做成黑箱）。

### 13.2 聊天记录蒸馏流水线

```
导入(微信/QQ导出文件) → 解析适配器(统一格式) → 分批 Map(LLM提取事实+风格样本)
→ Reduce(合并去重/归纳风格画像) → 草稿( persona草稿 + 记忆候选列表 )
→ 人工审阅编辑 → 入库( persona + memories[source=import] )
```

- **输入适配**：微信推荐用 MemoTrace（留痕）导出的 CSV（发言人/时间/内容）；
  QQ 用 NTQQ 导出 txt。解析器做成可插拔 adapter，统一转成 `{speaker, time, text}` 流。
- **本体选择**：导入时指定记录里"谁是本体"（按发言人选择）—— 蒸馏自己也行（自己的
  分身），蒸馏他人（如亲人）也行，提供记录即视为有权。
- **Map-Reduce**：记录量大（数万条），按时间窗口分批喂给便宜模型分批提取，
  再合并：同一事实保留更晚/更高频版本并累加 confidence；风格画像汇总口头禅、
  句式、性格、立场 → 生成 persona.system_prompt 草稿。
- **人工审阅必选**：蒸馏产物先以草稿呈现，用户勾选/编辑记忆条目、修改风格描述，
  确认后才创建 persona 与入库。蒸馏不追求一次完美。

### 13.2.1 贴近真人的要求（保真度三要素）

1. **数据（根本，占一半）**：本体消息（不是对方的）≥3000 条效果较好，几百条只能出轮廓；
   覆盖多个聊天对象、多个时期、多种场景（闲聊/正事/玩笑）——单一对象单一话题只能学会
   那一个切面；文字为主的记录最佳，表情包/语音在导出中是占位符（表情偏好可另行统计）。
   私聊优先于群聊（群聊上下文碎）。
2. **提取方法（系统责任）**：
   - **Few-shot 原句**：从记录挑选本体最有代表性的对话片段原文放入 persona，LLM 模仿
     原句远比模仿描述准确——这是保真度最大杠杆；
   - **规则统计**（不耗 LLM）：口头禅 top-N、句长分布、标点习惯（省略号/波浪线）、
     「哈哈」重复次数、称呼方式——这些细节最容易被 LLM 忽略却最影响"像不像"；
   - 事实与风格分开提取、分批多遍。
3. **运行时（系统责任）**：persona 建议绑定较强模型（小模型演不像）；prompt 显式列出
   禁止项（不自称 AI、不用书面腔、回复长度贴合本人均值——多数人回消息只有一两个词）；
   temperature 用本人实际风格校准。

**期望管理**：目标是熟人短对话不穿帮，不是 100% 复刻；语音/方言无法还原；
越贴近越需人工在草稿审阅和日常聊天中迭代校准（后续可加"不像之处"反馈入口）。
- **长任务**：异步后台执行（asyncio task + 进度写 DB，前端轮询），避免请求超时。

### 13.3 蒸馏产物的使用（与现有链路的适配）

- 蒸馏完成即得到一个**普通 persona**：在会话中选择它 → 走现有注入机制
  （风格 prompt + 该人物 memories + 历史）→ 以该人物口吻回应。
- 记忆与 persona 分离存储：换底层模型重建 persona 时记忆保留；同一人物的记忆
  可导出/迁移。
- **持续进化**：与数字分身继续聊天时可开启"持续学习"，新事实按 13.1 正常累积。
- **合规**：蒸馏 persona 带 source=distilled 标记，发布到市场强制走审核态
  （防冒充真人）；导入的原文仅用于蒸馏不长期保留，记忆只存事实与必要摘录。

### 13.4 里程碑排序

1. 长期记忆（13.1，先行 —— 表结构已按蒸馏需求预留 source/category/confidence）
2. 聊天记录导入 + 蒸馏流水线（13.2/13.3）
3. M4 的 pgvector 检索升级（记忆量大后自动启用）
