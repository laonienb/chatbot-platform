"""账本双阶段结算（billing step 2b）。

阶段A（开流前）：reserve_usage —— INSERT pending 行 + commit。
  即使进程在上游调用途中被 kill，pending 行已在库，对账任务可收编。
阶段B（流结束/异常/取消）：settle_usage —— UPDATE 该行为终态 + commit。
  任何路径（正常完成、上游报错、客户端断开）都收敛到恰好一次 settle。

不变量：任何消耗了上游 token 的请求，最终必须恰好有一行终态记录。
幂等：idempotency_key 唯一约束 —— 重复请求不双扣（冲突时复用已有行）。
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.charge import charge_settled_log
from app.billing.pricing import normalize_model_string, quote_upstream_cost
from app.billing.rating import compute_billed, load_rating_rule, rule_snapshot
from app.models import UsageLog


async def reserve_usage(
    db: AsyncSession,
    *,
    user_id: UUID,
    model_requested: str,
    model_upstream: str,
    source: str = "native",
    conversation_id: UUID | None = None,
    api_key_id: UUID | None = None,
    persona_id: UUID | None = None,
    channel: str | None = None,
    source_uid: str | None = None,
    idempotency_key: str | None = None,
    estimated_prompt_tokens: int = 0,
    estimated_completion_tokens: int = 0,
) -> UUID | None:
    """阶段A：pending 行落库并 commit。返回 reservation 的 usage_logs.id。

    幂等键冲突（同一请求重试）→ 返回已有行的 id，不新建（不双扣）。
    """
    if idempotency_key:
        existing = (
            await db.execute(select(UsageLog).where(UsageLog.idempotency_key == idempotency_key))
        ).scalars().first()
        if existing is not None:
            return existing.id

    # 费率快照在预扣时冻结 —— 结算用同一规则，不受期间规则变更影响
    rule, mr = await load_rating_rule(db, user_id=user_id, model=model_upstream)
    snap = rule_snapshot(rule, mr)

    quote = quote_upstream_cost(
        model_upstream,
        prompt_tokens=estimated_prompt_tokens,
        completion_tokens=estimated_completion_tokens,
    )
    # 预扣估算（供 step 5 准入用；当前 billed 预估按规则算）
    est_billed = compute_billed(
        snap,
        prompt_tokens=estimated_prompt_tokens,
        completion_tokens=estimated_completion_tokens,
    )
    reserved_tokens = estimated_prompt_tokens + estimated_completion_tokens

    # provider 从归一化后的模型串提取（裸名 gpt-4o-mini → openai/gpt-4o-mini）
    normalized = normalize_model_string(model_upstream) or model_upstream or ""

    entry = UsageLog(
        id=uuid4(),
        user_id=user_id,
        api_key_id=api_key_id,
        conversation_id=conversation_id,
        idempotency_key=idempotency_key,
        source=source,
        channel=channel,
        source_uid=source_uid,
        model=model_requested,
        model_upstream=model_upstream,
        provider=normalized.split("/", 1)[0] if "/" in normalized else None,
        persona_id=persona_id,
        status="pending",
        reserved_tokens=reserved_tokens if reserved_tokens else None,
        reserved_cost=est_billed[0] if est_billed else (quote.upstream if quote.upstream else None),
        rate_snapshot=snap,
        rating_rule_id=rule.id if rule else None,
        needs_review=quote.needs_review,
    )
    db.add(entry)
    try:
        await db.commit()
    except IntegrityError:
        # 并发同键（两路同时通过了上面的查重）：回滚后复用已存在行。
        # 回滚只可能丢本 entry —— native 键含 uuid 永不冲突，会话里没有
        # 其他未提交写入；compat 为无状态链路，同样安全。
        await db.rollback()
        if not idempotency_key:
            raise
        existing = (
            await db.execute(select(UsageLog).where(UsageLog.idempotency_key == idempotency_key))
        ).scalars().first()
        if existing is None:
            raise
        return existing.id
    return entry.id


async def settle_usage(
    db: AsyncSession,
    reservation_id: UUID,
    *,
    done,
    attribution: dict | None = None,
    error_code: str | None = None,
) -> None:
    """阶段B：结算 pending 行为终态。幂等：已终态的行直接跳过。

    done 是 StreamDone/LLMResult（prompt_tokens/completion_tokens/
    cache_read_tokens/reasoning_tokens/metering_source/needs_review/finish_reason）。
    """
    entry = await db.get(UsageLog, reservation_id)
    if entry is None or entry.status != "pending":
        return  # 已结算（幂等保护）或行不存在

    prompt = int(getattr(done, "prompt_tokens", 0) or 0)
    completion = int(getattr(done, "completion_tokens", 0) or 0)
    cache_read = int(getattr(done, "cache_read_tokens", 0) or 0)
    reasoning = int(getattr(done, "reasoning_tokens", 0) or 0)
    finish = getattr(done, "finish_reason", None)
    metering = getattr(done, "metering_source", "provider")
    needs_review = bool(getattr(done, "needs_review", False))

    # 模型服务成本归因（协议 §5.1）。cost_usd **只替换 upstream 一侧**（毛利核算），
    # 用户扣费（billed）始终走下面的 rating_rules 快照 —— 两条路径绝不混用。
    svc_cost = getattr(done, "cost_usd", None)
    svc_status = getattr(done, "cost_status", None)
    model_used = getattr(done, "model_used", None)
    svc_provider = getattr(done, "provider", None)

    # 上游成本优先级链：service_exact > service_estimated > price_table > none。
    # 价格表仅在服务未给可信成本时才跑（惰性，省掉 remote-exact 路径的 litellm 开销）。
    if svc_status in ("exact", "estimated") and svc_cost is not None:
        cost_upstream = svc_cost if isinstance(svc_cost, Decimal) else Decimal(str(svc_cost))
        cost_source = "service_exact" if svc_status == "exact" else "service_estimated"
        if svc_status == "estimated":
            needs_review = True  # 采信但标复核
    else:
        # unknown / cost_usd 缺失 / 字段整体缺失（Proxy 期）→ 价格表回落。
        # 价格表也查不到 → (0, needs_review)，cost_source=none，绝不把 unknown 当免费。
        quote = quote_upstream_cost(
            entry.model_upstream or entry.model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_read_tokens=cache_read,
            reasoning_tokens=reasoning,
        )
        cost_upstream = quote.upstream
        cost_source = "none" if quote.needs_review else "price_table"
        needs_review = needs_review or quote.needs_review

    snap = entry.rate_snapshot
    billed = compute_billed(
        snap,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cache_read_tokens=cache_read,
        reasoning_tokens=reasoning,
    )
    if billed is not None:
        cost_billed, currency = billed
    else:
        # 无费率规则（snap 为空/播种失败）→ billed=None → 兜底用 upstream（记一笔待对账）
        cost_billed, currency = cost_upstream, "usd"

    if error_code:
        status = "failed" if completion == 0 else "settled"  # 有部分生成仍记 settled
    else:
        status = "settled"

    # 上游身份归因：模型服务如实回报的 model_used/provider 优先（红线3），
    # 否则沿用现有归一化推导（非 remote 路径 model_used/svc_provider 均为 None）。
    if model_used:
        entry.model_upstream = model_used
    else:
        entry.model_upstream = entry.model_upstream or getattr(done, "model", None) or entry.model
    if svc_provider:
        entry.provider = svc_provider
    elif not entry.provider:
        norm = normalize_model_string(entry.model_upstream)
        entry.provider = norm.split("/", 1)[0] if norm and "/" in norm else None
    entry.prompt_tokens = prompt
    entry.completion_tokens = completion
    entry.cache_read_tokens = cache_read
    entry.reasoning_tokens = reasoning
    entry.metering_source = metering
    entry.attribution = attribution
    entry.cost_upstream = cost_upstream
    entry.cost_source = cost_source
    entry.cost_billed = cost_billed
    entry.currency = currency
    entry.cost = float(cost_billed) if cost_billed is not None else None  # 兼容旧字段
    entry.finish_reason = finish
    entry.error_code = error_code
    entry.needs_review = needs_review
    entry.status = status
    entry.settled_at = datetime.now(UTC)

    # 钱包扣费（同事务，恰好一次由 usage_log_id 幂等保证）。
    # 只扣 credit 币种；余额不足时部分支付并标 needs_review（见 charge_settled_log）。
    # 失败/断流的账行同样扣：prompt token 已被上游消耗，不可免单。
    await charge_settled_log(db, entry)
    await db.commit()
