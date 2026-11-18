from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..services.watermark import validate_period_name


class BillItem(BaseModel):
    external_ref: str = Field(min_length=4, max_length=120)
    invoice_id: int = Field(gt=0)
    customer: str = Field(min_length=1, max_length=120)
    route: str = Field(min_length=1, max_length=60)
    amount: float = Field(ge=0, le=100000)
    temperature: float = Field(ge=-80, le=80)
    status: Literal['settled', 'pending']


class IngestionInput(BaseModel):
    period: str
    bills: list[BillItem] = Field(min_length=1, max_length=200)

    @field_validator('period')
    @classmethod
    def period_format(cls, value):
        return validate_period_name(value)


class PeriodInput(BaseModel):
    period: str
    status: Literal['open'] = 'open'

    @field_validator('period')
    @classmethod
    def period_format(cls, value):
        return validate_period_name(value)
