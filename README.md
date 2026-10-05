# chatbot-platform

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

设计阶段。里程碑规划见 DESIGN.md 第 11 节：

- [ ] M0 骨架 — 脚手架、注册登录、人设 CRUD、非流式对话
- [ ] M1 Web 可用 — SSE 流式、会话管理、LiteLLM 多模型
- [ ] M2 机器人接入 — OpenAI 兼容层、API Key、AstrBot + NapCat 部署
- [ ] M3 平台化 — 人设市场、配额限流、管理后台、小程序端
- [ ] M4 增强 — RAG 知识库、长期记忆、工具调用

## 快速开始（M0 完成后补充）
