"""Write the per-run eval report as JSON and Markdown."""

import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.harness.scenario import REPO

REPORTS_DIR = REPO / "evals" / "reports"
# No investigator exists before the agent workflow lands, so every scenario is skipped.
SKIP_REASON = "no investigator yet (T-3.6)"


def _git(git: str, *args: str) -> str:
    proc = subprocess.run(  # noqa: S603 -- fixed git args
        [git, *args], cwd=REPO, capture_output=True, text=True, check=False
    )
    return proc.stdout.strip()


def _git_sha() -> str:
    """Return HEAD's sha, suffixed with -dirty when the tree has local changes."""
    git = shutil.which("git")
    if git is None:
        return "unknown"
    sha = _git(git, "rev-parse", "HEAD") or "unknown"
    return f"{sha}-dirty" if _git(git, "status", "--porcelain") else sha


def write_report(scenarios: list[dict[str, Any]], out_root: Path = REPORTS_DIR) -> Path:
    """Write report.json and report.md under a new timestamped directory and return it."""
    now = datetime.now(UTC)
    out = out_root / now.strftime("%Y%m%dT%H%M%S%fZ")
    out.mkdir(parents=True)
    results = [
        {
            "id": s["id"],
            "lab_fault": s["setup"]["lab_fault"],
            "status": "skipped",
            "reason": SKIP_REASON,
        }
        for s in scenarios
    ]
    report = {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "git_sha": _git_sha(),
        "scenario_count": len(results),
        "results": results,
    }
    (out / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    rows = "\n".join(
        f"| {r['id']} | {r['lab_fault']} | {r['status']} | {r['reason']} |" for r in results
    )
    (out / "report.md").write_text(
        f"# Eval report\n\n- generated_at: {report['generated_at']}\n"
        f"- git_sha: {report['git_sha']}\n- scenarios: {len(results)}\n\n"
        "| Scenario | Lab fault | Status | Reason |\n"
        f"| --- | --- | --- | --- |\n{rows}\n",
        encoding="utf-8",
    )
    return out
