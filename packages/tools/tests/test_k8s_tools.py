import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from opsteam_contracts.tool_request import ToolRequest
from opsteam_tools import k8s_view
from opsteam_tools.backends import DirectBackend, IntegrationTarget
from opsteam_tools.connectors.http import ConnectorError
from opsteam_tools.executors import Denied, check_request
from opsteam_tools.registry import TOOLS
from opsteam_tools.registry.k8s import K8sEventsArgs, K8sResourcesArgs, K8sRolloutsArgs
from pydantic import SecretStr, ValidationError

from tests.certs import cluster_ca

FAKE_SECRET = "dev-only-db-password-k8s"
LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


def _container() -> dict[str, Any]:
    return {
        "name": "app",
        "image": "opsteam-lab/demo-app:v2",
        "command": ["/app", f"--db-password={FAKE_SECRET}"],
        "env": [{"name": "DB_PASSWORD", "value": FAKE_SECRET}],
        "envFrom": [{"secretRef": {"name": "db"}}],
        "volumeMounts": [{"name": "creds", "mountPath": "/secrets"}],
        "resources": {"limits": {"memory": "64Mi", "nvidia.com/gpu": "1"}},
    }


def _metadata(name: str, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "namespace": "shop",
        "creationTimestamp": "2026-11-05T10:00:00Z",
        "labels": {"service": "checkout", "version": "v2"},
        "annotations": {LAST_APPLIED: json.dumps({"env": FAKE_SECRET})},
        "managedFields": [{"manager": "kubectl"}],
    } | extra


POD = {
    "metadata": _metadata("checkout-7f9c-abcde"),
    "spec": {
        "nodeName": "opsteam-lab-control-plane",
        "containers": [_container()],
        "volumes": [{"name": "creds", "secret": {"secretName": "db"}}],
        "serviceAccountName": "default",
    },
    "status": {
        "phase": "Running",
        "startTime": "2026-11-05T10:00:05Z",
        "conditions": [{"type": "Ready", "status": "False", "reason": "ContainersNotReady"}],
        "containerStatuses": [
            {
                "name": "app",
                "ready": False,
                "restartCount": 4,
                "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 40s"}},
                "lastState": {
                    "terminated": {
                        "reason": "OOMKilled",
                        "exitCode": 137,
                        "finishedAt": "2026-11-05T10:05:00Z",
                    }
                },
            }
        ],
    },
}

DEPLOYMENT = {
    "metadata": _metadata(
        "checkout",
        generation=3,
        annotations={"deployment.kubernetes.io/revision": "3", LAST_APPLIED: FAKE_SECRET},
    ),
    "spec": {
        "replicas": 2,
        "strategy": {"type": "RollingUpdate"},
        "template": {"spec": {"containers": [_container()]}},
    },
    "status": {
        "observedGeneration": 3,
        "readyReplicas": 1,
        "updatedReplicas": 2,
        "availableReplicas": 1,
        "unavailableReplicas": 1,
        "conditions": [
            {"type": "Progressing", "status": "True", "reason": "NewReplicaSetAvailable"}
        ],
    },
}


def test_pod_view_keeps_state_and_drops_env_volumes_and_annotations() -> None:
    view = k8s_view.pod(POD)
    assert FAKE_SECRET not in json.dumps(view)
    assert set(view) == {
        "name", "namespace", "created_at", "labels", "phase", "reason", "node", "started_at",
        "conditions", "containers",
    }  # fmt: skip
    (container,) = view["containers"]
    assert container == {
        "name": "app",
        "image": "opsteam-lab/demo-app:v2",
        "resources": {
            "requests": {"cpu": None, "memory": None},
            "limits": {"cpu": None, "memory": "64Mi"},
        },
        "ready": False,
        "restart_count": 4,
        "state": {
            "state": "waiting",
            "reason": "CrashLoopBackOff",
            "message": "back-off 40s",
            "exit_code": None,
            "started_at": None,
            "finished_at": None,
        },
        "last_state": {
            "state": "terminated",
            "reason": "OOMKilled",
            "message": None,
            "exit_code": 137,
            "started_at": None,
            "finished_at": "2026-11-05T10:05:00Z",
        },
    }
    assert view["labels"] == {"service": "checkout", "version": "v2"}


def test_deployment_view_keeps_the_rollout_state_only() -> None:
    view = k8s_view.deployment(DEPLOYMENT)
    assert FAKE_SECRET not in json.dumps(view)
    assert view["revision"] == "3"
    assert view["replicas"] == {
        "desired": 2, "ready": 1, "updated": 2, "available": 1, "unavailable": 1,
    }  # fmt: skip
    assert (view["generation"], view["observed_generation"]) == (3, 3)
    assert view["containers"][0]["image"] == "opsteam-lab/demo-app:v2"


