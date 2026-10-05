"""会话：创建（注入开场白）/ 列表 / 改名置顶 / 删除 / 发消息（M0 非流式）/ 翻历史。"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import delete, select

from app.api.deps import CurrentUser, DbSession
from app.models import Conversation, Message, Persona
from app.schemas.conversation import (
    ConversationCreate,
    ConversationOut,
    ConversationUpdate,
    MessageOut,
    MessageSendIn,
    SendMessageOut,
)
from app.services.chat import send_message
from app.services.persona import can_view_persona

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


async def _get_owned_conversation(conversation_id: UUID, user_id, db) -> Conversation:
    conv = await db.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return conv


@router.post("", response_model=ConversationOut, status_code=status.HTTP_201_CREATED)
async def create_conversation(body: ConversationCreate, user: CurrentUser, db: DbSession):
    persona = await db.get(Persona, body.persona_id)
    if persona is None or not can_view_persona(persona, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "人设不存在")
    conv = Conversation(
        user_id=user.id,
        persona_id=persona.id,
        title=body.title or persona.name,
        channel=body.channel,
    )
    db.add(conv)
    await db.flush()
    if persona.opening_message:
        db.add(
            Message(
                conversation_id=conv.id,
                role="assistant",
                content=persona.opening_message,
            )
        )
        conv.last_message_at = conv.created_at
    await db.commit()
    await db.refresh(conv)
    return conv


@router.get("", response_model=list[ConversationOut])
async def list_conversations(user: CurrentUser, db: DbSession):
    stmt = (
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(Conversation.pinned.desc(), Conversation.last_message_at.desc().nulls_last())
    )
    return (await db.execute(stmt)).scalars().all()


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def update_conversation(
    conversation_id: UUID, body: ConversationUpdate, user: CurrentUser, db: DbSession
):
    conv = await _get_owned_conversation(conversation_id, user.id, db)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(conv, field, value)
    await db.commit()
    await db.refresh(conv)
    return conv


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(conversation_id: UUID, user: CurrentUser, db: DbSession):
    conv = await _get_owned_conversation(conversation_id, user.id, db)
    await db.execute(delete(Message).where(Message.conversation_id == conv.id))
    await db.delete(conv)
    await db.commit()


@router.post("/{conversation_id}/messages", response_model=SendMessageOut)
async def send(
    conversation_id: UUID, body: MessageSendIn, user: CurrentUser, db: DbSession
):
    if body.stream:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "流式输出将在 M1 支持，当前请使用 stream=false"
        )
    conv = await _get_owned_conversation(conversation_id, user.id, db)
    user_message, assistant_message = await send_message(db, conv, body.content)
    return SendMessageOut(
        user_message=MessageOut.model_validate(user_message),
        assistant_message=MessageOut.model_validate(assistant_message),
    )


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conversation_id: UUID,
    user: CurrentUser,
    db: DbSession,
    limit: int = 50,
    before_id: UUID | None = None,
):
    """倒序取一页（limit 上限 200），返回时转为时间正序，便于聊天 UI 直接追加。"""
    limit = min(limit, 200)
    conv = await _get_owned_conversation(conversation_id, user.id, db)
    stmt = (
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(limit)
    )
    if before_id is not None:
        before = await db.get(Message, before_id)
        if before is not None:
            stmt = stmt.where(Message.created_at < before.created_at)
    messages = (await db.execute(stmt)).scalars().all()
    return list(reversed(messages))
