"""billing step 3+4: 费率表（rating_rules/model_ratings）+ 钱包（wallets/wallet_entries）

Revision ID: c2b3d4e5f6a7
Revises: b1a2c3d4e5f6
Create Date: 2026-10-06 00:00:01.000000

rating_rules：一条计费规则 = 一个计费口径（per_token / per_request / hybrid /
period_allowance）。请求时把命中的规则快照进 usage_logs.rate_snapshot，
规则改了历史账单仍可复现（账单可复现是对外承诺的前提）。

model_ratings：credit_multiplier —— 「不同模型按一定比例扣积分」的可调旋钮，
不改代码即可调价。

wallets/wallet_entries：积分余额 + append-only 流水。钱包唯一真源是流水，
wallets 表为快写缓存。充值=预收账款（负债），消费时才转收入 —— 这是
会计口径，对账时必须能解释。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c2b3d4e5f6a7'
down_revision: Union[str, None] = 'b1a2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'rating_rules',
        sa.Column('id', sa.String(length=40), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('scope', sa.String(length=16), nullable=False),
        sa.Column('scope_ref_id', sa.String(length=64), nullable=True),
        sa.Column('priority', sa.Integer(), nullable=False),
        sa.Column('charge_dimension', sa.String(length=32), nullable=False),
        sa.Column('unit', sa.String(length=16), nullable=False),
        sa.Column('base_amount', sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column('rate_input', sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column('rate_output', sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column('rate_reasoning', sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column('rate_cache_read', sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column('rate_cache_write', sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column('cost_basis', sa.String(length=16), nullable=False),
        sa.Column('effective_from', sa.DateTime(timezone=True), nullable=False),
        sa.Column('effective_to', sa.DateTime(timezone=True), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'model_ratings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('rating_rule_id', sa.String(length=40), nullable=False),
        sa.Column('model', sa.String(length=128), nullable=False),
        sa.Column('credit_multiplier', sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column('base_credits_per_request', sa.Numeric(precision=20, scale=10), nullable=False),
        sa.ForeignKeyConstraint(['rating_rule_id'], ['rating_rules.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('rating_rule_id', 'model', name='uq_rating_rule_model'),
    )
    op.create_index('ix_model_ratings_rating_rule_id', 'model_ratings', ['rating_rule_id'], unique=False)

    op.create_table(
        'wallets',
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('balance', sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column('lifetime_topup', sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('user_id'),
    )
    op.create_table(
        'wallet_entries',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('entry_type', sa.String(length=16), nullable=False),
        sa.Column('amount', sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column('balance_after', sa.Numeric(precision=20, scale=6), nullable=False),
        sa.Column('usage_log_id', sa.Uuid(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['usage_log_id'], ['usage_logs.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('usage_log_id', name='uq_wallet_entries_usage_log'),
    )
    op.create_index('ix_wallet_entries_user_id', 'wallet_entries', ['user_id'], unique=False)

    # 存量用户补发注册赠送积分（余额准入 402 上线时点的初始化；金额与
    # settings.signup_grant_credits 默认一致）。钱包唯一真源是流水，两表同批写入。
    from datetime import UTC, datetime

    from app.config import get_settings

    grant = get_settings().signup_grant_credits
    if grant > 0:
        bind = op.get_bind()
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")
        for (uid,) in bind.execute(sa.text("SELECT id FROM users")).fetchall():
            bind.execute(
                sa.text(
                    "INSERT INTO wallets (user_id, balance, lifetime_topup, updated_at) "
                    "VALUES (:u, :b, :b, :now)"
                ),
                {"u": uid, "b": float(grant), "now": now},
            )
            bind.execute(
                sa.text(
                    "INSERT INTO wallet_entries "
                    "(id, user_id, entry_type, amount, balance_after, note, created_at) "
                    "VALUES (lower(hex(randomblob(16))), :u, 'grant', :b, :b, :note, :now)"
                ),
                {"u": uid, "b": float(grant), "now": now, "note": "存量用户计费上线赠送"},
            )


def downgrade() -> None:
    op.drop_index('ix_wallet_entries_user_id', table_name='wallet_entries')
    op.drop_table('wallet_entries')
    op.drop_table('wallets')
    op.drop_index('ix_model_ratings_rating_rule_id', table_name='model_ratings')
    op.drop_table('model_ratings')
    op.drop_table('rating_rules')
