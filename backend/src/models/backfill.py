from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from ..config.database import Base


class Backfill(Base):
    """补算批次：把某个账期水位之前的迟到账单补进已有证据。

    (evidence_id, target_seq) 唯一，重复投递只会命中同一批次，不会重复计入。
    """

    __tablename__ = 'backfill_batches'
    __table_args__ = (UniqueConstraint('evidence_id', 'target_seq', name='uq_backfill_target'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(20), index=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('batches.id'), index=True)
    evidence_id: Mapped[int] = mapped_column(ForeignKey('evidence.id'), index=True)
    target_seq: Mapped[int] = mapped_column(Integer)
    affected_customers: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default='queued')
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())
