"""管理端计费接口：毛利监控查询 + 手动触发对账（billing step 6）。"""

from fastapi import APIRouter, Query

from app.api.deps import AdminUser, DbSession
from app.billing.monitor import margin_violations, summarize_margin
from app.billing.quota import reconcile_pending

router = APIRouter(prefix="/api/v1/admin/billing", tags=["admin"])


@router.get("/margin")
async def margin(admin: AdminUser, db: DbSession, limit: int = Query(20, ge=1, le=200)):
    """毛利汇总 + 负毛利明细。violations > 0 说明费率配错，需人工检查。"""
    summary = await summarize_margin(db)
    rows = await margin_violations(db, limit=limit)
    return {
        "requests": summary["requests"],
        "violations": summary["violations"],
        "billed_total": float(summary["billed_total"]),
        "revenue_usd": float(summary["revenue_usd"]),
        "upstream_total": float(summary["upstream_total"]),
        "margin": float(summary["margin"]),
        "margin_ratio": float(summary["margin_ratio"]) if summary["margin_ratio"] is not None else None,
        "violations_detail": [
            {
                "id": str(r.id),
                "user_id": str(r.user_id),
                "model": r.model_upstream or r.model,
                "cost_billed": float(r.cost_billed),
                "cost_upstream": float(r.cost_upstream),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.post("/reconcile")
async def reconcile_now(admin: AdminUser, db: DbSession):
    """立即收编残留 pending 账行（正常由后台循环按 reconcile_interval_seconds 执行）。"""
    abandoned = await reconcile_pending(db)
    return {"abandoned": abandoned}
