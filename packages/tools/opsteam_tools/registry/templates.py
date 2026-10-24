"""Named PromQL templates: the LLM picks a name and typed values, code builds the query."""

import re
from collections.abc import Mapping, Set
from string import Template

from opsteam_core.errors import OpsteamError

# Same alphabet as catalog names: no quotes, braces or whitespace can reach the query.
VALUE = re.compile(r"[A-Za-z0-9_.\-/:]{1,128}")
PARAMS = frozenset({"service", "environment"})
MAX_EXPRESSION = 2000

# Defaults follow OpenTelemetry metric names; integrations override them with metric_mapping.
# string.Template (${name}) because PromQL already uses {} for label matchers.
TEMPLATES: Mapping[str, str] = {
    "http_latency_p95": (
        "histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket"
        '{service="${service}", environment="${environment}"}[5m])))'
    ),
    "http_error_rate": (
        'sum(rate(http_server_request_duration_seconds_count{service="${service}", '
        'environment="${environment}", http_response_status_code=~"5.."}[5m])) / '
        'sum(rate(http_server_request_duration_seconds_count{service="${service}", '
        'environment="${environment}"}[5m]))'
    ),
    "request_rate": (
        'sum(rate(http_server_request_duration_seconds_count{service="${service}", '
        'environment="${environment}"}[5m]))'
    ),
    "pod_restarts": (
        'sum(increase(kube_pod_container_status_restarts_total{service="${service}", '
        'environment="${environment}"}[15m]))'
    ),
    "memory_working_set": (
        'sum(container_memory_working_set_bytes{service="${service}", '
        'environment="${environment}"})'
    ),
    "cpu_saturation": (
        'sum(rate(container_cpu_usage_seconds_total{service="${service}", '
        'environment="${environment}"}[5m]))'
        ' / sum(kube_pod_container_resource_limits{resource="cpu", service="${service}", '
        'environment="${environment}"})'
    ),
}


class TemplateError(OpsteamError):
    """A template name, parameter or mapping was refused."""

    code = "tool.invalid_arguments"


def validate_mapping(mapping: Mapping[str, str]) -> None:
    """Check an integration's metric_mapping: known names, only ${service}/${environment}."""
    for name, expression in mapping.items():
        if name not in TEMPLATES:
            raise TemplateError(f"unknown template {name!r}")
        if len(expression) > MAX_EXPRESSION:
            raise TemplateError(f"mapping for {name!r} is longer than {MAX_EXPRESSION} characters")
        template = Template(expression)
        if not template.is_valid():
            raise TemplateError(f"mapping for {name!r} has a stray '$'; write '$$' for a literal")
        if unknown := set(template.get_identifiers()) - PARAMS:
            raise TemplateError(f"mapping for {name!r} uses unknown placeholders {sorted(unknown)}")


def render(
    name: str,
    params: Mapping[str, str],
    *,
    mapping: Mapping[str, str],
    catalog: Set[tuple[str, str]],
) -> str:
    """Build the PromQL for `name`; values must be well-formed and the service in the catalog."""
    if name not in TEMPLATES:
        raise TemplateError(f"unknown template {name!r}")
    if set(params) != PARAMS:
        raise TemplateError(f"parameters must be exactly {sorted(PARAMS)}")
    for key, value in params.items():
        if not VALUE.fullmatch(value):
            raise TemplateError(f"parameter {key!r} has a disallowed value")
    if (params["service"], params["environment"]) not in catalog:
        raise TemplateError("service is not in the tenant's catalog")
    expression = mapping.get(name, TEMPLATES[name])
    validate_mapping({name: expression})
    return Template(expression).substitute(params)
