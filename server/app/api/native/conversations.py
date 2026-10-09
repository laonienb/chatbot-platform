"""会话：创建（注入开场白）/ 列表 / 改名置顶 / 删除 / 发消息（非流式 + SSE 流式）/ 翻历史。"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, select

from app.api.deps import CurrentUser, DbSession
from app.billing.ratelimit import rate_limit_native
from app.core.sse import SSE_DONE, SSE_HEADERS, format_sse, stream_error_frame
from app.llm.gateway import StreamDone
from app.llm.remote import ModelServiceError
from app.models import Conversation, Message, Persona
from app.schemas.conversation import (
    ConversationCreate,
    ConversationOut,
    ConversationUpdate,
    MessageOut,
    MessageSendIn,
    SendMessageOut,
)
from app.services.chat import (
    ensure_native_admitted,
    regenerate_stream,
    send_message,
    send_message_stream,
)
from app.services.persona import can_view_persona

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


async def _sse_from(pieces):
    """服务层流式片段 → 对外 SSE：内容 delta →（错误终止时）错误帧 → `[DONE]`。

    §6.7 / 工作单 P1-9 **对外那一半**。此前原生面**没有任何**错误出口：模型服务报错时异常
    直接从生成器抛出，而 Starlette 在 `stream_response` 里**先发 200 响应头、再迭代 body**
    （`starlette/responses.py:251`）⇒ 状态码早已不可改，客户端只看到"没有 `[DONE]` 的半截流"，
    既拿不到 `error.code` 也拿不到退避时长 —— 上游限流/超时对用户完全不可见（§18.2-F2 的
    最后一跳）。现按协议形态补齐：`data: {"error": {…code、retry_after_s…}}` 其后仍发
    `[DONE]`（G3-1）。

    ⚠️ 判据是 `error_message` 有值、**不是** `terminate_reason` 有值：§8.1.1 的绝对上限也写
    terminate_reason，但它对外按「回复中断」表现、**不给**错误提示（§8.1.1 末句）。
    """
    done: StreamDone | None = None
    try:
        async for piece in pieces:
            if isinstance(piece, StreamDone):
                done = piece
                continue  # 结束标记不外发，落库已在服务层完成
            yield format_sse({"choices": [{"index": 0, "delta": {"content": piece}}]})
    except ModelServiceError as e:  # 首字节前就已经失败（HTTP 错误状态传到平台后转发）
        yield format_sse(
            stream_error_frame(
                e.code,
                e.message,
                err_type=e.err_type,
                retryable=e.retryable,
                retry_after_s=e.retry_after,
            )
        )
        yield SSE_DONE
        return
    except Exception as e:  # 其余上游异常：给得出 shape 就给，code 留空由客户端按自有策略处理
        yield format_sse(stream_error_frame(None, f"LLM upstream error: {e}"))
        yield SSE_DONE
        return
    if done is not None and done.error_message is not None:
        # 模型服务的**流内**错误帧已由 RemoteBackend 归因到 done，对外同样不得吞掉。
        yield format_sse(
            stream_error_frame(
                done.terminate_reason,
                done.error_message,
                retryable=done.error_retryable,
                retry_after_s=done.retry_after_s,
            )
        )
    yield SSE_DONE


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
    data = body.model_dump(exclude_unset=True)
    if data.get("model") == "":
        data["model"] = None  # 空串=恢复默认（人设/平台默认）
    for field, value in data.items():
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


@router.post("/{conversation_id}/messages")
async def send(
    conversation_id: UUID,
    body: MessageSendIn,
    user: CurrentUser,
    db: DbSession,
    _rl: None = Depends(rate_limit_native),  # noqa: ARG001 — 限流依赖（无返回值）
):
    conv = await _get_owned_conversation(conversation_id, user.id, db)
    # 准入（配额 429 / 余额 402）必须在开流之前 —— 200 之后无法再回错误状态码
    await ensure_native_admitted(db, conv, body.content)
    if body.stream:
        return StreamingResponse(
            _sse_from(send_message_stream(db, conv, body.content, source="native")),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    try:
        user_message, assistant_message = await send_message(db, conv, body.content, source="native")
    except HTTPException:
        raise
    except ModelServiceError:
        raise  # 交给全局处理器按协议 §7 映射状态码/Retry-After
    except Exception as e:  # 上游 LLM 异常 → 与兼容层对齐的 502（预扣行已在服务层结算为 failed）
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"LLM upstream error: {e}")
    return SendMessageOut(
        user_message=MessageOut.model_validate(user_message),
        assistant_message=MessageOut.model_validate(assistant_message),
    )


@router.post("/{conversation_id}/regenerate")
async def regenerate(
    conversation_id: UUID,
    user: CurrentUser,
    db: DbSession,
    _rl: None = Depends(rate_limit_native),  # noqa: ARG001
):
    """重新生成最后一条助手回复（替换而非追加）。仅 SSE 流式返回。"""
    conv = await _get_owned_conversation(conversation_id, user.id, db)
    await ensure_native_admitted(db, conv)  # content=None：历史末尾已是该用户消息

    return StreamingResponse(
        _sse_from(regenerate_stream(db, conv, source="native")),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
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
