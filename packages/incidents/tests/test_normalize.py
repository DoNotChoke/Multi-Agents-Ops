import pytest
from opsteam_incidents.normalize import resolve_service

CATALOG = {("checkout", "shop"): "svc_CHECKOUT", ("checkout", "prod"): "svc_PROD"}


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        # OTel names first, then the common short ones.
        (
            {"service.name": "checkout", "deployment.environment.name": "prod"},
            {"service_id": "svc_PROD", "name": "checkout", "environment": "prod"},
        ),
        (
            {"service": "checkout", "environment": "prod", "namespace": "shop"},
            {"service_id": "svc_PROD", "name": "checkout", "environment": "prod"},
        ),
        (
            {"service": "checkout", "env": "prod"},
            {"service_id": "svc_PROD", "name": "checkout", "environment": "prod"},
        ),
        # Without an environment label the namespace stands in, as in the Prometheus import.
        (
            {"service": "checkout", "namespace": "shop"},
            {"service_id": "svc_CHECKOUT", "name": "checkout", "environment": "shop"},
        ),
        # Unknown pair: kept raw so the incident still opens and the UI can offer to add it.
        (
            {"service": "cart", "namespace": "shop"},
            {"name": "cart", "environment": "shop"},
        ),
        ({"service": "checkout"}, {"name": "checkout"}),
        ({"alertname": "NodeDown"}, {}),
    ],
)
def test_resolve_service(labels: dict[str, str], expected: dict[str, str]) -> None:
    assert resolve_service(labels, CATALOG) == expected


def test_names_match_exactly() -> None:
    assert "service_id" not in resolve_service({"service": "Checkout", "env": "prod"}, CATALOG)
    assert "service_id" not in resolve_service({"service": "checkout ", "env": "prod"}, CATALOG)
