"""Static checks on the dev compose stack; they run without Docker."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE_DIR = Path(__file__).resolve().parents[2] / "deploy" / "compose"
COMPOSE = yaml.safe_load((COMPOSE_DIR / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICES: dict[str, dict[str, Any]] = COMPOSE["services"]
ENV_EXAMPLE = dict(
    line.split("=", 1)
    for line in (COMPOSE_DIR / ".env.example").read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.startswith("#")
)
SENSITIVE_NAME = re.compile(r"PASSWORD|SECRET|SALT|TOKEN|_KEY(_ID)?$|_AUTH$", re.IGNORECASE)
VAR_REF = re.compile(r"\$\{(\w+)")


def _one_shot(service: dict[str, Any]) -> bool:
    return service.get("restart") == "no"


def _env_items(service: dict[str, Any]) -> list[tuple[str, str]]:
    env = service.get("environment", {})
    if isinstance(env, list):
        return [tuple(item.split("=", 1)) for item in env]  # type: ignore[misc]
    return [(k, str(v)) for k, v in env.items()]


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_ports_bind_localhost_only(name: str) -> None:
    for port in SERVICES[name].get("ports", []):
        assert str(port).startswith("127.0.0.1:"), f"{name} publishes {port}"


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_long_running_services_have_healthcheck(name: str) -> None:
    service = SERVICES[name]
    assert _one_shot(service) or "healthcheck" in service, f"{name} has no healthcheck"


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_images_are_pinned(name: str) -> None:
    image = SERVICES[name]["image"]
    tag = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""
    assert re.search(r"\d", tag) and "latest" not in tag, f"{name} image {image} is not pinned"


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_secrets_come_from_env_file(name: str) -> None:
    for key, value in _env_items(SERVICES[name]):
        if SENSITIVE_NAME.search(key):
            assert value.startswith("${"), f"{name}.{key} is hardcoded"


def test_every_variable_is_defined_in_env_example() -> None:
    used = set(VAR_REF.findall((COMPOSE_DIR / "docker-compose.yml").read_text(encoding="utf-8")))
    assert used <= ENV_EXAMPLE.keys(), f"missing in .env.example: {used - ENV_EXAMPLE.keys()}"


@pytest.mark.parametrize("key", sorted(k for k in ENV_EXAMPLE if SENSITIVE_NAME.search(k)))
def test_env_example_secrets_are_obviously_fake(key: str) -> None:
    value = ENV_EXAMPLE[key]
    assert value.startswith("dev-only-") or set(value) == {"0"}, f"{key} does not look fake"
