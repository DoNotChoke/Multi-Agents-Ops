"""Summarise Prometheus series for an LLM: a few statistics and the intervals that look abnormal."""

import math
from datetime import UTC, datetime
from statistics import median
from typing import Any

from opsteam_tools.registry.spec import Limits

MAX_ANOMALIES = 5
MAD_K = 5.0  # shortcut: uncalibrated threshold, revisit with eval baselines in T-4.7
RELATIVE = 0.5  # a flat baseline still needs a 50% change to count
MAX_LABELS, MAX_LABEL_VALUE = 50, 256


def compress_series(result: list[dict[str, Any]], limits: Limits) -> dict[str, Any]:
    """Compress a Prometheus vector or matrix `result`, saying what was truncated or dropped."""
    compressed = [_compress_one(s, limits.max_points) for s in result]
    truncated = any(c.pop("_truncated") for c in compressed)
    if len(compressed) > limits.max_series:
        compressed.sort(key=lambda c: c["_score"], reverse=True)
    kept = compressed[: limits.max_series]
    for c in compressed:
        c.pop("_score")
    return {"series": kept, "truncated": truncated, "dropped_series": len(compressed) - len(kept)}


def _compress_one(series: dict[str, Any], max_points: int) -> dict[str, Any]:
    points = series.get("values") or ([series["value"]] if "value" in series else [])
    truncated = bool(max_points) and len(points) > max_points
    if truncated:
        points = points[-max_points:]  # the latest data matters most
    finite = [(float(ts), v) for ts, raw in points if math.isfinite(v := float(raw))]
    labels = {
        str(k): str(v)[:MAX_LABEL_VALUE]
        for k, v in list(series.get("metric", {}).items())[:MAX_LABELS]
    }
    out: dict[str, Any] = {
        "labels": labels,
        "stats": {"points": len(finite), "non_finite": len(points) - len(finite)},
        "anomalies": [],
        "_truncated": truncated,
        "_score": (False, 0.0),
    }
    if not finite:
        return out

    values = [v for _, v in finite]
    # The window usually starts before the incident, so its first third is the reference.
    baseline = median(values[: max(3, len(values) // 3)])
    spread = median(abs(v - baseline) for v in values[: max(3, len(values) // 3)])
    threshold = max(MAD_K * 1.4826 * spread, RELATIVE * abs(baseline))

    intervals: list[list[tuple[float, float]]] = []
    previous_flagged = False
    for ts, v in finite:
        flagged = abs(v - baseline) > threshold
        if flagged and previous_flagged:
            intervals[-1].append((ts, v))
        elif flagged:
            intervals.append([(ts, v)])
        previous_flagged = flagged
    peaks = [max((v for _, v in run), key=lambda v: abs(v - baseline)) for run in intervals]
    biggest = sorted(range(len(intervals)), key=lambda i: abs(peaks[i] - baseline), reverse=True)
    out["anomalies"] = [
        {
            "start_ts": intervals[i][0][0],
            "end_ts": intervals[i][-1][0],
            "start_at": _iso(intervals[i][0][0]),
            "end_at": _iso(intervals[i][-1][0]),
            "peak": peaks[i],
        }
        for i in sorted(biggest[:MAX_ANOMALIES])  # chronological
    ]

    ordered = sorted(values)
    out["stats"] = {
        "min": ordered[0],
        "max": ordered[-1],
        "mean": round(sum(values) / len(values), 6),
        "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "last": values[-1],
        "baseline": baseline,
        "points": len(values),
        "non_finite": len(points) - len(values),
    }
    out["_score"] = (bool(intervals), max(abs(v - baseline) for v in values))
    return out


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
