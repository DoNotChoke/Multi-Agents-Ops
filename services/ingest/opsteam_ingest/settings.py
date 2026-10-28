"""Ingest process settings from `OPSTEAM_*` environment variables."""

from opsteam_core.config import Settings
from pydantic import SecretStr


class IngestSettings(Settings):
    """`database_url` connects as opsteam_app; `system_database_url` as opsteam_system."""

    database_url: SecretStr
    system_database_url: SecretStr
    # Same key as the API: webhook secrets live in the shared SecretStore.
    secrets_key: SecretStr
    redis_url: SecretStr
    webhook_rate_per_minute: int = 60
    # How long every alert must stay resolved before the incident is, and when it closes.
    incident_stable_window_seconds: int = 600
    incident_close_after_seconds: int = 86400
    # Where notification links point: the web app's origin.
    app_url: str = "http://localhost:3000"
