"""Allowlisted views of Kubernetes objects: state, reasons, restarts, images and times only."""

# Fields are copied in, never filtered out: env, volumes, annotations or new API fields never leak.

from collections.abc import Callable
from typing import Any

MAX_TEXT, MAX_LABELS, MAX_VALUE = 300, 20, 256
REVISION = "deployment.kubernetes.io/revision"
Obj = dict[str, Any]


def _d(value: Any) -> Obj:
    return value if isinstance(value, dict) else {}


def _l(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _text(value: Any, limit: int = MAX_TEXT) -> str | None:
    return None if value is None else str(value)[:limit]


def _meta(obj: Obj) -> Obj:
    meta = _d(obj.get("metadata"))
    return {
        "name": _text(meta.get("name")),
        "namespace": _text(meta.get("namespace")),
        "created_at": _text(meta.get("creationTimestamp")),
    }


def _labels(obj: Obj) -> dict[str, str]:
    labels = _d(_d(obj.get("metadata")).get("labels"))
    return {str(k)[:MAX_VALUE]: str(v)[:MAX_VALUE] for k, v in list(labels.items())[:MAX_LABELS]}


def _revision(obj: Obj) -> str | None:
    return _text(_d(_d(obj.get("metadata")).get("annotations")).get(REVISION), 20)


def _conditions(status: Obj) -> list[Obj]:
    return [
        {
            "type": _text(c.get("type")),
            "status": _text(c.get("status")),
            "reason": _text(c.get("reason")),
            "message": _text(c.get("message")),
            "last_transition_at": _text(c.get("lastTransitionTime")),
        }
        for c in map(_d, _l(status.get("conditions")))
    ]


def _resources(container: Obj) -> Obj:
    resources = _d(container.get("resources"))
    return {
        kind: {k: _text(_d(resources.get(kind)).get(k), 32) for k in ("cpu", "memory")}
        for kind in ("requests", "limits")
    }


def _containers(pod_spec: Obj) -> list[Obj]:
    return [
        {"name": _text(c.get("name")), "image": _text(c.get("image")), "resources": _resources(c)}
        for c in map(_d, _l(pod_spec.get("containers")))
    ]


def _state(state: Any) -> Obj | None:
    for phase, detail in _d(state).items():
        if phase in ("waiting", "running", "terminated"):
            d = _d(detail)
            return {
                "state": phase,
                "reason": _text(d.get("reason")),
                "message": _text(d.get("message")),
                "exit_code": d.get("exitCode") if isinstance(d.get("exitCode"), int) else None,
                "started_at": _text(d.get("startedAt")),
                "finished_at": _text(d.get("finishedAt")),
            }
    return None


def pod(obj: Obj) -> Obj:
    spec, status = _d(obj.get("spec")), _d(obj.get("status"))
    statuses = {_text(s.get("name")): s for s in map(_d, _l(status.get("containerStatuses")))}
    containers = []
    for c in _containers(spec):
        s = statuses.get(c["name"], {})
        containers.append(
            c
            | {
                "ready": s.get("ready") is True,
                "restart_count": s.get("restartCount", 0)
                if isinstance(s.get("restartCount"), int)
                else 0,
                "state": _state(s.get("state")),
                "last_state": _state(s.get("lastState")),
            }
        )
    return _meta(obj) | {
        "labels": _labels(obj),
        "phase": _text(status.get("phase")),
        "reason": _text(status.get("reason")),
        "node": _text(spec.get("nodeName")),
        "started_at": _text(status.get("startTime")),
        "conditions": _conditions(status),
        "containers": containers,
    }


def _count(status: Obj, key: str) -> int:
    value = status.get(key)
    return value if isinstance(value, int) else 0


def deployment(obj: Obj) -> Obj:
    spec, status = _d(obj.get("spec")), _d(obj.get("status"))
    template = _d(_d(spec.get("template")).get("spec"))
    return _meta(obj) | {
        "labels": _labels(obj),
        "revision": _revision(obj),
        "paused": spec.get("paused") is True,
        "strategy": _text(_d(spec.get("strategy")).get("type")),
        "replicas": {
            "desired": _count(spec, "replicas"),
            "ready": _count(status, "readyReplicas"),
            "updated": _count(status, "updatedReplicas"),
            "available": _count(status, "availableReplicas"),
            "unavailable": _count(status, "unavailableReplicas"),
        },
        "generation": _count(_d(obj.get("metadata")), "generation"),
        "observed_generation": _count(status, "observedGeneration"),
        "conditions": _conditions(status),
        "containers": _containers(template),
    }


def replicaset(obj: Obj) -> Obj:
    spec, status = _d(obj.get("spec")), _d(obj.get("status"))
    owners = _l(_d(obj.get("metadata")).get("ownerReferences"))
    owner = next((_d(o) for o in owners if _d(o).get("kind") == "Deployment"), {})
    return _meta(obj) | {
        "revision": _revision(obj),
        "deployment": _text(owner.get("name")),
        "replicas": {
            "desired": _count(spec, "replicas"),
            "ready": _count(status, "readyReplicas"),
            "available": _count(status, "availableReplicas"),
        },
        "containers": _containers(_d(_d(spec.get("template")).get("spec"))),
    }


def node(obj: Obj) -> Obj:
    spec, status = _d(obj.get("spec")), _d(obj.get("status"))
    allocatable = _d(status.get("allocatable"))
    return _meta(obj) | {
        "unschedulable": spec.get("unschedulable") is True,
        "taints": [
            {"key": _text(t.get("key")), "effect": _text(t.get("effect"))}
            for t in map(_d, _l(spec.get("taints")))
        ],
        "allocatable": {k: _text(allocatable.get(k), 32) for k in ("cpu", "memory", "pods")},
        "kubelet_version": _text(_d(status.get("nodeInfo")).get("kubeletVersion"), 40),
        "conditions": _conditions(status),
    }


def hpa(obj: Obj) -> Obj:
    spec, status = _d(obj.get("spec")), _d(obj.get("status"))
    target = _d(spec.get("scaleTargetRef"))
    return _meta(obj) | {
        "target": {"kind": _text(target.get("kind")), "name": _text(target.get("name"))},
        "min_replicas": _count(spec, "minReplicas") or 1,
        "max_replicas": _count(spec, "maxReplicas"),
        "current_replicas": _count(status, "currentReplicas"),
        "desired_replicas": _count(status, "desiredReplicas"),
        "conditions": _conditions(status),
    }


def service(obj: Obj) -> Obj:
    spec = _d(obj.get("spec"))
    return _meta(obj) | {
        "type": _text(spec.get("type")),
        "selector": {
            str(k)[:MAX_VALUE]: str(v)[:MAX_VALUE]
            for k, v in list(_d(spec.get("selector")).items())[:MAX_LABELS]
        },
        "ports": [
            {
                "name": _text(p.get("name")),
                "port": p.get("port") if isinstance(p.get("port"), int) else None,
                "target_port": _text(p.get("targetPort"), 32),
                "protocol": _text(p.get("protocol"), 8),
            }
            for p in map(_d, _l(spec.get("ports")))
        ],
    }


def event(obj: Obj) -> Obj:
    involved = _d(obj.get("involvedObject"))
    series = _d(obj.get("series"))
    last = obj.get("lastTimestamp") or series.get("lastObservedTime") or obj.get("eventTime")
    return {
        "type": _text(obj.get("type")),
        "reason": _text(obj.get("reason")),
        "message": _text(obj.get("message"), 500),
        "object": {"kind": _text(involved.get("kind")), "name": _text(involved.get("name"))},
        "count": _count(obj, "count") or _count(series, "count") or 1,
        "first_at": _text(obj.get("firstTimestamp") or obj.get("eventTime")),
        "last_at": _text(last),
    }


VIEWS: dict[str, Callable[[Obj], Obj]] = {
    "pods": pod,
    "deployments": deployment,
    "replicasets": replicaset,
    "nodes": node,
    "hpa": hpa,
    "services": service,
}
