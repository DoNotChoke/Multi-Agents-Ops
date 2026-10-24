import json
import math
from pathlib import Path
from typing import Any

from hypothesis import given
from hypothesis import strategies as st
from opsteam_tools.compress import compress_series
from opsteam_tools.registry.spec import Limits

LIMITS = Limits(max_bytes=1 << 20, max_series=20, max_points=11_000)
T0 = 1_792_000_000


def _series(values: list[Any], **labels: str) -> dict[str, Any]:
    return {"metric": labels, "values": [[T0 + 15 * i, str(v)] for i, v in enumerate(values)]}


def test_spike_in_the_middle_is_one_interval() -> None:
    values = [1.0] * 20 + [9.0] * 4 + [1.0] * 20
    (s,) = compress_series([_series(values, service="checkout")], LIMITS)["series"]
    assert s["labels"] == {"service": "checkout"}
    assert s["stats"] == {
        "min": 1.0, "max": 9.0, "mean": round(sum(values) / len(values), 6), "p95": 9.0,
        "last": 1.0, "baseline": 1.0, "points": 44, "non_finite": 0,
    }  # fmt: skip
    (anomaly,) = s["anomalies"]
    assert (anomaly["start_ts"], anomaly["end_ts"], anomaly["peak"]) == (T0 + 300, T0 + 345, 9.0)
    assert anomaly["start_at"] == "2026-10-14T17:51:40Z"


def test_level_shift_after_the_baseline_is_flagged_not_the_baseline() -> None:
    values = [0.24] * 10 + [2.42] * 19
    (s,) = compress_series([_series(values)], LIMITS)["series"]
    (anomaly,) = s["anomalies"]
    assert anomaly["start_ts"] == T0 + 150 and anomaly["end_ts"] == T0 + 28 * 15
    assert s["stats"]["baseline"] == 0.24


def test_flat_and_tiny_wobble_series_have_no_anomalies() -> None:
    for values in ([5.0] * 30, [5.0, 5.01, 4.99] * 10):
        assert compress_series([_series(values)], LIMITS)["series"][0]["anomalies"] == []


def test_zero_baseline_flags_any_nonzero_value() -> None:
    values = [0.0] * 15 + [0.3, 0.4] + [0.0] * 13
    (s,) = compress_series([_series(values)], LIMITS)["series"]
    assert [a["peak"] for a in s["anomalies"]] == [0.4]


def test_non_finite_values_are_counted_not_used() -> None:
    (s,) = compress_series([_series([1, "NaN", 2, "+Inf", "-Inf", 3])], LIMITS)["series"]
    assert s["stats"]["non_finite"] == 3
    assert (s["stats"]["min"], s["stats"]["max"], s["stats"]["points"]) == (1.0, 3.0, 3)


def test_empty_and_all_nan_series() -> None:
    out = compress_series([_series([]), _series(["NaN", "NaN"])], LIMITS)
    assert [s["stats"] for s in out["series"]] == [
        {"points": 0, "non_finite": 0},
        {"points": 0, "non_finite": 2},
    ]


def test_instant_vector_result() -> None:
    (s,) = compress_series([{"metric": {"job": "a"}, "value": [T0, "4"]}], LIMITS)["series"]
    assert (s["stats"]["last"], s["stats"]["points"]) == (4.0, 1)


def test_series_limit_keeps_the_interesting_ones() -> None:
    quiet = [_series([1.0] * 30, pod=f"quiet-{i}") for i in range(4)]
    loud = _series([1.0] * 15 + [50.0] * 15, pod="loud")
    out = compress_series([*quiet, loud], Limits(max_bytes=1, max_series=2))
    assert out["dropped_series"] == 3 and len(out["series"]) == 2
    assert out["series"][0]["labels"] == {"pod": "loud"}


def test_point_limit_keeps_the_latest_points_and_says_so() -> None:
    out = compress_series([_series(range(100))], Limits(max_bytes=1, max_series=5, max_points=40))
    assert out["truncated"] is True
    assert out["series"][0]["stats"]["points"] == 40 and out["series"][0]["stats"]["last"] == 99.0
    exact = compress_series([_series(range(40))], Limits(max_bytes=1, max_series=1, max_points=40))
    assert exact["truncated"] is False and exact["dropped_series"] == 0


def test_long_label_values_and_many_labels_are_capped() -> None:
    labels = {f"k{i}": "v" * 1000 for i in range(80)}
    (s,) = compress_series([_series([1.0], **labels)], LIMITS)["series"]
    assert len(s["labels"]) == 50 and all(len(v) == 256 for v in s["labels"].values())


def test_anomaly_count_is_capped_at_five() -> None:
    values = [1.0] * 12 + [1.0, 9.0] * 20
    (s,) = compress_series([_series(values)], LIMITS)["series"]
    assert len(s["anomalies"]) == 5
    assert s["anomalies"] == sorted(s["anomalies"], key=lambda a: a["start_ts"])


def test_replayed_db_pool_incident_shows_checkout_latency_jump() -> None:
    replay = Path(__file__).parents[3] / "evals/replay/checkout-db-pool-after-release/replay.json"
    doc = json.loads(replay.read_text(encoding="utf-8"))
    (latency,) = [m for m in doc["metrics"] if m["query_id"] == "latency_p95"]
    out = compress_series(latency["result"], LIMITS)
    by_service = {s["labels"]["service"]: s for s in out["series"]}
    assert by_service["checkout"]["anomalies"][0]["peak"] > 2.0
    assert by_service["payment"]["anomalies"] == []


finite = st.floats(allow_nan=False, allow_infinity=False, min_value=-1e9, max_value=1e9)
values = st.lists(st.one_of(finite, st.sampled_from(["NaN", "+Inf"])), max_size=60)


@given(st.lists(values, max_size=8), st.integers(1, 5), st.integers(1, 50))
def test_output_never_exceeds_the_limits(
    all_values: list[list[Any]], series: int, points: int
) -> None:
    out = compress_series(
        [_series(v, i=str(n)) for n, v in enumerate(all_values)],
        Limits(max_bytes=1, max_series=series, max_points=points),
    )
    assert len(out["series"]) <= series
    assert out["dropped_series"] == max(0, len(all_values) - series)
    for s in out["series"]:
        assert s["stats"]["points"] + s["stats"]["non_finite"] <= points
        assert len(s["anomalies"]) <= 5
        assert all(
            math.isfinite(v) for k, v in s["stats"].items() if k not in ("points", "non_finite")
        )
