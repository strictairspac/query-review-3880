from pydantic import BaseModel


class EvidenceOutput(BaseModel):
    id: int
    batch_id: int
    period: str | None = None
    state: str | None = None
    watermark_seq: int | None = None
    affected_customers: list[str] | None = None
    reason: str | None = None
    version: int | None = None
    watermark: dict | None = None
    batch_status: str | None = None
    batch_status_reason: str | None = None
    result: dict
