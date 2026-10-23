"""Entry point for `make eval`: validate every scenario, run them and write a report."""

import logging
import sys

from evals.harness.report import write_report
from evals.harness.scenario import ScenarioError, load_scenarios

log = logging.getLogger("eval")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        scenarios = load_scenarios()
    except ScenarioError as exc:
        for line in exc.errors:
            log.error(line)
        return 1
    out = write_report(scenarios)
    log.info("%d scenarios, report in %s", len(scenarios), out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
