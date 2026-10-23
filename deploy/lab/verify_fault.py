"""Turn each lab fault on, check its alerts reach Alertmanager in time, then heal it."""

import json
import logging
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ALERTS_URL = "http://127.0.0.1:30093/api/v2/alerts?active=true&silenced=false&inhibited=false"
LAB_SH = Path(__file__).resolve().parent / "lab.sh"
POLL_S = 10
# Restart-based alerts look back 10 minutes, so quiet can take that long after a heal.
QUIET_TIMEOUT_S = 15 * 60

log = logging.getLogger("verify_fault")


@dataclass(frozen=True)
class Want:
    """An alert that must fire, matched on the `service` label or a pod name prefix."""

    alertname: str
    target: str

    def matches(self, labels: dict[str, str]) -> bool:
        return labels.get("alertname") == self.alertname and (
            labels.get("service") == self.target
            or labels.get("pod", "").startswith(self.target + "-")
        )


@dataclass(frozen=True)
class Fault:
    expect: tuple[Want, ...]
    timeout_s: int


# Order: quick faults first, restart-based ones (slowest to go quiet) last.
FAULTS = {
    "benign_noise": Fault((), 5 * 60),
    "dependency_slow": Fault(
        (Want("HighLatencyP95", "checkout"), Want("HighLatencyP95", "frontend")), 8 * 60
    ),
    "bad_release_db_pool": Fault((Want("HighLatencyP95", "checkout"),), 8 * 60),
    "crashloop_config": Fault(
        (Want("PodCrashLooping", "checkout"), Want("HighErrorRate", "frontend")), 8 * 60
    ),
    "oom_killed_service": Fault(
        (Want("ContainerOOMKilled", "payment"), Want("PodRestarting", "payment")), 8 * 60
    ),
}


def shop_alerts(raw: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return the label sets of active alerts in the shop namespace."""
    return [
        a["labels"]
        for a in raw
        if a["status"]["state"] == "active" and a["labels"].get("namespace") == "shop"
    ]


def missing(wants: tuple[Want, ...], firing: list[dict[str, str]]) -> list[Want]:
    """Return the wanted alerts that are not firing yet."""
    return [w for w in wants if not any(w.matches(labels) for labels in firing)]


def _fetch() -> list[dict[str, str]]:
    with urllib.request.urlopen(ALERTS_URL, timeout=10) as resp:
        return shop_alerts(json.load(resp))


def _lab(*args: str) -> None:
    # which() follows PATH; a bare "bash" on Windows would pick System32's WSL launcher first.
    bash = shutil.which("bash")
    if bash is None:
        raise RuntimeError("bash not found on PATH")
    subprocess.run([bash, str(LAB_SH), *args], check=True)  # noqa: S603 -- fixed args


def _wait_quiet() -> bool:
    deadline = time.monotonic() + QUIET_TIMEOUT_S
    while time.monotonic() < deadline:
        if not _fetch():
            return True
        time.sleep(POLL_S)
    log.error("alerts still active after %ss: %s", QUIET_TIMEOUT_S, _names(_fetch()))
    return False


def _names(alerts: list[dict[str, str]]) -> list[str]:
    return sorted({f"{a['alertname']}/{a.get('service') or a.get('pod', '?')}" for a in alerts})


def verify(name: str) -> bool:
    """Run one fault end to end and report whether it behaved as expected."""
    fault = FAULTS[name]
    if not _wait_quiet():
        return False
    log.info("[%s] applying fault", name)
    _lab("fault", name)
    start = time.monotonic()
    first_seen: dict[Want, float] = {}
    ok = False
    try:
        while time.monotonic() - start < fault.timeout_s:
            firing = _fetch()
            for want in fault.expect:
                if want not in first_seen and not missing((want,), firing):
                    first_seen[want] = time.monotonic() - start
                    log.info(
                        "[%s] %s/%s firing after %.0fs",
                        name,
                        want.alertname,
                        want.target,
                        first_seen[want],
                    )
            if not fault.expect and firing:
                log.error("[%s] unexpected alerts: %s", name, _names(firing))
                break
            if fault.expect and len(first_seen) == len(fault.expect):
                ok = True
                break
            time.sleep(POLL_S)
        else:
            ok = not fault.expect
            if fault.expect:
                log.error("[%s] never fired: %s", name, missing(fault.expect, _fetch()))
    finally:
        log.info("[%s] healing", name)
        _lab("heal")
    quiet = _wait_quiet()
    log.info("[%s] %s (quiet after heal: %s)", name, "PASS" if ok else "FAIL", quiet)
    return ok and quiet


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    names = argv or list(FAULTS)
    unknown = [n for n in names if n not in FAULTS]
    if unknown:
        log.error("unknown faults %s; known: %s", unknown, list(FAULTS))
        return 2
    results = {n: verify(n) for n in names}
    for n, passed in results.items():
        log.info("%-22s %s", n, "PASS" if passed else "FAIL")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