def test_every_view_survives_garbage_and_never_copies_unknown_fields() -> None:
    garbage: dict[str, Any] = {
        "metadata": "not a dict",
        "spec": {"containers": [None, 3, {"env": [FAKE_SECRET]}], "ports": "x"},
        "status": {"conditions": [{"message": "m" * 1000}], "containerStatuses": None},
        "secret": FAKE_SECRET,
    }
    for name, view in k8s_view.VIEWS.items():
        out = json.dumps(view(garbage))
        assert FAKE_SECRET not in out, name
        assert "m" * 301 not in out, name
    assert FAKE_SECRET not in json.dumps(k8s_view.event(garbage))


def test_event_view_reads_both_event_shapes() -> None:
    legacy = {
        "type": "Warning",
        "reason": "BackOff",
        "message": "Back-off restarting failed container",
        "involvedObject": {"kind": "Pod", "name": "checkout-7f9c-abcde", "uid": "u"},
        "count": 7,
        "firstTimestamp": "2026-11-05T10:01:00Z",
        "lastTimestamp": "2026-11-05T10:06:00Z",
    }
    assert k8s_view.event(legacy) == {
        "type": "Warning",
        "reason": "BackOff",
        "message": "Back-off restarting failed container",
        "object": {"kind": "Pod", "name": "checkout-7f9c-abcde"},
        "count": 7,
        "first_at": "2026-11-05T10:01:00Z",
        "last_at": "2026-11-05T10:06:00Z",
    }
    modern = {"reason": "Pulled", "eventTime": "2026-11-05T10:00:01.000000Z"}
    assert k8s_view.event(modern)["last_at"] == "2026-11-05T10:00:01.000000Z"
    assert k8s_view.event(modern)["count"] == 1


NOW = datetime(2026, 11, 5, 10, 10, tzinfo=UTC)
TARGET = IntegrationTarget(
    "https://93.184.216.34:6443", SecretStr("dev-only-k8s-reader-token"), {}, ca_cert=cluster_ca()
)


class Clock:
    def now(self) -> datetime:
        return NOW


def _backend(routes: dict[str, Any], seen: list[httpx.Request]) -> DirectBackend:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=routes[request.url.path])

    backend = DirectBackend(transport=httpx.MockTransport(handler), clock=Clock())
    return backend


async def test_get_resources_returns_only_allowlisted_fields() -> None:
    seen: list[httpx.Request] = []
    routes = {"/api/v1/namespaces/shop/pods": {"items": [POD], "metadata": {"continue": "x"}}}
    args = K8sResourcesArgs(kind="pods", namespace="shop", label_selector="service=checkout")
    fetched = await _backend(routes, seen).run(TOOLS["k8s.get_resources"], args, TARGET, set())
    assert FAKE_SECRET not in json.dumps(fetched.data) and fetched.raw == fetched.data
    assert fetched.data["truncated"] is True
    assert fetched.data["items"][0]["containers"][0]["restart_count"] == 4
    (request,) = seen
    assert dict(request.url.params) == {"limit": "200", "labelSelector": "service=checkout"}
    assert request.headers["authorization"] == "Bearer dev-only-k8s-reader-token"


async def test_a_name_becomes_a_field_selector_and_nodes_are_cluster_wide() -> None:
    seen: list[httpx.Request] = []
    routes = {"/api/v1/nodes": {"items": []}, "/apis/apps/v1/namespaces/shop/deployments": {
        "items": [DEPLOYMENT]
    }}  # fmt: skip
    backend = _backend(routes, seen)
    await backend.run(
        TOOLS["k8s.get_resources"], K8sResourcesArgs(kind="nodes", namespace="shop"), TARGET, set()
    )
    args = K8sResourcesArgs(kind="deployments", namespace="shop", name="checkout")
    fetched = await backend.run(TOOLS["k8s.get_resources"], args, TARGET, set())
    assert seen[1].url.params["fieldSelector"] == "metadata.name=checkout"
    assert fetched.data["items"][0]["revision"] == "3"


def _event(reason: str, last: str) -> dict[str, Any]:
    return {
        "reason": reason,
        "type": "Warning",
        "message": f"{reason} happened",
        "involvedObject": {"kind": "Pod", "name": "checkout-1"},
        "lastTimestamp": last,
    }


