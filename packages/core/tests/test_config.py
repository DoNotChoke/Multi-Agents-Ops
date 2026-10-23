import pytest
from opsteam_core.config import Settings
from pydantic import ValidationError


def test_reads_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSTEAM_ENV", "production")
    monkeypatch.setenv("OPSTEAM_LOG_LEVEL", "WARNING")
    settings = Settings()
    assert settings.env == "production"
    assert settings.log_level == "WARNING"


def test_env_must_be_set_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPSTEAM_ENV", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_rejects_unknown_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSTEAM_ENV", "prod")
    with pytest.raises(ValidationError):
        Settings()


def test_netguard_dev_allow_parses_host_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSTEAM_ENV", "dev")
    monkeypatch.setenv("OPSTEAM_NETGUARD_DEV_ALLOW", '["127.0.0.1:30090"]')
    assert Settings().netguard_dev_allow == frozenset({"127.0.0.1:30090"})


def test_netguard_dev_allow_is_refused_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSTEAM_ENV", "production")
    monkeypatch.setenv("OPSTEAM_NETGUARD_DEV_ALLOW", '["127.0.0.1:30090"]')
    with pytest.raises(ValidationError, match="production"):
        Settings()
