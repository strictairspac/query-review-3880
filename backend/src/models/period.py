from datetime import datetime
from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column
from ..config.database import Base


class SettlementPeriod(Base):
    """账期入库水位。

    ingest_seq: 本账期已受理的迟到账单序号，每笔有效入库 +1。
    stable_seq: 已验证稳定的水位；只有账期关闭且没有待完成补算时才会追平 ingest_seq。
    auto_close: 关账后因迟到账单进入补算，补算全部完成后自动回到 closed。
    """

    __tablename__ = 'settlement_periods'
    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default='open')
    ingest_seq: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    stable_seq: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    auto_close: Mapped[bool] = mapped_column(Boolean, default=False, server_default='false')
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
