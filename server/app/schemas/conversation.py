from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ConversationCreate(BaseModel):
    persona_id: UUID
    title: str | None = Field(default=None, max_length=128)
    channel: str = Field(default="web", pattern=r"^(web|qq|wechat|miniprogram)$")


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=128)
    pinned: bool | None = None


class ConversationOut(BaseModel):
    id: UUID
    persona_id: UUID
    channel: str
    title: str | None
    pinned: bool
    last_message_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageSendIn(BaseModel):
    content: str = Field(min_length=1, max_length=32000)
    stream: bool = False  # M0 仅支持非流式；流式在 M1 落地


class MessageOut(BaseModel):
    id: UUID
    conversation_id: UUID
    role: str
    content: str
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class SendMessageOut(BaseModel):
    """发送消息的响应：用户消息 + 助手回复。"""

    user_message: MessageOut
    assistant_message: MessageOut
