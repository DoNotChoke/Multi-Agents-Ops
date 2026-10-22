"""The authenticated caller context every business function receives first."""

from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from ulid import ULID

from opsteam_core.ids import parse_id

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class Principal(BaseModel):
    """Who is acting: a user, an internal system component, or a runner."""

    model_config = _FROZEN
    type: Literal["user", "system", "runner"]
    id: str

    @model_validator(mode="after")
    def _id_matches_type(self) -> Self:
        if self.type == "user":
            parse_id(self.id, "usr")
        elif self.type == "runner":
            parse_id(self.id, "rnr")
        return self


class RequestContext(BaseModel):
    """Identity, tenant and permissions established server-side at the edge; never from input."""

    model_config = _FROZEN
    tenant_id: Annotated[str, AfterValidator(lambda v: parse_id(v, "tnt"))]
    principal: Principal
    roles: tuple[str, ...]
    scopes: frozenset[str]
    request_id: str = Field(default_factory=lambda: str(ULID()))
    incident_id: Annotated[str, AfterValidator(lambda v: parse_id(v, "inc"))] | None = None
