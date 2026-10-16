FROM python:3.12-slim
WORKDIR /srv
COPY pyproject.toml ./
COPY msvc ./msvc
# 必须装 [real] extra：litellm 在 pyproject.toml 的 optional-dependencies 里，
# 而生产用 MS_UPSTREAM=litellm（deploy/docker-compose.prod.yml 注入，覆盖下面的 ENV）。
# 只装 "." 的话容器能起，但每次聊天都会在 msvc/provider.py 的 import litellm 处
# ModuleNotFoundError —— 属于"起来了但用不了"，故这里装全量。
RUN pip install --no-cache-dir ".[real]"
# 出厂默认 fake（离线确定性上游）；生产由 compose 注入 MS_UPSTREAM=litellm 覆盖
ENV MS_UPSTREAM=fake
EXPOSE 8000
CMD ["uvicorn", "msvc.main:app", "--host", "0.0.0.0", "--port", "8000"]
