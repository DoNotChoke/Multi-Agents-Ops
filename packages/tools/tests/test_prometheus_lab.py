"""Against the kind lab's Prometheus; run with `uv run pytest -m lab` after `make lab-up`."""

from datetime import UTC, datetime, timedelta

import pytest
from opsteam_tools.connectors.prometheus import PrometheusConnector

pytestmark = pytest.mark.lab

LAB = PrometheusConnector("http://127.0.0.1:30090", dev_allow=frozenset({"127.0.0.1:30090"}))


async def test_build_info_and_up() -> None:
    assert (await LAB.build_info())["version"]
    assert (await LAB.query("up"))["result"]


async def test_query_range_over_the_last_ten_minutes() -> None:
    end = datetime.now(UTC)
    data = await LAB.query_range("sum(up)", end - timedelta(minutes=10), end, step_s=60)
    assert data["resultType"] == "matrix"
    assert len(data["result"][0]["values"]) >= 5


async def test_alerts_is_a_list() -> None:
    assert isinstance(await LAB.alerts(), list)
