"""Static checks on the kind lab manifests; they run without a cluster."""

from pathlib import Path
from typing import Any

import yaml

LAB = Path(__file__).resolve().parents[2] / "deploy" / "lab"
EXPECTED_ALERTS = {
    "HighLatencyP95",
    "HighErrorRate",
    "PodRestarting",
    "PodCrashLooping",
    "ContainerOOMKilled",
}


def _load(rel: str) -> Any:
    return yaml.safe_load((LAB / rel).read_text(encoding="utf-8"))


def _load_all(rel: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all((LAB / rel).read_text(encoding="utf-8")) if d]


def _node_ports() -> set[int]:
    ports = {
        _load("observability/prometheus-values.yaml")["server"]["service"]["nodePort"],
        _load("observability/prometheus-values.yaml")["alertmanager"]["service"]["nodePort"],
        _load("observability/loki-values.yaml")["gateway"]["service"]["nodePort"],
    }
    for doc in _load_all("base/shop.yaml"):
        if doc["kind"] == "Service" and doc["spec"].get("type") == "NodePort":
            ports |= {p["nodePort"] for p in doc["spec"]["ports"]}
    return ports


def test_kind_publishes_only_on_localhost() -> None:
    mappings = _load("kind.yaml")["nodes"][0]["extraPortMappings"]
    assert mappings and all(m["listenAddress"] == "127.0.0.1" for m in mappings)


def test_every_node_port_is_mapped_by_kind() -> None:
    mapped = {m["containerPort"] for m in _load("kind.yaml")["nodes"][0]["extraPortMappings"]}
    assert _node_ports() == mapped


def test_alert_rules_cover_the_lab_faults() -> None:
    groups = _load("observability/prometheus-values.yaml")["serverFiles"]["alerting_rules.yml"]
    names = {r["alert"] for g in groups["groups"] for r in g["rules"]}
    assert names == EXPECTED_ALERTS | {"Watchdog"}


def test_the_watchdog_always_fires_and_repeats_every_minute() -> None:
    values = _load("observability/prometheus-values.yaml")
    groups = values["serverFiles"]["alerting_rules.yml"]["groups"]
    [rule] = [r for g in groups for r in g["rules"] if r["alert"] == "Watchdog"]
    assert rule["expr"] == "vector(1)"
    [route] = values["alertmanager"]["config"]["route"]["routes"]
    assert route["matchers"] == ['alertname="Watchdog"']
    assert (route["receiver"], route["repeat_interval"]) == ("opsteam", "1m")


def test_shop_images_are_pinned_or_local() -> None:
    for rel in ("base/shop.yaml", "base/postgres.yaml"):
        for doc in _load_all(rel):
            if doc["kind"] != "Deployment":
                continue
            for c in doc["spec"]["template"]["spec"]["containers"]:
                local = c["image"].startswith("opsteam-lab/") and c["imagePullPolicy"] == "Never"
                assert local or c["image"].split(":")[-1][0].isdigit(), c["image"]
