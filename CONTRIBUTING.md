# 参与贡献

感谢关注本项目！当前处于早期阶段（M0 完成），接口与数据模型还在快速迭代，欢迎以下形式的贡献：

- Issue：报 bug、提需求、讨论设计
- PR：修 bug、补测试、完善文档
- 大的功能改动建议先开 Issue 讨论再动手（对应 DESIGN.md 的里程碑规划）

## 开发环境搭建

依赖 Python 3.12+，数据库默认 SQLite 零配置，LLM 默认 mock 后端（无需任何 API Key）：

```bash
cd server
python -m venv .venv
.venv/bin/pip install -e ".[dev]"   # Windows: .venv\Scripts\pip
cp .env.example .env
.venv/bin/alembic upgrade head
.venv/bin/pytest
```

## 提交约定

- 提交信息用祈使句并注明范围，如 `feat(server): ...`、`fix(web): ...`、`docs: ...`
- 新功能必须带 pytest 测试；改数据模型必须生成 Alembic 迁移（CI 会验证迁移可升级/降级）
- 提 PR 前确保本地 `pytest` 全绿

## 设计约定

架构与接口以 [DESIGN.md](./DESIGN.md) 为唯一真源，改设计先改文档再改代码。
小改动请同步更新 DESIGN.md 对应小节。

## 许可

提交即表示同意以 [MIT License](./LICENSE) 许可你的贡献。
