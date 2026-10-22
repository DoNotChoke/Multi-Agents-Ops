"""Process settings loaded from `OPSTEAM_*` environment variables."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings shared by every process; `env` has no default so production is never implied."""

    model_config = SettingsConfigDict(env_prefix="OPSTEAM_", frozen=True)
    env: Literal["dev", "test", "production"]
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
