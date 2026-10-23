"""Entry point for `make eval`: validate scenarios, optionally run them on the lab, report."""

import argparse
import logging
import sys
from typing import Any

from evals.harness.lab import passed, run_scenario
from evals.harness.report import write_report
from evals.harness.scenario import ScenarioError, load_scenarios

log = logging.getLogger("eval")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="make eval")
    parser.add_argument("--lab", action="store_true", help="apply each fault on the kind lab")
    parser.add_argument("--record", action="store_true", help="with --lab, save replay telemetry")
    parser.add_argument("ids", nargs="*", help="scenario ids (default: all)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        scenarios = load_scenarios()
    except ScenarioError as exc:
        for line in exc.errors:
            log.error(line)
        return 1
    unknown = set(args.ids) - {s["id"] for s in scenarios}
    if unknown:
        log.error("unknown scenarios: %s", sorted(unknown))
        return 2
    scenarios = [s for s in scenarios if not args.ids or s["id"] in args.ids]

    detections: dict[str, dict[str, Any]] = {}
    if args.lab:
        detections = {s["id"]: run_scenario(s, args.record) for s in scenarios}
    out = write_report(scenarios, detections=detections)
    log.info("%d scenarios, report in %s", len(scenarios), out)
    if args.lab:
        for sid, result in detections.items():
            log.info("%-36s %s", sid, "PASS" if passed(result) else f"FAIL {result}")
        return 0 if all(passed(r) for r in detections.values()) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
