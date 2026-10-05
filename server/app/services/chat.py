"""对话服务：有状态（第一方客户端）与无状态（OpenAI 兼容层）两条链路的共用核心。

计费（billing step 2b）：双阶段结算 —— 调 LLM 前 reserve_usage 落 pending 行，
任何路径（正常、报错、取消）收敛到恰好一次 settle_usage。
"""

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import anyio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.attribution import attribute_prompt_tokens, estimate_completion_tokens, prompt_total
from app.billing.charge import InsufficientBalance, balance as wallet_balance
from app.billing.ledger import reserve_usage, settle_usage
from app.billing.quota import QuotaExceeded, admit_request
from app.billing.rating import compute_billed, load_rating_rule, rule_snapshot
from app.config import get_settings
from app.llm.gateway import LLMResult, StreamDone, chat_stream, get_llm_backend
from app.models import ApiKey, Conversation, LlmModel, Message, Persona, UsageLog, User
from app.services.memory import build_memory_block, get_recent_memories, schedule_extraction

# 上下文窗口：最多回看的历史消息条数（超长截断策略 M1 细化为滚动摘要）
CONTEXT_WINDOW = 50

# 准入预估的回复长度：预检只估不记账（实测在结算侧），取保守的默认回复长度；
# 估算偏小导致的余额缺口由结算侧部分支付 + needs_review 兜底。
ESTIMATED_COMPLETION_TOKENS = 512


def persona_system_prompt(persona: Persona) -> str:
    """平台注入的人设 system prompt。前缀 [persona:<slug>] 便于调试与 mock 链路识别。"""
    return f"[persona:{persona.slug}] {persona.system_prompt}"


def build_llm_messages(
    persona: Persona | None, history: list[Message], memory_block: str | None = None
) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    if persona and persona.system_prompt:
        messages.append({"role": "system", "content": persona_system_prompt(persona)})
    if memory_block:
        messages.append({"role": "system", "content": memory_block})
    messages.extend({"role": m.role, "content": m.content} for m in history)
    return messages


async def ensure_admitted(
    db: AsyncSession, *, user_id: UUID, llm_messages: list[dict[str, str]], model: str
) -> None:
    """准入检查（周期 token 配额 + 积分余额），不足抛 QuotaExceeded / InsufficientBalance。

    必须由路由层在响应开始前调用 —— 流式 200 之后无法再回 429/402。
    估算 = prompt + 默认回复长度，只用于准入；记账永远用结算侧实测值。
    """
    settings = get_settings()
    est_prompt = prompt_total(llm_messages, model=model)
    est_tokens = est_prompt + ESTIMATED_COMPLETION_TOKENS

    ok, used, allowance = await admit_request(
        db, user_id, est_tokens, allowance_tokens=settings.quota_monthly_tokens
    )
    if not ok:
        raise QuotaExceeded(f"本月 token 额度已用尽（已用 {used} / 额度 {allowance}）")

    # 余额预检：按平台费率预估本次成本（credit 计价才走钱包）
    rule, mr = await load_rating_rule(db, user_id=user_id, model=model)
    billed = compute_billed(
        rule_snapshot(rule, mr),
        prompt_tokens=est_prompt,
        completion_tokens=ESTIMATED_COMPLETION_TOKENS,
    )
    if billed is None:
        return  # 无费率规则（播种失败等）：跳过余额检查，结算侧 needs_review 兜底
    amount, unit = billed
    if unit != "credit" or amount <= 0:
        return  # 非积分计价（usd/cny/周期额度）不走钱包
    bal = await wallet_balance(db, user_id)
    if bal < amount:
        raise InsufficientBalance(f"积分余额不足（余额 {bal}，本次预估需 {amount}）")


