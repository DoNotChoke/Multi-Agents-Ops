import pytest
from opsteam_incidents.severity import severity


@pytest.mark.parametrize(
    ("hints", "kwargs", "expected"),
    [
        ([], {}, "info"),
        ([None, "info"], {}, "info"),
        (["warning"], {}, "medium"),
        (["warning", "critical", None], {}, "high"),
        # Each condition raises one step; critical is the ceiling.
        (["info"], {"production_high": True}, "medium"),
        (["warning"], {"production_high": True}, "high"),
        (["critical"], {"production_high": True}, "critical"),
        (["warning"], {"services_affected": 3}, "high"),
        (["warning"], {"services_affected": 2}, "medium"),
        (["warning"], {"slo_fast_burn": True}, "high"),
        (
            ["critical"],
            {"production_high": True, "services_affected": 5, "slo_fast_burn": True},
            "critical",
        ),
        (["info"], {"production_high": True, "slo_fast_burn": True}, "high"),
    ],
)
def test_severity(hints: list[str | None], kwargs: dict[str, object], expected: str) -> None:
    assert severity(hints, **kwargs) == expected  # type: ignore[arg-type]
