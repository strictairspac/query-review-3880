from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import ForeignKey, JSON, Integer, String, Text, text
from ..config.database import Base


class Evidence(Base):
    __tablename__ = 'evidence'
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey('batches.id'), unique=True)
    period: Mapped[str] = mapped_column(String(20), default='', index=True)
    result: Mapped[dict] = mapped_column(JSON)
    # 本证据实际采用的账期入库水位。
    watermark_seq: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    # 乐观并发版本；补算/复核提交必须匹配读取时的版本，避免互相覆盖。
    version: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    # valid=可查；stale=迟到账单已使其部分失效，补算进行中；failed=无结果。
    state: Mapped[str] = mapped_column(String(20), default='valid', server_default=text("'valid'"))
    affected_customers: Mapped[list] = mapped_column(JSON, default=list)
    invalidated_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
