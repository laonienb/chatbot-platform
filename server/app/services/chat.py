"""对话服务：有状态（第一方客户端）与无状态（OpenAI 兼容层）两条链路的共用核心。"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.llm.gateway import LLMResult, StreamDone, chat_stream, get_llm_backend
from app.models import ApiKey, Conversation, Message, Persona, UsageLog, User

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


async def send_message(db: AsyncSession, conversation: Conversation, content: str) -> tuple[Message, Message]:
    """有状态链路：落用户消息 → 组装上下文 → 调 LLM → 落助手消息与用量。返回 (用户消息, 助手消息)。"""
    persona = await db.get(Persona, conversation.persona_id)

    user_message = Message(conversation_id=conversation.id, role="user", content=content)
    db.add(user_message)
    await db.flush()  # 先拿到 user_message.id/created_at，便于上下文排序

    history = (
        (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(CONTEXT_WINDOW)
            )
        )
        .scalars()
        .all()
    )
    history = list(reversed(history))  # 时间正序喂给模型

    llm_messages = build_llm_messages(persona, history)
    model = persona.model if persona and persona.model else get_settings().llm_default_model
    result = await get_llm_backend().chat(
        llm_messages,
        model,
        temperature=persona.temperature if persona else None,
        top_p=persona.top_p if persona else None,
        max_tokens=persona.max_tokens if persona else None,
    )

    assistant_message = Message(
        conversation_id=conversation.id,
        role="assistant",
        content=result.content,
        model=result.model,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
    )
    db.add(assistant_message)
    conversation.last_message_at = datetime.now(UTC)
    await _record_usage(
        db,
        user_id=conversation.user_id,
        model=result.model,
        result=result,
        persona_id=persona.id if persona else None,
        conversation_id=conversation.id,
    )
    await db.commit()
    await db.refresh(user_message)
    await db.refresh(assistant_message)
    return user_message, assistant_message


async def send_message_stream(
    db: AsyncSession, conversation: Conversation, content: str
) -> AsyncIterator[str | StreamDone]:
    """有状态流式链路：落用户消息 → 流式 yield 内容增量 → 结束时落库助手消息与用量。

    生成器正常迭代完毕才提交；客户端中途断开导致 GeneratorExit 时不提交本次回复。
    """
    persona = await db.get(Persona, conversation.persona_id)
    user_message = Message(conversation_id=conversation.id, role="user", content=content)
    db.add(user_message)
    await db.flush()

    history = (
        (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(CONTEXT_WINDOW)
            )
        )
        .scalars()
        .all()
    )
    history = list(reversed(history))

    llm_messages = build_llm_messages(persona, history)
    model = persona.model if persona and persona.model else get_settings().llm_default_model
    parts: list[str] = []
    async for piece in chat_stream(
        llm_messages,
        model,
        temperature=persona.temperature if persona else None,
        top_p=persona.top_p if persona else None,
        max_tokens=persona.max_tokens if persona else None,
    ):
        if isinstance(piece, StreamDone):
            assistant_message = Message(
                conversation_id=conversation.id,
                role="assistant",
                content="".join(parts),
                model=piece.model,
                prompt_tokens=piece.prompt_tokens,
                completion_tokens=piece.completion_tokens,
            )
            db.add(assistant_message)
            conversation.last_message_at = datetime.now(UTC)
            db.add(
                UsageLog(
                    user_id=conversation.user_id,
                    conversation_id=conversation.id,
                    model=piece.model,
                    persona_id=persona.id if persona else None,
                    prompt_tokens=piece.prompt_tokens,
                    completion_tokens=piece.completion_tokens,
                )
            )
            await db.commit()
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
    else:
        llm_messages = messages

    result = await get_llm_backend().chat(llm_messages, model, temperature=temperature, top_p=top_p, max_tokens=max_tokens)
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
    else:
        llm_messages = messages

    async for piece in chat_stream(llm_messages, model, temperature=temperature, top_p=top_p, max_tokens=max_tokens):
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
