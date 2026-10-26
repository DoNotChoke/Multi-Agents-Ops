"""Which catalog service an event is about, from its labels; unmatched events keep the raw names."""

from collections.abc import Mapping

NAME_LABELS = ("service.name", "service")
# namespace last: the Prometheus import files services under their namespace.
ENVIRONMENT_LABELS = ("deployment.environment.name", "environment", "env", "namespace")


def _first(labels: Mapping[str, str], keys: tuple[str, ...]) -> str | None:
    return next((labels[k] for k in keys if labels.get(k)), None)


def resolve_service(
    labels: Mapping[str, str], catalog: Mapping[tuple[str, str], str]
) -> dict[str, str]:
    """Return the event's service_ref: the catalog id when (name, environment) is known."""
    # shortcut: no k8s_workload or tenant mapping rules; add them once the catalog can hold them.
    name, environment = _first(labels, NAME_LABELS), _first(labels, ENVIRONMENT_LABELS)
    ref = {k: v for k, v in (("name", name), ("environment", environment)) if v is not None}
    if name is not None and environment is not None and (name, environment) in catalog:
        ref["service_id"] = catalog[(name, environment)]
    return ref
