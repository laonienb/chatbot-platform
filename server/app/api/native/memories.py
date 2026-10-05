"""长期记忆 API：按人设查看/添加（手动），单条编辑/删除。"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models import Memory, Persona
from app.schemas.memory import MemoryCreate, MemoryOut, MemoryUpdate
from app.services.memory import add_memory

router = APIRouter(prefix="/api/v1", tags=["memories"])


async def _owned_persona(db, persona_id: UUID, user_id) -> Persona:
    persona = await db.get(Persona, persona_id)
    if persona is None or persona.owner_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "人设不存在")
    return persona


async def _owned_memory(db, memory_id: UUID, user_id) -> Memory:
    m = await db.get(Memory, memory_id)
    if m is None or m.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "记忆不存在")
    return m


@router.get("/personas/{persona_id}/memories", response_model=list[MemoryOut])
async def list_memories(persona_id: UUID, user: CurrentUser, db: DbSession):
    await _owned_persona(db, persona_id, user.id)
    rows = (
        await db.execute(
            select(Memory)
            .where(Memory.user_id == user.id, Memory.persona_id == persona_id)
            .order_by(Memory.created_at.desc())
        )
    ).scalars().all()
    return rows


@router.post("/personas/{persona_id}/memories", response_model=MemoryOut, status_code=status.HTTP_201_CREATED)
async def create_memory(persona_id: UUID, body: MemoryCreate, user: CurrentUser, db: DbSession):
    await _owned_persona(db, persona_id, user.id)
    m = await add_memory(
        db, user_id=user.id, persona_id=persona_id, content=body.content,
        category=body.category or "basic", source="manual",
    )
    if m is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "已存在相同或相似的记忆")
    return m


@router.patch("/memories/{memory_id}", response_model=MemoryOut)
async def update_memory(memory_id: UUID, body: MemoryUpdate, user: CurrentUser, db: DbSession):
    m = await _owned_memory(db, memory_id, user.id)
    m.content = body.content.strip()[:500]
    await db.commit()
    await db.refresh(m)
    return m


@router.delete("/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(memory_id: UUID, user: CurrentUser, db: DbSession):
    m = await _owned_memory(db, memory_id, user.id)
    await db.delete(m)
    await db.commit()
