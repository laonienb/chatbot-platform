"""数据修复：迁移遗留的历史账行恢复为 settled

b1a2 整表重建时 status 列以 'pending' 为默认值，把迁移前**已完结**的历史账行
全部标成 pending。后果链：
1. /me/usage 的终态口径（settled/failed/abandoned）把 pending 排除 → 历史用量归零；
2. 对账任务 15 分钟后把它们收编为 abandoned + needs_review + reconciled_stale_pending
   —— token 数保住了，但账本里凭空多出一批"疑似崩溃"的待复核噪音
   （AGENTS.md「三」前端实测报告的现象）。

判据：新预扣行必带 idempotency_key（native 为 msg:/regen:，compat 为指纹/显式键），
`idempotency_key IS NULL` 只可能是迁移遗留行。本迁移同时处理两种中间状态：
- pending（对账尚未运行的库，如全新部署）
- abandoned + reconciled_stale_pending（对账已收编的库，如开发库）

幂等：可重复执行（第二次匹配 0 行）。downgrade 为 no-op（状态恢复不可逆推）。
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c2b3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FIX_SQL = """
UPDATE usage_logs
SET status = 'settled',
    started_at = created_at,
    settled_at = created_at,
    needs_review = FALSE,
    error_code = NULL
WHERE idempotency_key IS NULL
  AND (status = 'pending'
       OR (status = 'abandoned' AND error_code = 'reconciled_stale_pending'))
"""


def upgrade() -> None:
    op.execute(_FIX_SQL)


def downgrade() -> None:
    """数据修复不可逆：不回滚（回滚会把历史行重新变成 pending/abandoned 噪音）。"""
    pass
