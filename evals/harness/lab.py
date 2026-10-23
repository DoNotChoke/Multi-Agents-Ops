"""LAB=1 runner: apply a scenario's fault, time its alerts, optionally record, then heal."""

import json
import logging
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from evals.harness import replay
from evals.harness.scenario import REPO

ALERTS_URL = "http://127.0.0.1:30093/api/v2/alerts?active=true&silenced=false&inhibited=false"
LAB_SH = REPO / "deploy" / "lab" / "lab.sh"
POLL_S = 10
# Restart-based alerts look back 10 minutes, so quiet can take that long after a heal.
QUIET_TIMEOUT_S = 15 * 60

log = logging.getLogger("eval.lab")


@dataclass(frozen=True)
class Want:
    """An expected alert, matched on the `service` label or a pod name prefix."""

    alertname: str
    service: str

    def matches(self, labels: dict[str, str]) -> bool:
        return labels.get("alertname") == self.alertname and (
            labels.get("service") == self.service
            or labels.get("pod", "").startswith(self.service + "-")
        )

    def __str__(self) -> str:
        return f"{self.alertname}/{self.service}"


def wants(scenario: dict[str, Any]) -> tuple[Want, ...]:
    """Return the alerts a scenario expects."""
    alerts = scenario["expected_detection"]["alerts"]
    return tuple(Want(a["alertname"], a["service"]) for a in alerts)


def shop_alerts(raw: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return the label sets of active alerts in the shop namespace."""
    return [
        a["labels"]
        for a in raw
        if a["status"]["state"] == "active" and a["labels"].get("namespace") == "shop"
    ]


def missing(expected: tuple[Want, ...], firing: list[dict[str, str]]) -> list[Want]:
    """Return the expected alerts that are not firing yet."""
    return [w for w in expected if not any(w.matches(labels) for labels in firing)]


def _fetch() -> list[dict[str, str]]:
    with urllib.request.urlopen(ALERTS_URL, timeout=10) as resp:
        return shop_alerts(json.load(resp))


def _lab(*args: str) -> None:
    # which() follows PATH; a bare "bash" on Windows would pick System32's WSL launcher first.
    bash = shutil.which("bash")
    if bash is None:
        raise RuntimeError("bash not found on PATH")
    subprocess.run([bash, str(LAB_SH), *args], check=True)  # noqa: S603 -- fixed args


def _names(alerts: list[dict[str, str]]) -> list[str]:
    return sorted({f"{a['alertname']}/{a.get('service') or a.get('pod', '?')}" for a in alerts})


def _wait_quiet() -> bool:
    deadline = time.monotonic() + QUIET_TIMEOUT_S
    while time.monotonic() < deadline:
        if not _fetch():
            return True
        time.sleep(POLL_S)
    log.error("alerts still active after %ss: %s", QUIET_TIMEOUT_S, _names(_fetch()))
    return False


def _detect(sid: str, expected: tuple[Want, ...], window_s: int) -> dict[str, Any]:
    start = time.monotonic()
    seen: dict[str, int] = {}
    while time.monotonic() - start < window_s:
        firing = _fetch()
        if not expected and firing:
            log.error("[%s] unexpected alerts: %s", sid, _names(firing))
            return {"status": "noisy", "unexpected": _names(firing)}
        for want in expected:
            if str(want) not in seen and not missing((want,), firing):
                seen[str(want)] = round(time.monotonic() - start)
                log.info("[%s] %s firing after %ss", sid, want, seen[str(want)])
        if expected and len(seen) == len(expected):
            return {"status": "detected", "time_to_detect_seconds": seen}
        time.sleep(POLL_S)
    if not expected:
        return {"status": "silent"}
    log.error("[%s] not firing in %ss: %s", sid, window_s, missing(expected, _fetch()))
    return {"status": "missed", "time_to_detect_seconds": seen}


def run_scenario(scenario: dict[str, Any], record: bool) -> dict[str, Any]:
    """Run one scenario on the lab and return its detection result."""
    sid = scenario["id"]
    setup, expected = scenario["setup"], wants(scenario)
    if not _wait_quiet():
        return {"status": "not_quiet_before"}
    log.info("[%s] applying fault %s", sid, setup["lab_fault"])
    started_at = datetime.now(UTC)
    _lab("fault", setup["lab_fault"])
    try:
        window_s = scenario["expected_detection"].get(
            "max_time_to_detect_seconds", setup["fault_duration_seconds"]
        )
        result = _detect(sid, expected, window_s)
        if record:
            end_at = started_at + timedelta(seconds=setup["fault_duration_seconds"])
            time.sleep(max(0.0, (end_at - datetime.now(UTC)).total_seconds()))
            begin_at = started_at - timedelta(seconds=setup["warmup_seconds"])
            result["replay"] = replay.write(replay.record(scenario, begin_at, end_at)).as_posix()
            log.info("[%s] replay saved to %s", sid, result["replay"])
    finally:
        log.info("[%s] healing", sid)
        _lab("heal")
    result["quiet_after_heal"] = _wait_quiet()
    return result


def passed(result: dict[str, Any]) -> bool:
    """A scenario passes when detection matched expectations and the lab went quiet again."""
    return result["status"] in {"detected", "silent"} and bool(result.get("quiet_after_heal"))
