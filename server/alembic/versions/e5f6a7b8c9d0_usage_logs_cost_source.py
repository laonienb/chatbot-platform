"""billing S6: usage_logs 新增 cost_source 列（上游成本来源）

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-10-09 00:00:00.000000

模型服务解耦协议 §5.1（docs/model-service-protocol.md）：`metering_source` 记的是
**token 计量来源**，不是成本来源。接入模型服务后 cost_upstream 可能来自三处，必须
分得清「Proxy 过渡期本来就没有」与「provider 漏报」——否则对账清单无法归因。

cost_source 取值：
  service_exact      采信模型服务 x_model_service.cost_usd（cost_status=exact）
  service_estimated  采信但 needs_review（cost_status=estimated）
  price_table        服务未给/unknown → 平台 litellm 价格表推算
  none               价格表也查不到 → (0, needs_review)，进对账清单

nullable：历史账行（模型服务接入前）无此语义，留 NULL 即可，不回填。
SQLite 支持直接 ADD COLUMN 可空列，无需整表重建。幂等由 alembic 版本链保证。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('usage_logs', sa.Column('cost_source', sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column('usage_logs', 'cost_source')
