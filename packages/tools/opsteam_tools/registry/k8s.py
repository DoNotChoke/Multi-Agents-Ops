"""Kubernetes tools: allowlisted views of workloads, recent events and a deployment's rollouts."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from opsteam_tools.registry.spec import Limits, ToolSpec

Kind = Literal["pods", "deployments", "replicasets", "nodes", "hpa", "services"]
Namespace = Annotated[str, Field(pattern=r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")]
Name = Annotated[str, Field(pattern=r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")]
# Equality-based selectors only (app=x,tier!=db): no set syntax, spaces or quotes.
LabelSelector = Annotated[str, Field(max_length=256, pattern=r"^[A-Za-z0-9_.\-/=!,]+$")]
Reason = Annotated[str, Field(pattern=r"^[A-Za-z]{1,64}$")]
MAX_OBJECTS, MAX_REVISIONS = 200, 10
_STRICT = ConfigDict(extra="forbid")

PATHS: dict[str, str] = {
    "pods": "/api/v1/namespaces/{namespace}/pods",
    "deployments": "/apis/apps/v1/namespaces/{namespace}/deployments",
    "replicasets": "/apis/apps/v1/namespaces/{namespace}/replicasets",
    "nodes": "/api/v1/nodes",
    "hpa": "/apis/autoscaling/v2/namespaces/{namespace}/horizontalpodautoscalers",
    "services": "/api/v1/namespaces/{namespace}/services",
    "events": "/api/v1/namespaces/{namespace}/events",
}


class K8sResourcesArgs(BaseModel):
    """`namespace` must be the requested resource's environment; nodes ignore it for the listing."""

    model_config = _STRICT
    kind: Kind
    namespace: Namespace
    name: Name | None = None
    label_selector: LabelSelector | None = None


class K8sEventsArgs(BaseModel):
    model_config = _STRICT
    namespace: Namespace
    involved_object: Name | None = None
    window_seconds: int = Field(default=3600, ge=60, le=6 * 3600)
    reasons: list[Reason] | None = Field(default=None, max_length=10)


class K8sRolloutsArgs(BaseModel):
    model_config = _STRICT
    namespace: Namespace
    name: Name


_INFRA = frozenset({"infrastructure"})

K8S_GET_RESOURCES = ToolSpec(
    name="k8s.get_resources",
    description=(
        "Pods, deployments, replicasets, nodes, HPAs or services in the service's namespace: "
        "status, reasons, restarts, images, resources and conditions only."
    ),
    args=K8sResourcesArgs,
    integration_type="kubernetes",
    allowed_agents=_INFRA,
    timeout_ms=10_000,
    limits=Limits(max_bytes=4 << 20, max_series=MAX_OBJECTS),
)

K8S_EVENTS = ToolSpec(
    name="k8s.events",
    description=(
        "Kubernetes events in the service's namespace over the last window_seconds, newest first, "
        "optionally for one object or some reasons (e.g. BackOff, OOMKilling, FailedScheduling)."
    ),
    args=K8sEventsArgs,
    integration_type="kubernetes",
    allowed_agents=_INFRA,
    timeout_ms=10_000,
    limits=Limits(max_bytes=4 << 20, max_series=MAX_OBJECTS, max_window_s=6 * 3600),
)

K8S_ROLLOUTS = ToolSpec(
    name="k8s.rollouts",
    description=(
        "A deployment's current state and its last 10 revisions (replicaset, images, created_at), "
        "to tell whether a release preceded the incident."
    ),
    args=K8sRolloutsArgs,
    integration_type="kubernetes",
    allowed_agents=_INFRA,
    timeout_ms=10_000,
    limits=Limits(max_bytes=4 << 20, max_series=MAX_REVISIONS),
)
