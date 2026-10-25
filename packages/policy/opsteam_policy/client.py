"""OPA client: every failure to get a clear answer is a deny."""

import asyncio
import hashlib
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr, ValidationError

UNAVAILABLE = "policy_unavailable"
_PATH = re.compile(r"[a-z_]+(/[a-z_]+)*")


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True)
    allow: bool
    reasons: tuple[str, ...]
    obligations: dict[str, StrictInt | StrictStr | StrictBool]
    policy_version: str


class _Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allow: StrictBool
    reasons: list[StrictStr]
    obligations: dict[str, StrictInt | StrictStr | StrictBool]


class _Answer(BaseModel):
    result: _Result


def bundle_version(directory: Path) -> str:
    """Hash of the policy files (tests excluded), recorded with every decision."""
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*.rego")):
        if "tests" in path.relative_to(directory).parts:
            continue
        digest.update(path.relative_to(directory).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


class PolicyClient:
    """Asks OPA's data API for a decision document; `timeout` bounds the whole call."""

    def __init__(
        self,
        base_url: str,
        policy_version: str,
        *,
        timeout: float = 0.2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # shortcut: policy_version hashes the local files and trusts OPA loaded the same ones;
        # switch to bundle revisions when signed bundles ship to runners.
        self.policy_version = policy_version
        self.timeout = timeout
        self._http = httpx.AsyncClient(
            base_url=base_url, transport=transport, timeout=timeout, trust_env=False
        )

    async def decide(self, path: str, input: Mapping[str, Any]) -> Decision:
        if not _PATH.fullmatch(path):
            raise ValueError(f"bad policy path {path!r}")
        try:
            async with asyncio.timeout(self.timeout):
                response = await self._http.post(f"/v1/data/{path}", json={"input": input})
            response.raise_for_status()
            result = _Answer.model_validate_json(response.content).result
        except (TimeoutError, httpx.HTTPError, ValidationError):
            return self._deny()
        return Decision(**result.model_dump(), policy_version=self.policy_version)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _deny(self) -> Decision:
        return Decision(
            allow=False, reasons=(UNAVAILABLE,), obligations={}, policy_version=self.policy_version
        )
