# chatbot-platform

[![CI](https://github.com/laonienb/chatbot-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/laonienb/chatbot-platform/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

自建聊天机器人平台：API 优先的"大脑"，多端接入的客户端体系。

- **Web / App / 小程序** 通过平台原生 API 聊天，每个用户可自定义对话人设
- **QQ / 微信等 IM** 通过 OpenAI 兼容 API 由 AstrBot + NapCat 等现成框架接入（大脑与四肢分离）
- **多用户**：平台账号是唯一身份源，IM/小程序身份可绑定到账号

## 文档

- [DESIGN.md](./DESIGN.md) — 总体设计方案（架构 / API / 数据模型 / 里程碑 / 风险），当前项目唯一真源

## 目录结构

```
chatbot-platform/
├── DESIGN.md            # 设计文档
├── server/              # 后端：Python + FastAPI
│   ├── app/
│   │   ├── api/native/        # /api/v1 平台原生 API（auth / personas / conversations / admin）
│   │   ├── api/openai_compat/ # /v1/chat/completions OpenAI 兼容层
│   │   ├── core/              # 配置、安全(JWT/APIKey)、限流
│   │   ├── models/            # SQLAlchemy 数据模型
│   │   ├── services/          # 业务逻辑（persona / conversation / usage）
│   │   └── llm/               # LiteLLM 封装、人设注入、上下文组装
│   ├── alembic/         # 数据库迁移
│   └── tests/
├── web/                 # 前端：Next.js
├── deploy/              # docker-compose、AstrBot/NapCat 配置
└── docs/                # API 文档、部署手册（待写）
```

## 当前状态

M0 后端骨架完成。里程碑规划见 DESIGN.md 第 11 节：

- [x] M0 骨架 — 脚手架、注册登录、人设 CRUD、会话、非流式对话、OpenAI 兼容层（mock LLM）、API Key
- [ ] M1 Web 可用 — SSE 流式、会话管理、LiteLLM 多模型
- [ ] M2 机器人接入 — AstrBot + NapCat 部署（兼容层 M0 已就位）
- [ ] M3 平台化 — 人设市场、配额限流、管理后台、小程序端
- [ ] M4 增强 — RAG 知识库、长期记忆、工具调用

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

# 跑测试（37 个用例，内存 SQLite，不依赖 .env）
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

Docker 方式（PostgreSQL + Redis + server）：

```bash
cd deploy && docker compose up --build
```

## 参与贡献

欢迎 Issue 与 PR，开发环境搭建与提交约定见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 许可证

[MIT](./LICENSE)
