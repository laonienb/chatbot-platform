FROM python:3.12-slim
WORKDIR /srv
COPY pyproject.toml ./
COPY msvc ./msvc
RUN pip install --no-cache-dir .
# 真实上游：pip install "model-service[real]"（含 litellm），并设 MS_UPSTREAM=litellm
ENV MS_UPSTREAM=fake
EXPOSE 8000
CMD ["uvicorn", "msvc.main:app", "--host", "0.0.0.0", "--port", "8000"]
