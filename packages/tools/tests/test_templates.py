import json
from pathlib import Path
from typing import Any

import pytest
from opsteam_tools.registry.templates import TEMPLATES, TemplateError, render, validate_mapping

CATALOG = {("checkout", "prod"), ("payment", "prod")}
OK = {"service": "checkout", "environment": "prod"}
LAB_MAPPING = json.loads(
    (Path(__file__).parents[3] / "deploy" / "lab" / "metric_mapping.json").read_text()
)

GOLDEN = {
    "http_latency_p95": (
        "histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket"
        '{service="checkout", environment="prod"}[5m])))'
    ),
    "http_error_rate": (
        'sum(rate(http_server_request_duration_seconds_count{service="checkout", '
        'environment="prod", http_response_status_code=~"5.."}[5m])) / '
        'sum(rate(http_server_request_duration_seconds_count{service="checkout", '
        'environment="prod"}[5m]))'
    ),
    "request_rate": (
        'sum(rate(http_server_request_duration_seconds_count{service="checkout", '
        'environment="prod"}[5m]))'
    ),
    "pod_restarts": (
        'sum(increase(kube_pod_container_status_restarts_total{service="checkout", '
        'environment="prod"}[15m]))'
    ),
    "memory_working_set": (
        'sum(container_memory_working_set_bytes{service="checkout", environment="prod"})'
    ),
    "cpu_saturation": (
        'sum(rate(container_cpu_usage_seconds_total{service="checkout", environment="prod"}[5m]))'
        ' / sum(kube_pod_container_resource_limits{resource="cpu", service="checkout", '
        'environment="prod"})'
    ),
}

LAB_GOLDEN = {
    "http_latency_p95": (
        "histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket"
        '{namespace="shop", service="checkout"}[5m])))'
    ),
    "pod_restarts": (
        'sum(increase(kube_pod_container_status_restarts_total{namespace="shop", '
        'pod=~"checkout-.*"}[15m]))'
    ),
}


def test_golden_covers_every_template() -> None:
    assert set(GOLDEN) == set(TEMPLATES) == set(LAB_MAPPING)


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_default_golden(name: str) -> None:
    assert render(name, OK, mapping={}, catalog=CATALOG) == GOLDEN[name]


@pytest.mark.parametrize("name", sorted(LAB_GOLDEN))
def test_lab_mapping_golden(name: str) -> None:
    assert render(name, OK, mapping=LAB_MAPPING, catalog=CATALOG) == LAB_GOLDEN[name]


@pytest.mark.parametrize(
    "params",
    [
        {"service": 'checkout"} or vector(1) #', "environment": "prod"},
        {"service": "checkout}", "environment": "prod"},
        {"service": "check\nout", "environment": "prod"},
        {"service": "сheckout", "environment": "prod"},  # noqa: RUF001 -- Cyrillic es, a homoglyph of c
        {"service": "a" * 129, "environment": "prod"},
        {"service": "", "environment": "prod"},
        {"service": "orders", "environment": "prod"},  # valid shape, not in the catalog
        {"service": "checkout", "environment": "staging"},
        {"service": "checkout"},
        {"service": "checkout", "environment": "prod", "tenant": "x"},
    ],
)
def test_poisoned_or_out_of_scope_params_are_refused(params: dict[str, Any]) -> None:
    with pytest.raises(TemplateError):
        render("http_latency_p95", params, mapping={}, catalog=CATALOG)


def test_unknown_template_is_refused() -> None:
    with pytest.raises(TemplateError):
        render("raw_promql", OK, mapping={}, catalog=CATALOG)


@pytest.mark.parametrize(
    "mapping",
    [
        {"no_such_template": "up"},
        {"request_rate": 'sum(rate(x{tenant="${tenant}"}[5m]))'},
        {"request_rate": 'sum(rate(x{job=~"a$"}[5m]))'},  # bare $ must be written $$
        {"request_rate": "x" * 2001},
    ],
)
def test_bad_mappings_are_refused(mapping: dict[str, str]) -> None:
    with pytest.raises(TemplateError):
        validate_mapping(mapping)


def test_lab_mapping_is_valid() -> None:
    validate_mapping(LAB_MAPPING)
