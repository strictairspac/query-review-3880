from datetime import datetime
from sqlalchemy import DateTime, Integer, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from ..config.database import Base


class BillIngestion(Base):
    """迟到账单入库事件。external_ref 与 (账期, invoice_id) 双重幂等。"""

    __tablename__ = 'bill_ingestions'
    __table_args__ = (
        UniqueConstraint('period', 'external_ref', name='uq_ingestion_ref'),
        UniqueConstraint('period', 'invoice_id', name='uq_ingestion_invoice'),
        UniqueConstraint('period', 'seq', name='uq_ingestion_seq'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(20), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    external_ref: Mapped[str] = mapped_column(String(120))
    invoice_id: Mapped[int] = mapped_column(Integer)
    customer: Mapped[str] = mapped_column(String(120), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
