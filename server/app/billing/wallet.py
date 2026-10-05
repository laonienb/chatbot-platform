"""充值钱包（步骤4）：充值/消费/退款/过期。积分制和订阅制共用这一个余额。"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, utcnow


class Wallet(Base):
    """用户的积分余额。唯一真源是 wallet_entries 流水，本表为快写缓存。"""

    __tablename__ = "wallets"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=Decimal("0"))
    lifetime_topup: Mapped[Decimal] = mapped_column(Numeric(20, 6), default=Decimal("0"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WalletEntry(Base):
    """余额变动流水：append-only，与 usage_logs 账本互为镜像。"""

    __tablename__ = "wallet_entries"
    # 幂等的最后防线：一条账行最多一笔扣款流水（NULL 不受约束限制，充值/赠送不受影响）
    __table_args__ = (UniqueConstraint("usage_log_id", name="uq_wallet_entries_usage_log"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    entry_type: Mapped[str] = mapped_column(String(16))  # topup | consume | refund | grant | expire
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 6))  # 正入负出
    balance_after: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    usage_log_id: Mapped[UUID | None] = mapped_column(ForeignKey("usage_logs.id"))
    note: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
