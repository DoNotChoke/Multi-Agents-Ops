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


MAPPINGS = [
    # (matchers, service_id, name, environment), most specific first
    ({"service": "cart", "namespace": "shop"}, "svc_CHECKOUT", "checkout", "shop"),
    ({"job": "payment-api"}, "svc_PROD", "checkout", "prod"),
]


def test_a_mapping_maps_what_labels_alone_cannot() -> None:
    ref = resolve_service({"service": "cart", "namespace": "shop"}, CATALOG, MAPPINGS)
    assert ref == {"service_id": "svc_CHECKOUT", "name": "checkout", "environment": "shop"}
    ref = resolve_service({"job": "payment-api", "alertname": "X"}, CATALOG, MAPPINGS)
    assert ref == {"service_id": "svc_PROD", "name": "checkout", "environment": "prod"}


def test_every_matcher_must_match_and_the_catalog_wins() -> None:
    assert "service_id" not in resolve_service({"service": "cart"}, CATALOG, MAPPINGS)
    assert "service_id" not in resolve_service(
        {"service": "cart", "namespace": "other"}, CATALOG, MAPPINGS
    )
    # A label match on the catalog is used before any mapping.
    ref = resolve_service(
        {"service": "checkout", "env": "prod", "job": "payment-api"},
        CATALOG,
        [
            ({"job": "payment-api"}, "svc_CHECKOUT", "checkout", "shop"),
        ],
    )
    assert ref["service_id"] == "svc_PROD"


def test_the_first_matching_mapping_wins() -> None:
    labels = {"service": "cart", "namespace": "shop", "job": "payment-api"}
    assert resolve_service(labels, CATALOG, MAPPINGS)["service_id"] == "svc_CHECKOUT"
    assert resolve_service(labels, CATALOG, MAPPINGS[::-1])["service_id"] == "svc_PROD"
