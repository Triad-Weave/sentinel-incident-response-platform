"""Platform shared eval package (PB-15)."""

from platform_eval.schema import (
    ComparisonResult,
    EvalItem,
    EvalResultV1,
    MetricRange,
    compare_eval_runs,
    validate_eval_payload,
)

__all__ = [
    "EvalResultV1",
    "EvalItem",
    "MetricRange",
    "ComparisonResult",
    "validate_eval_payload",
    "compare_eval_runs",
]
