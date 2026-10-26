"""Incident severity from its firing alerts and context; deterministic, never an LLM call."""

from collections.abc import Iterable

_HINT_RANK = {"info": 0, "warning": 1, "critical": 2}
# Hints map info/warning/critical to info/medium/high; each raised step can reach critical.
_DISPLAY = ("info", "medium", "high", "critical")


def severity(
    hints: Iterable[str | None],
    *,
    production_high: bool = False,
    services_affected: int = 1,
    slo_fast_burn: bool = False,
) -> str:
    """Return info|medium|high|critical: the worst hint, one step up per aggravating condition."""
    rank = max((_HINT_RANK.get(h or "info", 0) for h in hints), default=0)
    rank += production_high + (services_affected >= 3) + slo_fast_burn
    return _DISPLAY[min(rank, len(_DISPLAY) - 1)]