async def ensure_native_admitted(
    db: AsyncSession, conversation: Conversation, content: str | None = None
) -> None:
    """原生面准入：按当前会话上下文（人设+记忆+历史）组装后检查。

    content 为本次新消息；None 表示重新生成（历史末尾已是该用户消息）。
    """
    persona = await db.get(Persona, conversation.persona_id)
    history = await _history(db, conversation.id)
    memory_block = await _memory_block(db, conversation, persona)
    llm_messages = build_llm_messages(persona, history, memory_block)
    if content is not None:
        llm_messages = llm_messages + [{"role": "user", "content": content}]
    model, _, _ = await resolve_chat_model(db, conversation, persona)
    await ensure_admitted(
        db, user_id=conversation.user_id, llm_messages=llm_messages, model=model
    )


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
    *,
    reservation_id: UUID | None = None,
    attribution: dict | None = None,
    source: str = "native",
    source_uid: str | None = None,
    error_code: str | None = None,
) -> Message:
    """落助手消息 + 结算账本。reservation_id 存在 → 结算 pending 行（双阶段阶段B）。"""
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
    if reservation_id is not None:
        await settle_usage(
            db,
            reservation_id,
            done=done,
            attribution=attribution,
            error_code=error_code,
        )
        # settle_usage 内部已 commit；以下字段不在 settle 内的收尾
        await db.flush()
    else:
        # 兼容旧路径（无 reservation）：直接建已结算行。cost 字段未计价，
        # 钱包扣费与计价走 settle_usage（当前所有调用方都带 reservation）。
        log = UsageLog(
            user_id=conversation.user_id,
            conversation_id=conversation.id,
            source=source,
            source_uid=source_uid,
            channel=conversation.channel,
            model=done.model,
            model_upstream=done.model,
            persona_id=persona.id if persona else None,
            prompt_tokens=done.prompt_tokens,
            completion_tokens=done.completion_tokens,
            cache_read_tokens=done.cache_read_tokens,
            reasoning_tokens=done.reasoning_tokens,
            metering_source=done.metering_source,
            attribution=attribution,
            status="settled",
            settled_at=datetime.now(UTC),
            needs_review=done.needs_review,
        )
        db.add(log)
        await db.commit()
    return assistant_message


async def send_message(db: AsyncSession, conversation: Conversation, content: str, *, source: str = "native") -> tuple[Message, Message]:
    """有状态非流式链路：落用户消息 → 预扣（阶段A）→ 调 LLM → 结算（阶段B）+ 落助手消息。

    LLM 报错时：预扣行以 error_code 结算（failed），用户消息已落库（阶段A已commit），
    异常向上抛给路由层转 502。
    """
    persona = await db.get(Persona, conversation.persona_id)
    user_message = Message(conversation_id=conversation.id, role="user", content=content)
    db.add(user_message)
    await db.flush()

    history = await _history(db, conversation.id)
    memory_block = await _memory_block(db, conversation, persona)
    llm_messages = build_llm_messages(persona, history, memory_block)
    model, api_base, api_key = await resolve_chat_model(db, conversation, persona)

    # 阶段A：pending 行落库（幂等键 = user_message.id —— 重复提交不双扣）
    reservation_id = await reserve_usage(
        db,
        user_id=conversation.user_id,
        model_requested=model,
        model_upstream=model,
        source=source,
        conversation_id=conversation.id,
        persona_id=persona.id if persona else None,
        channel=conversation.channel,
        idempotency_key=f"msg:{user_message.id}",
        estimated_prompt_tokens=prompt_total(llm_messages, model=model),
    )

    result = None
    try:
        result = await get_llm_backend().chat(
            llm_messages,
            model,
            temperature=persona.temperature if persona else None,
            top_p=persona.top_p if persona else None,
            max_tokens=persona.max_tokens if persona else None,
            api_base=api_base,
            api_key=api_key,
        )
    except Exception as exc:
        # 上游异常：预扣行以 error_code 结算（上游可能已耗 token，不可免单），
        # 异常继续上抛给路由层转 502。
        done_err = StreamDone(model=model, prompt_tokens=prompt_total(llm_messages, model=model), completion_tokens=0)
        await settle_usage(
            db, reservation_id, done=done_err,
            attribution=attribute_prompt_tokens(llm_messages, model=model, user_input=content),
            error_code=f"upstream_error:{type(exc).__name__}",
        )
        raise
    attribution = attribute_prompt_tokens(llm_messages, model=model, user_input=content)
    done = StreamDone(
        model=result.model,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cache_read_tokens=result.cache_read_tokens,
        reasoning_tokens=result.reasoning_tokens,
        finish_reason=result.finish_reason,
        metering_source=result.metering_source,
        needs_review=result.needs_review,
    )
    assistant_message = await _save_reply(
        db, conversation, persona, done, result.content,
        reservation_id=reservation_id, attribution=attribution, source=source,
    )
    schedule_extraction(
        db, conversation.id, conversation.user_id, conversation.persona_id, content, result.content
    )
    await db.refresh(user_message)
    await db.refresh(assistant_message)
    return user_message, assistant_message


