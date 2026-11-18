from pydantic import BaseModel, Field, field_validator

from ..services.watermark import validate_period_name


class BatchInput(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    sample_id: int = Field(gt=0)
    period: str
    candidate_sql: str = Field(min_length=6, max_length=6000)

    @field_validator('period')
    @classmethod
    def period_format(cls, value):
        return validate_period_name(value)
