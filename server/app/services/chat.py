"""对话服务：有状态（第一方客户端）与无状态（OpenAI 兼容层）两条链路的共用核心。"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.llm.gateway import LLMResult, StreamDone, chat_stream, get_llm_backend
from app.models import ApiKey, Conversation, LlmModel, Message, Persona, UsageLog, User

# 上下文窗口：最多回看的历史消息条数（超长截断策略 M1 细化为滚动摘要）
CONTEXT_WINDOW = 50


def persona_system_prompt(persona: Persona) -> str:
    """平台注入的人设 system prompt。前缀 [persona:<slug>] 便于调试与 mock 链路识别。"""
    return f"[persona:{persona.slug}] {persona.system_prompt}"


def build_llm_messages(persona: Persona | None, history: list[Message]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if persona and persona.system_prompt:
        messages.append({"role": "system", "content": persona_system_prompt(persona)})
    messages.extend({"role": m.role, "content": m.content} for m in history)
    return messages


async def resolve_model(db: AsyncSession, model_field: str) -> tuple[Persona | None, str]:
    """解析 model 字段：persona:<slug> → (persona, 底层模型)；其他值 → (None, 原样)。

    persona 不存在或非 active 抛 LookupError，由路由层转 404。
    """
    if not model_field.startswith("persona:"):
        return None, model_field
    slug = model_field.split(":", 1)[1]
    persona = (await db.execute(select(Persona).where(Persona.slug == slug))).scalar_one_or_none()
    if persona is None or persona.status != "active":
        raise LookupError(slug)
    return persona, persona.model or get_settings().llm_default_model


async def lookup_model_credentials(
    db: AsyncSession, model: str
) -> tuple[str | None, str | None]:
    """从模型注册表查该模型串的专属端点凭证（自部署/第三方 API）。"""
    row = (
        await db.execute(select(LlmModel).where(LlmModel.model == model, LlmModel.enabled == True))  # noqa: E712
    ).scalars().first()
    return (row.api_base, row.api_key) if row else (None, None)


async def default_model_string(db: AsyncSession) -> str:
    """注册表里的默认模型；注册表为空则用环境配置。"""
    row = (
        await db.execute(
            select(LlmModel).where(LlmModel.enabled == True, LlmModel.is_default == True)  # noqa: E712
        )
    ).scalars().first()
    if row is None:
        row = (await db.execute(select(LlmModel).where(LlmModel.enabled == True).limit(1))).scalars().first()  # noqa: E712
    return row.model if row else get_settings().llm_default_model


async def resolve_chat_model(
    db: AsyncSession, conversation: Conversation, persona: Persona | None
) -> tuple[str, str | None, str | None]:
    """会话级覆盖 > 人设偏好 > 注册表默认；并带出该模型的专属端点凭证。"""
    model = conversation.model or (persona.model if persona else None) or await default_model_string(db)
    api_base, api_key = await lookup_model_credentials(db, model)
    return model, api_base, api_key


async def _history(db: AsyncSession, conversation_id: UUID) -> list[Message]:
    rows = (
        (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(CONTEXT_WINDOW)
            )
        )
        .scalars()
        .all()
    )
    return list(reversed(rows))


async def _save_reply(
    db: AsyncSession,
    conversation: Conversation,
    persona: Persona | None,
    done: StreamDone,
    content: str,
) -> Message:
    assistant_message = Message(
        conversation_id=conversation.id,
        role="assistant",
        content=content,
        model=done.model,
        prompt_tokens=done.prompt_tokens,
        completion_tokens=done.completion_tokens,
    )
    db.add(assistant_message)
    conversation.last_message_at = datetime.now(UTC)
    db.add(
        UsageLog(
            user_id=conversation.user_id,
            conversation_id=conversation.id,
            model=done.model,
            persona_id=persona.id if persona else None,
            prompt_tokens=done.prompt_tokens,
            completion_tokens=done.completion_tokens,
        )
    )
    await db.commit()
    return assistant_message


async def send_message(db: AsyncSession, conversation: Conversation, content: str) -> tuple[Message, Message]:
    """有状态非流式链路：落用户消息 → 组装上下文 → 调 LLM → 落助手消息与用量。"""
    persona = await db.get(Persona, conversation.persona_id)
    user_message = Message(conversation_id=conversation.id, role="user", content=content)
    db.add(user_message)
    await db.flush()

    history = await _history(db, conversation.id)
    llm_messages = build_llm_messages(persona, history)
    model, api_base, api_key = await resolve_chat_model(db, conversation, persona)
    result = await get_llm_backend().chat(
        llm_messages,
        model,
        temperature=persona.temperature if persona else None,
        top_p=persona.top_p if persona else None,
        max_tokens=persona.max_tokens if persona else None,
        api_base=api_base,
        api_key=api_key,
    )
    done = StreamDone(model=result.model, prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens)
    assistant_message = await _save_reply(db, conversation, persona, done, result.content)
    await db.refresh(user_message)
    await db.refresh(assistant_message)
    return user_message, assistant_message


async def send_message_stream(
    db: AsyncSession, conversation: Conversation, content: str
) -> AsyncIterator[str | StreamDone]:
    """有状态流式链路：落用户消息 → 流式 yield 内容增量 → 结束时落库助手消息与用量。

    客户端中断（停止生成）时生成器被取消，本次回复与用户消息均不落库。
    """
    persona = await db.get(Persona, conversation.persona_id)
    db.add(Message(conversation_id=conversation.id, role="user", content=content))
    await db.flush()
    async for piece in _stream_reply(db, conversation, persona):
        yield piece


async def regenerate_stream(
    db: AsyncSession, conversation: Conversation
) -> AsyncIterator[str | StreamDone]:
    """重新生成：删除末尾的助手消息，基于既有历史（以用户消息结尾）再次生成。

    会话里还没有用户消息时（只有开场白）不删除任何内容，仅生成一条新回复。
    """
    persona = await db.get(Persona, conversation.persona_id)
    has_user_message = (
        await db.execute(
            select(Message.id)
            .where(Message.conversation_id == conversation.id, Message.role == "user")
            .limit(1)
        )
    ).scalar_one_or_none()
    if has_user_message is not None:
        # 逐条删除末尾连续的 assistant 消息
        while True:
            last = (
                await db.execute(
                    select(Message)
                    .where(Message.conversation_id == conversation.id)
                    .order_by(Message.created_at.desc(), Message.id.desc())
                    .limit(1)
                )
            ).scalars().first()
            if last is None or last.role != "assistant":
                break
            await db.execute(delete(Message).where(Message.id == last.id))
            await db.flush()
    async for piece in _stream_reply(db, conversation, persona):
        yield piece


async def _stream_reply(
    db: AsyncSession, conversation: Conversation, persona: Persona | None
) -> AsyncIterator[str | StreamDone]:
    """基于当前历史（应已含最新用户消息）流式生成并落库助手回复。"""
    history = await _history(db, conversation.id)
    llm_messages = build_llm_messages(persona, history)
    model, api_base, api_key = await resolve_chat_model(db, conversation, persona)
    parts: list[str] = []
    async for piece in chat_stream(
        llm_messages,
        model,
        temperature=persona.temperature if persona else None,
        top_p=persona.top_p if persona else None,
        max_tokens=persona.max_tokens if persona else None,
        api_base=api_base,
        api_key=api_key,
    ):
        if isinstance(piece, StreamDone):
            await _save_reply(db, conversation, persona, piece, "".join(parts))
            yield piece
        else:
            parts.append(piece)
            yield piece


async def run_completion(
    db: AsyncSession,
    *,
    user: User,
    api_key: ApiKey,
    model_field: str,
    messages: list[dict[str, str]],
    temperature: float | None,
    top_p: float | None,
    max_tokens: int | None,
) -> LLMResult:
    """无状态链路（OpenAI 兼容层）：解析 persona:<slug> → 注入人设 → 调 LLM → 记用量。

    persona 不存在时抛 LookupError，由路由层转成 OpenAI 格式的 model_not_found。
    """
    persona, model = await resolve_model(db, model_field)
    if persona:
        llm_messages = [{"role": "system", "content": persona_system_prompt(persona)}] + messages
        temperature = persona.temperature if persona.temperature is not None else temperature
        top_p = persona.top_p if persona.top_p is not None else top_p
        max_tokens = persona.max_tokens if persona.max_tokens is not None else max_tokens
        api_base, api_key_cfg = await lookup_model_credentials(db, model)
    else:
        llm_messages = messages
        api_base = api_key_cfg = None

    result = await get_llm_backend().chat(
        llm_messages, model, temperature=temperature, top_p=top_p, max_tokens=max_tokens,
        api_base=api_base, api_key=api_key_cfg,
    )
    await _record_usage(
        db,
        user_id=user.id,
        model=result.model,
        result=result,
        persona_id=persona.id if persona else None,
        api_key_id=api_key.id,
    )
    await db.commit()
    return result


async def run_completion_stream(
    db: AsyncSession,
    *,
    user: User,
    api_key: ApiKey,
    model_field: str,
    messages: list[dict[str, str]],
    temperature: float | None,
    top_p: float | None,
    max_tokens: int | None,
) -> AsyncIterator[str | StreamDone]:
    """无状态流式链路：参数与 run_completion 相同，流式 yield 内容增量与 StreamDone。"""
    persona, model = await resolve_model(db, model_field)
    if persona:
        llm_messages = [{"role": "system", "content": persona_system_prompt(persona)}] + messages
        temperature = persona.temperature if persona.temperature is not None else temperature
        top_p = persona.top_p if persona.top_p is not None else top_p
        max_tokens = persona.max_tokens if persona.max_tokens is not None else max_tokens
        api_base, api_key_cfg = await lookup_model_credentials(db, model)
    else:
        llm_messages = messages
        api_base = api_key_cfg = None

    async for piece in chat_stream(
        llm_messages, model, temperature=temperature, top_p=top_p, max_tokens=max_tokens,
        api_base=api_base, api_key=api_key_cfg,
    ):
        if isinstance(piece, StreamDone):
            db.add(
                UsageLog(
                    user_id=user.id,
                    api_key_id=api_key.id,
                    model=piece.model,
                    persona_id=persona.id if persona else None,
                    prompt_tokens=piece.prompt_tokens,
                    completion_tokens=piece.completion_tokens,
                )
            )
            await db.commit()
        yield piece


async def _record_usage(
    db: AsyncSession,
    *,
    user_id: UUID,
    model: str,
    result: LLMResult,
    persona_id: UUID | None = None,
    conversation_id: UUID | None = None,
    api_key_id: UUID | None = None,
) -> None:
    db.add(
        UsageLog(
            user_id=user_id,
            api_key_id=api_key_id,
            conversation_id=conversation_id,
            model=model,
            persona_id=persona_id,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
    )
