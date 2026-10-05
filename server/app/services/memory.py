"""长期记忆服务：提取（对话后自动）、去重、注入块构建（DESIGN.md §13.1）。

提取在每轮对话完成后以后台任务执行（自带独立会话，失败不影响主链路）；
蒸馏功能（§13.2）后续作为记忆的另一个来源直接调用 add_memory。
"""

import asyncio
import json
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import MEMORY_MARKER
from app.llm.gateway import get_llm_backend
from app.models import Memory, Persona

# 注入上限：超过此条数只注入最近 N 条（M4 上 pgvector 后改为语义检索 top-k）
MAX_INJECT = 30
# 每轮最多提取条数
MAX_EXTRACT = 5


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


async def get_recent_memories(db: AsyncSession, user_id, persona_id) -> list[Memory]:
    rows = (
        await db.execute(
            select(Memory)
            .where(Memory.user_id == user_id, Memory.persona_id == persona_id)
            .order_by(Memory.created_at.desc())
            .limit(MAX_INJECT)
        )
    ).scalars().all()
    return list(rows)


def build_memory_block(memories: list[Memory]) -> str | None:
    """把记忆格式化为上下文注入块；无记忆返回 None。"""
    if not memories:
        return None
    lines = "\n".join(f"- {m.content}" for m in reversed(memories))  # 注入时按时间正序
    return f"{MEMORY_MARKER}\n{lines}"


async def add_memory(
    db: AsyncSession,
    *,
    user_id,
    persona_id,
    content: str,
    category: str = "basic",
    source: str = "manual",
    source_ref: dict | None = None,
) -> Memory | None:
    """新增一条记忆，做轻量去重（完全相同或互相包含视为重复）。重复返回 None。"""
    content = content.strip()[:500]
    if not content:
        return None
    existing = (
        await db.execute(select(Memory).where(Memory.user_id == user_id, Memory.persona_id == persona_id))
    ).scalars().all()
    n_new = _norm(content)
    for e in existing:
        n_old = _norm(e.content)
        if n_new == n_old or (len(n_new) > 6 and n_new in n_old) or (len(n_old) > 6 and n_old in n_new):
            return None
    m = Memory(
        user_id=user_id,
        persona_id=persona_id,
        content=content,
        category=category,
        source=source,
        source_ref=source_ref,
    )
    db.add(m)
    await db.commit()
    return m


async def extract_and_store(maker, conversation_id, user_id, persona_id, user_text: str, assistant_text: str) -> None:
    """后台任务：从一轮对话中提取稳定事实并入库。使用调用方传入的 session 工厂，异常静默。"""
    try:
        async with maker() as db:
            persona = await db.get(Persona, persona_id)
            if persona is None or not persona.memory_enabled:
                return
            facts = await extract_facts(db, persona, user_text, assistant_text)
            for fact in facts:
                await add_memory(
                    db,
                    user_id=user_id,
                    persona_id=persona_id,
                    content=fact,
                    source="chat",
                    source_ref={"conversation_id": str(conversation_id)},
                )
    except Exception:
        import traceback

        traceback.print_exc()  # 后台提取失败不影响主链路，但打印便于诊断


EXTRACT_PROMPT = """分析以下对话，提取"用户"透露的值得长期记住的稳定事实（身份、喜好、背景、关系、重要事件）。
临时琐事、寒暄不要提取。每条以"用户"开头的一句话，最多{max_extract}条，没有就输出 []。
只输出 JSON 数组，例如 ["用户是一名程序员"]，不要输出任何其他内容。

对话：
用户: {user_text}
助手: {assistant_text}"""


async def extract_facts(db: AsyncSession, persona: Persona, user_text: str, assistant_text: str) -> list[str]:
    """调 LLM 提取事实；解析失败返回空列表。"""
    model = persona.model or (await _default_model(db))
    prompt = EXTRACT_PROMPT.format(max_extract=MAX_EXTRACT, user_text=user_text[:2000], assistant_text=assistant_text[:1000])
    try:
        result = await get_llm_backend().chat(
            [{"role": "user", "content": prompt}], model, max_tokens=400
        )
    except Exception:
        return []
    return _parse_facts(result.content)


def _parse_facts(text: str) -> list[str]:
    """从回复中解析 JSON 数组；容忍代码围栏等噪音。"""
    text = text.strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    facts = [str(x).strip()[:500] for x in data if isinstance(x, str) and x.strip()]
    return facts[:MAX_EXTRACT]


async def _default_model(db: AsyncSession) -> str:
    from app.services.chat import default_model_string

    return await default_model_string(db)


# 持有后台任务引用，防止 fire-and-forget 任务被垃圾回收（CPython 已知行为）
_background_tasks: set[asyncio.Task] = set()


def schedule_extraction(db: AsyncSession, conversation_id, user_id, persona_id, user_text: str, assistant_text: str) -> None:
    """调度后台提取任务（fire-and-forget）。复用请求的数据库引擎，保证测试/多库环境一致。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    maker = async_sessionmaker(db.bind, expire_on_commit=False)
    task = asyncio.create_task(extract_and_store(maker, conversation_id, user_id, persona_id, user_text, assistant_text))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
