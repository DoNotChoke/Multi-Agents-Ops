"""Which catalog service an event is about, from its labels; unmatched events keep the raw names."""

from collections.abc import Mapping, Sequence

NAME_LABELS = ("service.name", "service")
# namespace last: the Prometheus import files services under their namespace.
ENVIRONMENT_LABELS = ("deployment.environment.name", "environment", "env", "namespace")

# Tenant rule: (label matchers, service_id, service name, environment), first match wins.
ServiceMapping = tuple[Mapping[str, str], str, str, str]
# The tenant's mappings in the order they are tried: higher priority, the more specific, the older.
MAPPINGS_SQL = """
    select m.matchers, s.service_id, s.name, s.environment, m.mapping_id, m.priority, m.created_at
    from service_mappings m join services s on s.service_id = m.service_id
    order by m.priority desc, (select count(*) from jsonb_object_keys(m.matchers)) desc,
             m.created_at
"""


def _first(labels: Mapping[str, str], keys: tuple[str, ...]) -> str | None:
    return next((labels[k] for k in keys if labels.get(k)), None)


def resolve_service(
    labels: Mapping[str, str],
    catalog: Mapping[tuple[str, str], str],
    mappings: Sequence[ServiceMapping] = (),
) -> dict[str, str]:
    """Return the event's service_ref: by catalog labels, then tenant mappings, else raw names."""
    # shortcut: no k8s_workload step; add it once the catalog can hold workloads.
    name, environment = _first(labels, NAME_LABELS), _first(labels, ENVIRONMENT_LABELS)
    ref = {k: v for k, v in (("name", name), ("environment", environment)) if v is not None}
    if name is not None and environment is not None and (name, environment) in catalog:
        return ref | {"service_id": catalog[(name, environment)]}
    for matchers, service_id, mapped_name, mapped_environment in mappings:
        if all(labels.get(k) == v for k, v in matchers.items()):
            return {
                "service_id": service_id,
                "name": mapped_name,
                "environment": mapped_environment,
            }
    return ref
