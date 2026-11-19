from dataclasses import dataclass


@dataclass(frozen=True)
class CohortKey:
    method: str
    bank: str
    error_code: str
    error_step: str


@dataclass(frozen=True)
class CohortMetrics:
    method: str
    bank: str
    error_code: str
    error_step: str
    total_count: int
    failed_count: int
    failure_rate: float
    total_amount: int
    failed_amount: int
