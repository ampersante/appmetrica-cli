from __future__ import annotations

from pydantic import BaseModel


class Application(BaseModel):
    id: int
    name: str
    bundle_id: str | None = None
    model_config = {"extra": "allow"}


class DimensionValue(BaseModel):
    id: str | None = None
    name: str | None = None


class ReportRow(BaseModel):
    dimensions: list[DimensionValue]
    metrics: list[float | int | None]


class ReportResponse(BaseModel):
    query: dict
    data: list[ReportRow]
    total_rows: int
    totals: list[float | int | None] | None = None
    min: list[float | int | None] | None = None
    max: list[float | int | None] | None = None


class FunnelStepResult(BaseModel):
    step: int
    event_name: str
    users: int
    conversion_from_start: float
    conversion_from_prev: float
    drop_off: int


class FunnelResult(BaseModel):
    steps: list[FunnelStepResult]
    total_entered: int
    total_completed: int
    overall_conversion: float


class RetentionRow(BaseModel):
    cohort_date: str
    cohort_size: int
    retention: dict[int, float]


class CohortComparisonRow(BaseModel):
    metric: str
    cohort_a_value: float
    cohort_b_value: float
    difference: float
    difference_pct: float