async def send_message_stream(
    db: AsyncSession, conversation: Conversation, content: str, *, source: str = "native"
) -> AsyncIterator[str | StreamDone]:
    """有状态流式链路：落用户消息 + 预扣（阶段A commit）→ 流式 yield → 结算（阶段B）。

    阶段A 的 commit 让用户消息与预扣行独立于流的生命周期：客户端中断时
    用户消息与预扣行已落库，回复由 finally 以 error_code=client_disconnected
    结算（已生成部分照计费，上游 token 已消耗）。
    """
    persona = await db.get(Persona, conversation.persona_id)
    user_message = Message(conversation_id=conversation.id, role="user", content=content)
    db.add(user_message)
    await db.flush()

    # 组装上下文 + 模型解析（预扣需要 model_upstream 与估算 prompt）
    history = await _history(db, conversation.id)
    memory_block = await _memory_block(db, conversation, persona)
    llm_messages = build_llm_messages(persona, history, memory_block)
    model, _, _ = await resolve_chat_model(db, conversation, persona)

    reservation_id = await reserve_usage(
        db,
        user_id=conversation.user_id,
        model_requested=model,
        model_upstream=model,
        source=source,
        conversation_id=conversation.id,
        persona_id=persona.id if persona else None,
        channel=conversation.channel,
        idempotency_key=f"msg:{user_message.id}",
        estimated_prompt_tokens=prompt_total(llm_messages, model=model),
    )

    async for piece in _stream_reply(
        db, conversation, persona, reservation_id=reservation_id, source=source,
        user_input=content, llm_messages=llm_messages,
    ):
        yield piece


