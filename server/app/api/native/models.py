"""模型注册表：GET /api/v1/models（聊天下拉用，首访自动播种）+ 管理员 CRUD。"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import AdminUser, CurrentUser, DbSession
from app.models import LlmModel
from app.schemas.llm_model import LlmModelCreate, LlmModelOut, LlmModelUpdate, to_out

router = APIRouter(prefix="/api/v1", tags=["models"])
admin_router = APIRouter(prefix="/api/v1/admin/models", tags=["admin"])

# 首次使用的播种清单（mock 后端下即可聊；接真实模型后管理员可增删改）
SEED_MODELS = [
    {"name": "GPT-4o mini", "model": "gpt-4o-mini", "is_default": True, "sort": 0},
    {"name": "GPT-4o", "model": "gpt-4o", "sort": 1},
    {"name": "DeepSeek Chat", "model": "deepseek-chat", "sort": 2},
]


async def _seed_if_empty(db) -> None:
    any_model = (await db.execute(select(LlmModel).limit(1))).scalars().first()
    if any_model is not None:
        return
    for item in SEED_MODELS:
        db.add(LlmModel(**item))
    await db.commit()


@router.get("/models", response_model=list[LlmModelOut])
async def list_models(user: CurrentUser, db: DbSession):
    """启用的模型列表（聊天界面下拉）。首次访问播种默认清单。"""
    await _seed_if_empty(db)
    rows = (
        await db.execute(
            select(LlmModel).where(LlmModel.enabled == True).order_by(LlmModel.sort, LlmModel.created_at)  # noqa: E712
        )
    ).scalars().all()
    return [to_out(r) for r in rows]


# ---------- 管理员 ----------


@admin_router.get("", response_model=list[LlmModelOut])
async def admin_list(admin: AdminUser, db: DbSession):
    rows = (await db.execute(select(LlmModel).order_by(LlmModel.sort, LlmModel.created_at))).scalars().all()
    return [to_out(r) for r in rows]


@admin_router.post("", response_model=LlmModelOut, status_code=status.HTTP_201_CREATED)
async def admin_create(body: LlmModelCreate, admin: AdminUser, db: DbSession):
    exists = (
        await db.execute(select(LlmModel).where(LlmModel.model == body.model))
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"模型 {body.model} 已存在")
    row = LlmModel(**body.model_dump())
    db.add(row)
    await db.flush()
    if body.is_default:
        await _clear_default(db, row.id)
    await db.commit()
    await db.refresh(row)
    return to_out(row)


@admin_router.patch("/{model_id}", response_model=LlmModelOut)
async def admin_update(model_id: UUID, body: LlmModelUpdate, admin: AdminUser, db: DbSession):
    row = await db.get(LlmModel, model_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模型不存在")
    data = body.model_dump(exclude_unset=True)
    if data.get("api_key") == "":
        data.pop("api_key")  # 空串表示不修改
    for field, value in data.items():
        setattr(row, field, value)
    await db.flush()
    if body.is_default:
        await _clear_default(db, row.id)
    await db.commit()
    await db.refresh(row)
    return to_out(row)


@admin_router.delete("/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete(model_id: UUID, admin: AdminUser, db: DbSession):
    row = await db.get(LlmModel, model_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模型不存在")
    was_default = row.is_default
    await db.delete(row)
    await db.flush()
    if was_default:
        # 删除默认模型必须自动回退，否则全列表无默认、「会话回退到默认模型」语义悬空
        await _promote_default(db)
    await db.commit()


async def _clear_default(db, keep_id: UUID) -> None:
    rows = (await db.execute(select(LlmModel).where(LlmModel.is_default == True))).scalars().all()  # noqa: E712
    for r in rows:
        if r.id != keep_id:
            r.is_default = False


async def _promote_default(db) -> None:
    """从启用模型中把 sort 最小（并列取创建最早）者提升为新默认。

    无启用模型时不提升（列表已空或全禁用，无默认可言）。
    """
    candidate = (
        await db.execute(
            select(LlmModel)
            .where(LlmModel.enabled == True)  # noqa: E712
            .order_by(LlmModel.sort, LlmModel.created_at)
            .limit(1)
        )
    ).scalars().first()
    if candidate is not None:
        candidate.is_default = True
