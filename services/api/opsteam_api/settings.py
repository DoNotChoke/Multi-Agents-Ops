"""API process settings from `OPSTEAM_*` environment variables."""

from opsteam_core.config import Settings
from pydantic import SecretStr


class ApiSettings(Settings):
    """Settings for the API; `database_url` connects as the opsteam_app role."""

    database_url: SecretStr
    oidc_issuer: str
    oidc_jwks_url: str
    oidc_audience: str = "opsteam-api"
    # 32 bytes, base64; encrypts integration credentials in the dev SecretStore.
    secrets_key: SecretStr
    # Public base URL of the ingest service, shown to customers as their webhook address.
    ingest_url: str = "http://127.0.0.1:8001"
