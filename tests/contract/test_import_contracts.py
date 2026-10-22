"""The import-linter contracts in pyproject.toml must reject real violations (docs/10 §3, INV-03).

Each case copies all opsteam_* packages to a temp dir, plants one forbidden import, and runs
lint-imports with that copy first on PYTHONPATH so it shadows the editable installs.
"""

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIRS = sorted(
    p
    for p in [*ROOT.glob("packages/*/opsteam_*"), *ROOT.glob("services/*/opsteam_*")]
    if p.is_dir()
)
LINT_IMPORTS = shutil.which("lint-imports", path=str(Path(sys.executable).parent))
AGENTS_TOOLS = "INV-03: opsteam_agents reaches tools"
AGENTS_INFRA = "INV-03: opsteam_agents imports no infra"


def _lint_imports(
    tmp_path: Path, module: str | None = None, code: str = ""
) -> subprocess.CompletedProcess[str]:
    src = tmp_path / "src"
    for pkg in PACKAGE_DIRS:
        shutil.copytree(pkg, src / pkg.name)
    if module:
        src.joinpath(*module.split(".")).with_suffix(".py").write_text(code)
    assert LINT_IMPORTS, "lint-imports not installed in the venv"
    return subprocess.run(
        [LINT_IMPORTS, "--no-cache"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(src)},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("pkg", [p.name for p in PACKAGE_DIRS])
def test_every_member_is_importable(pkg: str) -> None:
    importlib.import_module(pkg)


def test_clean_tree_passes(tmp_path: Path) -> None:
    result = _lint_imports(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("module", "code", "broken_contract"),
    [
        ("opsteam_agents.bad", "import opsteam_db\n", AGENTS_TOOLS),
        ("opsteam_agents.bad", "from opsteam_tools import connectors\n", AGENTS_TOOLS),
        ("opsteam_agents.bad", "import opsteam_tools.gateway\n", AGENTS_TOOLS),
        ("opsteam_agents.bad", "import subprocess\n", AGENTS_INFRA),
        ("opsteam_agents.bad", "import httpx\n", AGENTS_INFRA),
        ("opsteam_agents.bad", "import kubernetes\n", AGENTS_INFRA),
        ("opsteam_executor.bad", "import opsteam_llm\n", "Executor has no path to agents/LLM"),
        ("opsteam_api.bad", "import opsteam_worker\n", "Services do not import each other"),
        ("opsteam_core.bad", "import opsteam_db\n", "opsteam_core imports no other"),
        ("opsteam_llm.bad", "import opsteam_db\n", "opsteam_llm imports only core, contracts"),
        ("opsteam_tools.bad", "import opsteam_agents\n", "opsteam_tools imports only"),
    ],
)
def test_violation_is_rejected(
    tmp_path: Path, module: str, code: str, broken_contract: str
) -> None:
    result = _lint_imports(tmp_path, module, code)
    assert result.returncode != 0
    broken = [line for line in result.stdout.splitlines() if "BROKEN" in line]
    assert any(broken_contract in line for line in broken), result.stdout
