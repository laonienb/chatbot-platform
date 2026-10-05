"""人设服务：slug 生成与可见性判断。"""

import re
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Persona

_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")


def slugify(text: str) -> str | None:
    """把名称转成合法 slug；无法转换（如纯中文）返回 None。"""
    slug = re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if slug and _SLUG_PATTERN.match(slug):
        return slug
    return None


async def generate_unique_slug(db: AsyncSession, requested: str | None, name: str) -> str:
    """slug 生成策略：显式指定 > 名称转换 > 随机；冲突时自动加后缀（仅自动生成时）。"""
    base = requested or slugify(name) or f"persona-{secrets.token_hex(3)}"
    candidate = base
    for _ in range(5):
        exists = (await db.execute(select(Persona.id).where(Persona.slug == candidate))).scalar_one_or_none()
        if exists is None:
            return candidate
        if requested:
            # 用户显式指定的 slug 不做自动改造，直接让上层报 409
            break
        candidate = f"{base}-{secrets.token_hex(2)}"
    raise ValueError(f"slug 已被占用: {candidate}")


def can_view_persona(persona: Persona, user_id) -> bool:
    return persona.owner_id == user_id or (persona.visibility == "public" and persona.status == "active")
