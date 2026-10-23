"""Process settings loaded from `OPSTEAM_*` environment variables."""

from typing import Literal, Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings shared by every process; `env` has no default so production is never implied."""

    model_config = SettingsConfigDict(env_prefix="OPSTEAM_", frozen=True)
    env: Literal["dev", "test", "production"]
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Exact "ip:port" pairs (the kind lab's Prometheus) that skip SSRF address checks.
    netguard_dev_allow: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def _no_dev_allow_in_production(self) -> Self:
        if self.netguard_dev_allow and self.env == "production":
            raise ValueError("OPSTEAM_NETGUARD_DEV_ALLOW must not be set in production")
        return self
