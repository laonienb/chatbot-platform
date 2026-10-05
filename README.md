# chatbot-platform

[![CI](https://github.com/laonienb/chatbot-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/laonienb/chatbot-platform/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

自建聊天机器人平台：API 优先的"大脑"，多端接入的客户端体系。

- **Web / App / 小程序** 通过平台原生 API 聊天，每个用户可自定义对话人设
- **QQ / 微信等 IM** 通过 OpenAI 兼容 API 由 AstrBot + NapCat 等现成框架接入（大脑与四肢分离）
- **多用户**：平台账号是唯一身份源，IM/小程序身份可绑定到账号

## 功能总览

**后端（FastAPI，双 API 面）**

- `/api/v1` 平台原生 API：JWT 注册登录（自动刷新）、人设 CRUD 与市场（发布/搜索/分类/fork）、会话与消息、长期记忆管理、API Key、用量查询、模型注册表（管理员）
- `/v1` OpenAI 兼容层：`model="persona:<slug>"` 即可让 AstrBot 等框架把人设当模型用
- SSE 流式（两套 API 同构）、非流式、重新生成、会话级模型覆盖
- 长期记忆：对话后自动提取事实 → 人设级记忆库 → 注入后续对话上下文（人设可开关）
- LLM 网关：mock（零 Key 开发）/ litellm（真实模型）双后端；模型注册表支持为每个模型配独立 `api_base`/`api_key`（自部署 vLLM/Ollama 或第三方中转均可）
- 计费与计量（开发中）：token 归因（人设/记忆/历史/输入拆分）、双阶段账本结算、费率规则与价格快照、充值钱包

**前端（Next.js 15 + React 19，深色精致主题）**

- 聊天页：人设/会话侧栏（搜索、移动端抽屉）、流式输出、停止/重新生成、模型下拉切换、Markdown 渲染、消息翻页、🧠 记忆管理弹窗、会话设置弹窗
- 人设市场：分类导航、搜索、fork 一键导入、发布者标识
- 密钥与用量页、账号设置（昵称/密码）、登录注册

## 目录结构

```
chatbot-platform/
├── DESIGN.md            # 设计文档（架构 / API / 数据模型 / 里程碑 / 风险），当前项目唯一真源
├── server/              # 后端：Python 3.12 + FastAPI + SQLAlchemy(async) + Alembic
│   ├── app/
│   │   ├── api/native/        # /api/v1 平台原生 API（auth / personas / conversations / memories / models / me）
│   │   ├── api/openai_compat/ # /v1/chat/completions OpenAI 兼容层
│   │   ├── billing/           # 计费：计价引擎 / token 归因 / 账本结算 / 费率 / 钱包
│   │   ├── core/              # 配置、安全(JWT/APIKey)、SSE
│   │   ├── llm/               # LLM 网关（mock / litellm 双后端）
│   │   ├── models/            # SQLAlchemy 数据模型
│   │   ├── schemas/           # Pydantic 请求/响应模型
│   │   └── services/          # 业务逻辑（chat / persona / memory）
│   ├── alembic/         # 数据库迁移
│   ├── scripts/         # make_admin 等运维脚本
│   └── tests/           # pytest（内存 SQLite，不依赖 .env）
├── web/                 # 前端：Next.js 15 + React 19（手写深色主题 CSS）
├── deploy/              # docker-compose（PostgreSQL + Redis + server）
└── docs/                # API 文档、部署手册（待写）
```

## 当前状态

进度快照（2026-10-05，里程碑规划见 DESIGN.md 第 11 节）：

- [x] M0 骨架 — 注册登录、人设 CRUD、会话、非流式对话、OpenAI 兼容层（mock LLM）、API Key
- [x] M1 Web 可用 — SSE 流式（双 API 面）、Next.js 聊天界面、me/usage 用量接口
- [x] M3 提前完成一部分 — 人设市场（发布/搜索/分类/fork）
- [x] M4 提前完成一部分 — 长期记忆（自动提取/注入/管理）；聊天记录蒸馏流水线已设计（DESIGN.md §13.2）待实现
- [x] 聊天增强 — 会话级模型切换、模型注册表、停止/重新生成、Markdown、局域网访问（同源代理）
- [ ] 计费与计量 — 进行中：归因/账本/费率/钱包的模型与服务层已落，收尾中
- [ ] M2 机器人接入 — AstrBot + NapCat 部署（兼容层已就位）
- [ ] M3 剩余 — 配额限流、管理后台、小程序端
- [ ] M4 剩余 — 蒸馏流水线、RAG 知识库、工具调用

## 快速开始（后端）

依赖：Python 3.12+。数据库默认用 SQLite 零配置；LLM 默认 mock 后端（无需任何 API Key，回复带 `[mock:*]` 标识）。

```bash
cd server
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"      # Linux/macOS: .venv/bin/pip
cp .env.example .env

# 初始化数据库
.venv/Scripts/alembic upgrade head          # Linux/macOS: .venv/bin/alembic

# 起服务（http://127.0.0.1:8000，文档在 /docs）
.venv/Scripts/uvicorn app.main:app --reload

# 跑测试（61 个用例，内存 SQLite，不依赖 .env）
.venv/Scripts/pytest
```

最小闭环（有状态链路）：

```bash
# 1. 注册（返回 access_token）
curl -X POST http://127.0.0.1:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"me@example.com","password":"password123"}'

# 2. 建人设 → 3. 建会话 → 4. 发消息拿回复（见 /docs 的 Swagger 调试）
```

OpenAI 兼容层（无状态，供 AstrBot 等框架接入）：

```bash
# 先在 /docs 里调 POST /api/v1/me/keys 生成 sk- 开头的 Key
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-你的Key" -H "Content-Type: application/json" \
  -d '{"model":"persona:人设slug","messages":[{"role":"user","content":"你好"}]}'
```

接入真实模型：复制 `.env` 中 `LLM_BACKEND=litellm` 并配 `LITELLM_API_KEY`/`LITELLM_API_BASE`，或起服务后在 `/docs` 用管理员账号把模型注册进 `llm_models`（可逐模型配端点与凭证）。

Docker 方式（PostgreSQL + Redis + server）：

```bash
cd deploy && docker compose up --build
```

## Web 前端

依赖 Node.js 20+（开发时用 Node 24 验证）：

```bash
cd web
npm install
npm run dev     # http://localhost:3000
```

功能：登录注册（JWT 自动刷新）、聊天（流式/停止/重新生成/模型切换/记忆管理）、人设市场、密钥与用量。

前端默认走 Next.js 同源反向代理访问后端（`next.config.mjs` 里的 rewrites，`BACKEND_URL` 可覆盖，默认 `http://127.0.0.1:8000`）——因此局域网内其他设备直接访问 `http://<电脑IP>:3000` 即可，无需配跨域。若前后端分开部署，用 `NEXT_PUBLIC_API_BASE` 指向后端地址。

## 参与贡献

欢迎 Issue 与 PR，开发环境搭建与提交约定见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 许可证

[MIT](./LICENSE)
