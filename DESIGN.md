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

## 12. 风险与对策

| 风险 | 对策 |
|---|---|
| NapCat 封号 | 专用小号 + 限频 + 关注协议端更新；合规退路是 QQ 开放平台官方 API |
| 个人微信协议封号 | 不做个人微信协议接入；走企业微信/服务号 |
| 兼容层被滥用 | API Key 作用域 + 模型白名单 + 配额硬顶 + 限流 |
| 上下文成本失控 | 上下文窗口策略（超长截断/滚动摘要，参考 AstrBot 的压缩机制）；usage 硬顶 |
| 人设内容合规 | 市场发布走审核态；系统级内容安全过滤预留 |
