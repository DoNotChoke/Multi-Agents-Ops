import pytest
from opsteam_worker import toolcall


def test_refuses_outside_dev(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in {
        "OPSTEAM_ENV": "production",
        "OPSTEAM_DATABASE_URL": "postgresql+asyncpg://x@127.0.0.1:1/none",
        "OPSTEAM_SECRETS_KEY": "dev-only-not-used",
        "OPSTEAM_REDIS_URL": "redis://127.0.0.1:1/0",
        "OPSTEAM_S3_ACCESS_KEY": "dev-only-s3",
        "OPSTEAM_S3_SECRET_KEY": "dev-only-s3",
    }.items():
        monkeypatch.setenv(name, value)
    assert toolcall.main(["--tenant", "x", "--integration", "y", "--service", "s",
                          "--environment", "e"]) == 2  # fmt: skip
    assert "OPSTEAM_ENV=dev" in capsys.readouterr().err
