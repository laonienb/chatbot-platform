"""人设 CRUD（M0 范围：创建/列表/详情/更新/删除；市场与 fork 在 M3）。"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import or_, select

from app.api.deps import CurrentUser, DbSession
from app.models import Conversation, Persona
from app.schemas.persona import PersonaCreate, PersonaOut, PersonaUpdate
from app.services.persona import can_view_persona, generate_unique_slug

router = APIRouter(prefix="/api/v1/personas", tags=["personas"])


@router.get("", response_model=list[PersonaOut])
async def list_personas(user: CurrentUser, db: DbSession):
    """我拥有的 + 市场可见（public 且 active）的人设。"""
    stmt = (
        select(Persona)
        .where(
            or_(
                Persona.owner_id == user.id,
                (Persona.visibility == "public") & (Persona.status == "active"),
            )
        )
        .order_by(Persona.created_at.desc())
    )
    return (await db.execute(stmt)).scalars().all()


@router.post("", response_model=PersonaOut, status_code=status.HTTP_201_CREATED)
async def create_persona(body: PersonaCreate, user: CurrentUser, db: DbSession):
    try:
        slug = await generate_unique_slug(db, body.slug, body.name)
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    persona = Persona(owner_id=user.id, slug=slug, **body.model_dump(exclude={"slug"}))
    db.add(persona)
    await db.commit()
    await db.refresh(persona)
    return persona


@router.get("/{persona_id}", response_model=PersonaOut)
async def get_persona(persona_id: UUID, user: CurrentUser, db: DbSession):
    persona = await db.get(Persona, persona_id)
    if persona is None or not can_view_persona(persona, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "人设不存在")
    return persona


@router.patch("/{persona_id}", response_model=PersonaOut)
async def update_persona(persona_id: UUID, body: PersonaUpdate, user: CurrentUser, db: DbSession):
    persona = await db.get(Persona, persona_id)
    if persona is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "人设不存在")
    if persona.owner_id != user.id and user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能修改自己的人设")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(persona, field, value)
    await db.commit()
    await db.refresh(persona)
    return persona


@router.delete("/{persona_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_persona(persona_id: UUID, user: CurrentUser, db: DbSession):
    persona = await db.get(Persona, persona_id)
    if persona is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "人设不存在")
    if persona.owner_id != user.id and user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "只能删除自己的人设")
    referenced = (
        await db.execute(select(Conversation.id).where(Conversation.persona_id == persona_id).limit(1))
    ).scalar_one_or_none()
    if referenced is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "该人设仍被会话引用，请先删除相关会话")
    await db.delete(persona)
    await db.commit()
