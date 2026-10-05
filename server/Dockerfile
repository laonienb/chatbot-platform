# Chatbot Platform server — Python 3.12 + FastAPI
FROM python:3.12-slim

WORKDIR /app

# 先装依赖以利用层缓存
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

EXPOSE 8000

# 启动前自动跑迁移（失败即退出，避免带病启动）
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