async def regenerate_stream(
    db: AsyncSession, conversation: Conversation, *, source: str = "native"
) -> AsyncIterator[str | StreamDone]:
    """重新生成：删除末尾的助手消息，基于既有历史（以用户消息结尾）再次生成。

    会话里还没有用户消息时（只有开场白）不删除任何内容，仅生成一条新回复。
    幂等键每次调用唯一（regen:{uuid}）：重复点击 = 重复生成 = 重复计费（上游
    跑了两次就必须收两次）；单次调用内 reserve 恰好一行，不会自我双扣。
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

    history = await _history(db, conversation.id)
    memory_block = await _memory_block(db, conversation, persona)
    llm_messages = build_llm_messages(persona, history, memory_block)
    model, _, _ = await resolve_chat_model(db, conversation, persona)

    reservation_id = await reserve_usage(
        db,
        user_id=conversation.user_id,
        model_requested=model,
        model_upstream=model,
        source=source,
        conversation_id=conversation.id,
        persona_id=persona.id if persona else None,
        channel=conversation.channel,
        idempotency_key=f"regen:{uuid4().hex}",  # 每次调用唯一：重复生成必须重复计费
        estimated_prompt_tokens=prompt_total(llm_messages, model=model),
    )

    async for piece in _stream_reply(
        db, conversation, persona, reservation_id=reservation_id, source=source,
        llm_messages=llm_messages,
    ):
        yield piece


async def _stream_reply(
    db: AsyncSession,
    conversation: Conversation,
    persona: Persona | None,
    *,
    reservation_id: UUID | None = None,
    source: str = "native",
    user_input: str | None = None,
    llm_messages: list[dict[str, str]] | None = None,
) -> AsyncIterator[str | StreamDone]:
    """基于当前历史（应已含最新用户消息）流式生成并结算（阶段B）。"""
    # 历史只加载一次：组装上下文 + 记忆提取时取末轮用户输入都用它
    history = await _history(db, conversation.id)
    if llm_messages is None:
        memory_block = await _memory_block(db, conversation, persona)
        llm_messages = build_llm_messages(persona, history, memory_block)
    model, api_base, api_key = await resolve_chat_model(db, conversation, persona)
    attribution = attribute_prompt_tokens(llm_messages, model=model, user_input=user_input)
    last_user_text = user_input or next(
        (m.content for m in reversed(history) if m.role == "user"), ""
    )

    parts: list[str] = []
    settled = False

    async def _settle(error_code: str | None = None) -> None:
        """兜底结算（异常/取消/无 StreamDone 的终止）。已结算则幂等跳过。"""
        nonlocal settled
        if settled or reservation_id is None:
            settled = True
            return
        settled = True
        done = StreamDone(
            model=model,
            prompt_tokens=prompt_total(llm_messages, model=model),
            completion_tokens=estimate_completion_tokens("".join(parts)),
            metering_source="estimated",
            needs_review=True,
        )
        await settle_usage(db, reservation_id, done=done, attribution=attribution, error_code=error_code)

    try:
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
                # 正常完成：落助手消息 + 用实测 done 结算（唯一产生账单的终态路径）
                if reservation_id is not None and not settled:
                    settled = True
                assistant_message = await _save_reply(
                    db, conversation, persona, piece, "".join(parts),
                    reservation_id=reservation_id, attribution=attribution, source=source,
                )
                # 记忆提取（不入账，fire-and-forget 后台任务）
                schedule_extraction(
                    db, conversation.id, conversation.user_id, conversation.persona_id,
                    last_user_text, "".join(parts),
                )
                yield piece
            else:
                parts.append(piece)
                yield piece
    except asyncio.CancelledError:
        # 客户端断开：已生成部分照计费（上游 token 已消耗）。
        # shield：anyio 取消作用域下每个 await 都会再抛 CancelledError，
        # 不屏蔽则 settle 永远落不了库，只能等 15min 对账收编。
        with anyio.CancelScope(shield=True):
            await _settle("client_disconnected")
        raise
    except Exception as exc:
        await _settle(f"upstream_error:{type(exc).__name__}")
        raise
    finally:
        # 兜底：流在没有 StreamDone 的情况下终止（理论上不应到达）
        if not settled and reservation_id is not None:
            await _settle("stream_terminated")


async def _memory_block(db: AsyncSession, conversation: Conversation, persona: Persona | None) -> str | None:
    """取该会话（用户+人设）的长期记忆注入块；人设关闭记忆则返回 None。"""
    if persona is not None and not persona.memory_enabled:
        return None
    memories = await get_recent_memories(db, conversation.user_id, conversation.persona_id)
    return build_memory_block(memories)


def _compat_idem_key(api_key: ApiKey, model_field: str, messages: list[dict[str, str]]) -> str:
    """兼容层幂等键：api_key + model + 规范化消息体哈希 + 10 分钟时间窗。

    - user 字段（归因）不进指纹 —— 同一请求带不带 user 都不该双扣。
    - 时间窗：窗内同 body 重发（AstrBot 网络抖动重发）不双扣；窗外的合法
      重复请求正常计费 —— 没有窗口，同 body 的第二次请求会永久免单
      （上游照样跑、照样花钱），收入损失无上界。
    - 调用方显式传 Idempotency-Key header 时以 header 为准、不加窗口
      （调用方自己承诺语义，见 api/openai_compat）。
    """
    import hashlib
    import json as _json

    payload = _json.dumps(
        {"model": model_field, "messages": [{"role": m["role"], "content": m["content"]} for m in messages]},
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(f"{api_key.id}:{payload}".encode()).hexdigest()[:40]
    window = int(time.time()) // 600  # 10 分钟桶
    return f"compat:{api_key.id.hex[:16]}:{digest}:{window}"


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
    source_uid: str | None = None,
    idempotency_key: str | None = None,
) -> LLMResult:
    """无状态链路（OpenAI 兼容层）：解析 persona:<slug> → 注入人设 → 预扣 → 调 LLM → 结算。

    persona 不存在时抛 LookupError（路由层转 404，此时未预扣、不产生账）。
    LLM 报错时预扣行以 error_code 结算（上游可能已消耗 token，不可免单）。
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

    # 阶段A：pending 落库。幂等键：调用方 Idempotency-Key header 优先，
    # 否则用 api_key+请求指纹（去掉 user 字段 —— 归因字段不改变计费语义）。
    if not idempotency_key:
        idempotency_key = _compat_idem_key(api_key, model_field, messages)
    reservation_id = await reserve_usage(
        db,
        user_id=user.id,
        model_requested=model_field,
        model_upstream=model,
        source="compat",
        api_key_id=api_key.id,
        persona_id=persona.id if persona else None,
        source_uid=source_uid,
        idempotency_key=idempotency_key,
        estimated_prompt_tokens=prompt_total(llm_messages, model=model),
    )

    try:
        result = await get_llm_backend().chat(
            llm_messages, model, temperature=temperature, top_p=top_p, max_tokens=max_tokens,
            api_base=api_base, api_key=api_key_cfg,
        )
    except Exception as exc:
        # 上游异常：按已消耗部分结算（阶段B），异常继续上抛给路由层转 502
        done_err = StreamDone(model=model, prompt_tokens=prompt_total(llm_messages, model=model), completion_tokens=0)
        await settle_usage(
            db, reservation_id, done=done_err,
            attribution=attribute_prompt_tokens(llm_messages, model=model),
            error_code=f"upstream_error:{type(exc).__name__}",
        )
        raise

    await settle_usage(
        db,
        reservation_id,
        done=StreamDone(
            model=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cache_read_tokens=result.cache_read_tokens,
            reasoning_tokens=result.reasoning_tokens,
            finish_reason=result.finish_reason,
            metering_source=result.metering_source,
            needs_review=result.needs_review,
        ),
        attribution=attribute_prompt_tokens(llm_messages, model=model),
    )
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
    source_uid: str | None = None,
    idempotency_key: str | None = None,
) -> AsyncIterator[str | StreamDone]:
    """无状态流式链路：参数与 run_completion 相同，流式 yield 内容增量与 StreamDone。

    预扣在生成器首次迭代时执行（路由层已在此之前完成 persona 解析与
    白名单校验，403/404 不预扣、不产生账）。
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

    if not idempotency_key:
        idempotency_key = _compat_idem_key(api_key, model_field, messages)
    reservation_id = await reserve_usage(
        db,
        user_id=user.id,
        model_requested=model_field,
        model_upstream=model,
        source="compat",
        api_key_id=api_key.id,
        persona_id=persona.id if persona else None,
        source_uid=source_uid,
        idempotency_key=idempotency_key,
        estimated_prompt_tokens=prompt_total(llm_messages, model=model),
    )
    attribution = attribute_prompt_tokens(llm_messages, model=model)
    parts: list[str] = []
    settled = False

    async def _settle(error_code: str) -> None:
        nonlocal settled
        if settled:
            return
        settled = True
        await settle_usage(
            db, reservation_id,
            done=StreamDone(
                model=model,
                prompt_tokens=prompt_total(llm_messages, model=model),
                completion_tokens=estimate_completion_tokens("".join(parts)),
                metering_source="estimated",
                needs_review=True,
            ),
            attribution=attribution,
            error_code=error_code,
        )

    try:
        async for piece in chat_stream(
            llm_messages, model, temperature=temperature, top_p=top_p, max_tokens=max_tokens,
            api_base=api_base, api_key=api_key_cfg,
        ):
            if isinstance(piece, StreamDone):
                if not settled:
                    settled = True
                    await settle_usage(db, reservation_id, done=piece, attribution=attribution)
            else:
                parts.append(piece)
            yield piece
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True):  # 同 _stream_reply：取消语义下保证结算落库
            await _settle("client_disconnected")
        raise
    except Exception as exc:
        await _settle(f"upstream_error:{type(exc).__name__}")
        raise
    finally:
        if not settled:
            await _settle("stream_terminated")


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