async def test_events_keep_the_window_and_reasons_newest_first() -> None:
    seen: list[httpx.Request] = []
    events = [
        _event("BackOff", "2026-11-05T10:05:00Z"),
        _event("OOMKilling", "2026-11-05T10:08:00Z"),
        _event("BackOff", "2026-11-05T08:00:00Z"),  # outside the window
        _event("Pulled", "2026-11-05T10:09:00Z"),
    ]
    routes = {"/api/v1/namespaces/shop/events": {"items": events}}
    args = K8sEventsArgs(
        namespace="shop",
        involved_object="checkout-1",
        window_seconds=1800,
        reasons=["BackOff", "OOMKilling"],
    )
    fetched = await _backend(routes, seen).run(TOOLS["k8s.events"], args, TARGET, set())
    assert [e["reason"] for e in fetched.data["items"]] == ["OOMKilling", "BackOff"]
    assert seen[0].url.params["fieldSelector"] == "involvedObject.name=checkout-1"


def _replicaset(name: str, revision: str, owner: str = "checkout") -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "annotations": {"deployment.kubernetes.io/revision": revision},
            "ownerReferences": [{"kind": "Deployment", "name": owner}],
        },
        "spec": {"template": {"spec": {"containers": [{"name": "app", "image": f"app:{name}"}]}}},
    }


async def test_rollouts_list_owned_revisions_newest_first() -> None:
    seen: list[httpx.Request] = []
    deployment = DEPLOYMENT | {
        "spec": DEPLOYMENT["spec"] | {"selector": {"matchLabels": {"service": "checkout"}}}
    }
    sets = [_replicaset(f"rs{i}", str(i)) for i in range(1, 13)]
    sets.append(_replicaset("other", "99", owner="checkout-canary"))
    routes = {
        "/apis/apps/v1/namespaces/shop/deployments": {"items": [deployment]},
        "/apis/apps/v1/namespaces/shop/replicasets": {"items": sets},
    }
    args = K8sRolloutsArgs(namespace="shop", name="checkout")
    fetched = await _backend(routes, seen).run(TOOLS["k8s.rollouts"], args, TARGET, set())
    assert [r["revision"] for r in fetched.data["revisions"]] == [str(i) for i in range(12, 2, -1)]
    assert fetched.data["truncated"] is True
    assert seen[1].url.params["labelSelector"] == "service=checkout"
    assert FAKE_SECRET not in json.dumps(fetched.data)
    missing = await _backend({"/apis/apps/v1/namespaces/shop/deployments": {"items": []}}, []).run(
        TOOLS["k8s.rollouts"], args, TARGET, set()
    )
    assert missing.data == {"deployment": None, "revisions": [], "truncated": False}


async def test_no_token_or_ca_fails_before_any_call() -> None:
    seen: list[httpx.Request] = []
    target = IntegrationTarget(TARGET.endpoint, None, {}, ca_cert=TARGET.ca_cert)
    with pytest.raises(ConnectorError):
        await _backend({}, seen).run(
            TOOLS["k8s.events"], K8sEventsArgs(namespace="shop"), target, set()
        )
    assert seen == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"kind": "secrets", "namespace": "shop"},
        {"kind": "configmaps", "namespace": "shop"},
        {"kind": "pods", "namespace": "shop", "name": "../secrets"},
        {"kind": "pods", "namespace": "shop", "label_selector": "app in (a,b)"},
        {"kind": "pods", "namespace": "Shop!"},
        {"kind": "pods", "namespace": "shop", "field_selector": "spec.nodeName=x"},
    ],
)
def test_resource_arguments_are_refused(arguments: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        K8sResourcesArgs.model_validate(arguments)


def _request(tool: str, arguments: dict[str, Any]) -> ToolRequest:
    return ToolRequest.model_validate(
        {
            "tool": tool,
            "integration_id": "int_01JDEVSEED0000000000000000",
            "resource": {"service": "checkout", "environment": "shop"},
            "arguments": arguments,
        }
    )


def test_namespace_must_be_the_catalog_environment_and_agent_infrastructure() -> None:
    ok = _request("k8s.get_resources", {"kind": "pods", "namespace": "shop"})
    assert check_request(ok, "infrastructure")[0].integration_type == "kubernetes"
    for agent in ("metrics", "logs"):
        with pytest.raises(Denied) as caught:
            check_request(ok, agent)
        assert caught.value.reason == "tool.not_allowed"
    elsewhere = _request("k8s.events", {"namespace": "kube-system"})
    with pytest.raises(Denied, match="namespace"):
        check_request(elsewhere, "infrastructure")
