"""人设 CRUD 与市场（M0 范围：CRUD；市场：搜索/标签/fork）。"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import String, cast, or_, select

from app.api.deps import CurrentUser, DbSession
from app.models import Conversation, Persona, User
from app.schemas.persona import PersonaCreate, PersonaMarketOut, PersonaOut, PersonaUpdate
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


@router.get("/market", response_model=list[PersonaMarketOut])
async def persona_market(
    user: CurrentUser,
    db: DbSession,
    q: str | None = None,
    tag: str | None = None,
    limit: int = 60,
):
    """公共市场：public 且 active 的人设，支持关键词（名称/人设正文）与标签过滤。

    注意：必须声明在 /{persona_id} 之前，否则 "market" 会被当作 UUID 解析失败。
    """
    limit = min(max(limit, 1), 100)
    stmt = (
        select(Persona, User.display_name.label("owner_name"))
        .join(User, Persona.owner_id == User.id)
        .where(Persona.visibility == "public", Persona.status == "active")
        .order_by(Persona.created_at.desc())
        .limit(limit)
    )
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Persona.name.ilike(like), Persona.system_prompt.ilike(like)))
    if tag:
        # tags 是 JSON 数组列，SQLite/PG 通用做法：转文本后按 "tag" 匹配
        stmt = stmt.where(cast(Persona.tags, String).ilike(f'%"{tag.strip()}"%'))
    rows = (await db.execute(stmt)).all()
    return [
        PersonaMarketOut(
            **PersonaOut.model_validate(persona).model_dump(),
            owner_name=owner_name,
        )
        for persona, owner_name in rows
    ]


@router.get("/{persona_id}", response_model=PersonaOut)
async def get_persona(persona_id: UUID, user: CurrentUser, db: DbSession):
    persona = await db.get(Persona, persona_id)
    if persona is None or not can_view_persona(persona, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "人设不存在")
    return persona


@router.post("/{persona_id}/fork", response_model=PersonaOut, status_code=status.HTTP_201_CREATED)
async def fork_persona(persona_id: UUID, user: CurrentUser, db: DbSession):
    """复制他人（或自己）的人设为私有副本。"""
    persona = await db.get(Persona, persona_id)
    if persona is None or not can_view_persona(persona, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "人设不存在")
    if persona.owner_id == user.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "自己的人设无需 fork，直接编辑即可")
    try:
        slug = await generate_unique_slug(db, None, f"{persona.slug}-fork")
    except ValueError:
        raise HTTPException(status.HTTP_409_CONFLICT, "无法生成唯一 slug")
    forked = Persona(
        owner_id=user.id,
        slug=slug,
        name=persona.name,
        avatar_url=persona.avatar_url,
        system_prompt=persona.system_prompt,
        model=persona.model,
        temperature=persona.temperature,
        top_p=persona.top_p,
        max_tokens=persona.max_tokens,
        opening_message=persona.opening_message,
        visibility="private",
        forked_from=persona.id,
        tags=persona.tags,
    )
    db.add(forked)
    await db.commit()
    await db.refresh(forked)
    return forked


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
