"""API process settings from `OPSTEAM_*` environment variables."""

from opsteam_core.config import Settings
from pydantic import SecretStr


class ApiSettings(Settings):
    """Settings for the API; `database_url` connects as the opsteam_app role."""

    database_url: SecretStr
    oidc_issuer: str
    oidc_jwks_url: str
    oidc_audience: str = "opsteam-api"
