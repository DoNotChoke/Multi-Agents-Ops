import pytest
from opsteam_api import devseed


def test_refuses_production(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "OPSTEAM_ENV": "production",
        "OPSTEAM_DATABASE_URL": "postgresql+asyncpg://x@127.0.0.1:1/none",
        "OPSTEAM_OIDC_ISSUER": "https://idp.test",
        "OPSTEAM_OIDC_JWKS_URL": "https://idp.test/certs",
        "OPSTEAM_SECRETS_KEY": "dev-only-not-used",
    }.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit, match="production"):
        devseed.main()
