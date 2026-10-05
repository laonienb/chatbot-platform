"""billing step 2a: usage_logs 账本改造（双阶段状态机/幂等/计量精度/价格快照）

Revision ID: b1a2c3d4e5f6
Revises: f877d5ae1d0c
Create Date: 2026-10-06 00:00:00.000000

设计红线（见 app/billing/pricing.py）：
- 幂等键唯一约束 → 重复请求不双扣
- status 状态机 → pending 先落库，崩溃可由对账任务收编
- metering_source 区分实测/估算 → 可审计
- rate_snapshot → 费率规则改了，历史账单仍可复现

SQLite 不支持 ALTER 加约束，整表重建走 batch 模式（copy-and-move）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b1a2c3d4e5f6'
down_revision: Union[str, None] = 'f877d5ae1d0c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# usage_logs 的全部列（batch 模式重建时必须给全）
_NEW_COLS = [
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('api_key_id', sa.Uuid(), nullable=True),
    sa.Column('conversation_id', sa.Uuid(), nullable=True),
    sa.Column('model', sa.String(length=128), nullable=False),  # 请求的 model 字段
    sa.Column('persona_id', sa.Uuid(), nullable=True),
    sa.Column('prompt_tokens', sa.Integer(), nullable=True),
    sa.Column('completion_tokens', sa.Integer(), nullable=True),
    sa.Column('cost', sa.Numeric(precision=12, scale=6), nullable=True),  # 兼容旧字段 = cost_billed
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    # --- billing step 2a 新增 ---
    sa.Column('idempotency_key', sa.String(length=128), nullable=True),
    sa.Column('source', sa.String(length=16), server_default='native', nullable=False),
    sa.Column('channel', sa.String(length=16), nullable=True),
    sa.Column('source_uid', sa.String(length=128), nullable=True),
    sa.Column('model_upstream', sa.String(length=128), nullable=True),
    sa.Column('provider', sa.String(length=32), nullable=True),
    sa.Column('cache_read_tokens', sa.Integer(), nullable=True),
    sa.Column('reasoning_tokens', sa.Integer(), nullable=True),
    sa.Column('metering_source', sa.String(length=16), nullable=True),
    sa.Column('attribution', sa.JSON(), nullable=True),
    sa.Column('rating_rule_id', sa.String(length=40), nullable=True),
    sa.Column('rate_snapshot', sa.JSON(), nullable=True),
    sa.Column('cost_upstream', sa.Numeric(precision=20, scale=10), nullable=True),
    sa.Column('cost_billed', sa.Numeric(precision=20, scale=10), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=True),
    sa.Column('status', sa.String(length=16), server_default='pending', nullable=False),
    sa.Column('reserved_tokens', sa.Integer(), nullable=True),
    sa.Column('reserved_cost', sa.Numeric(precision=20, scale=10), nullable=True),
    sa.Column('finish_reason', sa.String(length=32), nullable=True),
    sa.Column('error_code', sa.String(length=64), nullable=True),
    sa.Column('needs_review', sa.Boolean(), server_default=sa.text('0'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
]


def upgrade() -> None:
    # batch 内复制旧数据：created_at / id 等已有列保持不变
    with op.batch_alter_table('usage_logs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('idempotency_key', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('source', sa.String(length=16), server_default='native', nullable=False))
        batch_op.add_column(sa.Column('channel', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('source_uid', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('model_upstream', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('provider', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('cache_read_tokens', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('reasoning_tokens', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('metering_source', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('attribution', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('rating_rule_id', sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column('rate_snapshot', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('cost_upstream', sa.Numeric(precision=20, scale=10), nullable=True))
        batch_op.add_column(sa.Column('cost_billed', sa.Numeric(precision=20, scale=10), nullable=True))
        batch_op.add_column(sa.Column('currency', sa.String(length=3), nullable=True))
        batch_op.add_column(sa.Column('status', sa.String(length=16), server_default='pending', nullable=False))
        batch_op.add_column(sa.Column('reserved_tokens', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('reserved_cost', sa.Numeric(precision=20, scale=10), nullable=True))
        batch_op.add_column(sa.Column('finish_reason', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('error_code', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('needs_review', sa.Boolean(), server_default=sa.text('0'), nullable=False))
        batch_op.add_column(sa.Column('started_at', sa.DateTime(timezone=True),
                                     server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False))
        batch_op.add_column(sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_unique_constraint('uq_usage_logs_idempotency_key', ['idempotency_key'])
        # 预扣状态扫描对账任务的查询条件：status + started_at
        batch_op.create_index('ix_usage_logs_status', ['status'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('usage_logs', schema=None) as batch_op:
        batch_op.drop_index('ix_usage_logs_status')
        batch_op.drop_constraint('uq_usage_logs_idempotency_key', type_='unique')
        for col in ['settled_at', 'started_at', 'needs_review', 'error_code', 'finish_reason', 'reserved_cost',
                    'reserved_tokens', 'status', 'currency', 'cost_billed', 'cost_upstream', 'rate_snapshot',
                    'rating_rule_id', 'attribution', 'metering_source', 'reasoning_tokens', 'cache_read_tokens',
                    'provider', 'model_upstream', 'source_uid', 'channel', 'source', 'idempotency_key']:
            batch_op.drop_column(col)
