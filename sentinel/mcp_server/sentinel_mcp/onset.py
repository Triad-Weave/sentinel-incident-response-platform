"""Time-series onset and trend detection for MCP metric queries (BUILD_PROMPT section 8).

Baseline = mean and population standard deviation of the first third of the window.
`onset_at` = first later point departing from the baseline mean by > 3 sigma AND >= 50% relative
change (with a small absolute floor for near-zero metrics), or None.
`trend` = `spike` if the last point exceeds baseline by > 3 sigma and >= 50% relative,
else `rising`, `falling`, or `flat` computed from an ordinary least-squares slope over the series.
"""

from __future__ import annotations

import statistics


def _ols_slope(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = statistics.fmean(values)
    num = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(values))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den > 0 else 0.0


def compute_series_stats(points: list[tuple[str, float]]) -> dict[str, object]:
    """Compute baseline, current, min, max, change_pct, trend, and onset_at."""
    if not points:
        return {
            "baseline": None,
            "current": None,
            "min": None,
            "max": None,
            "change_pct": None,
            "onset_at": None,
            "trend": "no_data",
        }

    values = [v for _, v in points]
    split = max(1, len(values) // 3)
    base_slice = values[:split]
    baseline = statistics.fmean(base_slice)
    stdev = statistics.pstdev(base_slice) if len(base_slice) > 1 else 0.0
    current = values[-1]

    if abs(baseline) > 1e-9:
        change_pct = round(((current - baseline) / abs(baseline)) * 100.0, 2)
    elif abs(current) > 1e-9:
        change_pct = 100.0
    else:
        change_pct = 0.0

    # 3-sigma threshold AND at least 50% relative change (tiny absolute floor for near-zero metrics)
    rel_floor = max(abs(baseline) * 0.5, 0.01)
    threshold = max(3.0 * stdev, rel_floor)

    onset_at: str | None = None
    for ts, val in points[split:]:
        if abs(val - baseline) > threshold:
            onset_at = ts
            break

    # Spike if final point is > threshold above baseline AND exceeds prior tail median sharply,
    # otherwise classify via least-squares slope.
    slope = _ols_slope(values)
    total_drift = slope * max(1, len(values) - 1)
    slope_threshold = max(abs(baseline) * 0.15, stdev, 0.01)

    if current - baseline > threshold and (
        len(values) >= 3
        and current > max(values[:-1])
        and (current - statistics.median(values[:-1])) > threshold
    ):
        trend = "spike"
    elif total_drift > slope_threshold and current >= baseline:
        trend = "rising"
    elif total_drift < -slope_threshold and current <= baseline:
        trend = "falling"
    else:
        trend = "flat"

    return {
        "baseline": round(baseline, 5),
        "current": round(current, 5),
        "min": round(min(values), 5),
        "max": round(max(values), 5),
        "change_pct": change_pct,
        "onset_at": onset_at,
        "trend": trend,
    }
