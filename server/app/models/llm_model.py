"""LLM 模型注册表：平台可用模型清单，一条记录一个可选项。

model 字段是 LiteLLM 模型串（如 gpt-4o-mini、openai/qwen、ollama/llama3、deepseek-chat）；
自部署（vLLM/Ollama）或第三方 API 通过 api_base + api_key 指定各自端点。
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import Uuid

from app.database import Base, utcnow


class LlmModel(Base):
    __tablename__ = "llm_models"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(64))  # 展示名
    model: Mapped[str] = mapped_column(String(128), unique=True)  # LiteLLM 模型串
    api_base: Mapped[str | None] = mapped_column(String(512))
    api_key: Mapped[str | None] = mapped_column(String(512))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
