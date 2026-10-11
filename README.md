# chatbot-platform

[![CI](https://github.com/laonienb/chatbot-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/laonienb/chatbot-platform/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

自建聊天机器人平台：API 优先的"大脑"，多端接入的客户端体系。

- **Web / App / 小程序** 通过平台原生 API 聊天，每个用户可自定义对话人设
- **QQ / 微信等 IM** 通过 OpenAI 兼容 API 由 AstrBot + NapCat 等现成框架接入（大脑与四肢分离）
- **多用户**：平台账号是唯一身份源，IM/小程序身份可绑定到账号

## 功能总览

**后端（FastAPI，双 API 面）**

- `/api/v1` 平台原生 API：JWT 注册登录（自动刷新）、人设 CRUD 与市场（发布/搜索/分类/fork）、会话与消息、长期记忆管理、API Key、用量查询、钱包余额查询、模型注册表（管理员）
- `/v1` OpenAI 兼容层：`model="persona:<slug>"` 即可让 AstrBot 等框架把人设当模型用
- SSE 流式（两套 API 同构）、非流式、重新生成、会话级模型覆盖
- 长期记忆：对话后自动提取事实 → 人设级记忆库 → 注入后续对话上下文（人设可开关）
- LLM 接入：`mock`（零 Key 开发）/ `remote`（**独立模型服务**，生产目标态：凭证/路由/重试降级/成本归因/合规收口，见 `model-service/` 与 `docs/model-service-protocol.md`）/ `litellm`（旧直连，退役中）；模型注册表支持展示目录与逐模型配置
- 计费与计量：token 归因（人设/记忆/历史/输入拆分）、双阶段账本结算（幂等/价格快照）、费率规则、准入（月度配额 429 + 积分余额 402）、结算即扣钱包、注册赠送、RPM 限流、对账循环与毛利告警、管理端 `/admin/billing/*`

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
│   │   ├── llm/               # RemoteBackend（模型服务客户端）/ gateway / mock
│   │   ├── models/            # SQLAlchemy 数据模型
│   │   ├── schemas/           # Pydantic 请求/响应模型
│   │   └── services/          # 业务逻辑（chat / persona / memory）
│   ├── alembic/         # 数据库迁移
│   ├── scripts/         # make_admin 等运维脚本
│   └── tests/           # pytest（每测独立临时 SQLite，不依赖 .env）
├── model-service/       # 独立模型服务（v0.3）：凭证/路由/重试降级/成本归因/合规/幂等窗
├── web/                 # 前端：Next.js 15 + React 19（手写深色主题 CSS）
├── deploy/              # docker-compose（PostgreSQL + Redis + server；model-service 待落地）
└── docs/                # 模型服务协议契约与设计、计费接线说明、前端工作报告
```

## 当前状态

进度快照（2026-10-10，里程碑规划见 DESIGN.md 第 11 节）：

- [x] M0 骨架 — 注册登录、人设 CRUD、会话、非流式对话、OpenAI 兼容层（mock LLM）、API Key
- [x] M1 Web 可用 — SSE 流式（双 API 面）、Next.js 聊天界面、me/usage 用量接口
- [x] M3 提前完成一部分 — 人设市场（发布/搜索/分类/fork）；配额与限流（配额准入 429、余额准入 402、RPM 限流）；管理后台（毛利概览/负毛利明细/手动对账、模型管理 CRUD）
- [x] M4 提前完成一部分 — 长期记忆（自动提取/注入/管理）；聊天记录蒸馏流水线已设计（DESIGN.md §13.2）待实现
- [x] 聊天增强 — 会话级模型切换、模型注册表、停止/重新生成、Markdown、局域网访问（同源代理）
- [x] 计费与计量 — 归因/双阶段账本/费率/钱包扣费/准入/对账与毛利监控（`docs/compose/spec/billing-wiring.md`）；余额查询与展示已上线，用户侧充值未做
- [x] **模型服务解耦（2026-10-10 全单闭合）** — 独立 `model-service/` + 平台 `RemoteBackend`，契约 v1.7（`docs/model-service-protocol.md`），契约测试双侧全绿（40+180）；生产切换清单见 DESIGN.md §10
- [ ] 生产切换 — compose 落地 model-service + 切换清单（**代码侧①真实上游流式与错误映射已闭合 `c5ff7bb`**；余为部署/运维，见 DESIGN.md §10）
- [ ] M2 机器人接入 — AstrBot + NapCat 部署（兼容层已就位）
- [ ] M3 剩余 — 小程序端
- [ ] M4 剩余 — 蒸馏流水线（真实 LLM 通路已就绪，阻塞解除）、RAG 知识库、工具调用

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

# 跑测试（平台侧 180 个用例，每测独立临时 SQLite，不依赖 .env；模型服务侧另有 40 个契约用例）
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

接入真实模型（生产目标态）：起 `model-service/`（provider 凭证配在它那边），平台 `.env` 设 `LLM_BACKEND=remote` + `MODEL_SERVICE_BASE_URL` + `MODEL_SERVICE_TOKEN` + `MODEL_SERVICE_MODE=native`；生产切换清单见 `DESIGN.md` §10（真实上游流式与错误映射**已实现并验收** `c5ff7bb`——SW-1/SW-4；切换前请确认 §10 ②–⑦ 就绪）。旧直连方式（`LLM_BACKEND=litellm` + `LITELLM_API_KEY`/`LITELLM_API_BASE`）仍可用但退役中。

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

功能：登录注册（JWT 自动刷新）、聊天（流式/停止/重新生成/模型切换/记忆管理/记录导出 Markdown·JSON）、人设市场、密钥与用量、模型管理（admin）、深浅双主题（账号设置切换，localStorage 持久化）。

前端默认走 Next.js 同源反向代理访问后端（`next.config.mjs` 里的 rewrites，`BACKEND_URL` 可覆盖，默认 `http://127.0.0.1:8000`）——因此局域网内其他设备直接访问 `http://<电脑IP>:3000` 即可，无需配跨域。若前后端分开部署，用 `NEXT_PUBLIC_API_BASE` 指向后端地址。

## 参与贡献

欢迎 Issue 与 PR，开发环境搭建与提交约定见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 许可证

[MIT](./LICENSE)
