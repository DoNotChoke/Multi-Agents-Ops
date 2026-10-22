import json
from pathlib import Path
from typing import Any

import pytest
import structlog
from opsteam_core.logging import configure_logging

SECRETS: list[dict[str, Any]] = [
    json.loads(line)
    for line in (
        Path(__file__).resolve().parents[3] / "tests/security/redaction_corpus/secrets.jsonl"
    )
    .read_text(encoding="utf-8")
    .splitlines()
    if line.strip()
]


class Opaque:
    def __repr__(self) -> str:
        return "Opaque(password=hunter2fake)"


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_log_lines_are_json_with_utc_timestamp(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    structlog.get_logger().info("started", component="api")
    (line,) = _lines(capsys)
    assert line["event"] == "started"
    assert line["level"] == "info"
    assert line["timestamp"].endswith("Z")


def test_level_filters(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING")
    structlog.get_logger().info("hidden")
    assert capsys.readouterr().out == ""


def test_secrets_never_reach_the_output(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    log = structlog.get_logger()
    log.info(
        "calling upstream",
        password="hunter2fake",
        headers={"Authorization": "Bearer abc123.fake-token", "X-Trace": "ok"},
        obj=Opaque(),
        samples=[c["text"] for c in SECRETS],
    )
    try:
        raise RuntimeError("connect failed: postgres://admin:Pa55fake@db:5432/app")
    except RuntimeError:
        log.exception("db error")
    out = capsys.readouterr().out
    for secret in ["hunter2fake", "abc123.fake-token", "Pa55fake", *(c["secret"] for c in SECRETS)]:
        assert secret not in out
    assert '"X-Trace": "ok"' in out
