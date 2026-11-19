from dataclasses import dataclass


@dataclass(frozen=True)
class CohortKey:
    method: str
    bank: str


@dataclass(frozen=True)
class CohortMetrics:
    """
    Traffic metrics for one (method, bank) cohort.

    error_code / error_step are the cohort's dominant *failure signature*: the
    (error_code, error_step) pair shared by the most failed payments in the
    cohort ("NONE" when the cohort has no failures). Captured payments carry no
    error code, so cohorts must not be keyed on it - otherwise every error
    cohort would trivially show a ~100% failure rate.
    """
    method: str
    bank: str
    error_code: str
    error_step: str
    total_count: int
    failed_count: int
    failure_rate: float
    total_amount: int
    failed_amount: int
    signature_failed_count: int = 0
    signature_failed_amount: int = 0
