"""Bearer token verification against the identity provider's signing keys."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import jwt
from opsteam_core.errors import OpsteamError

# Fixed in code: the token header never chooses the algorithm (no "none", no HMAC).
ALGORITHMS = ["RS256", "ES256"]
LEEWAY_S = 30


class InvalidToken(OpsteamError):
    """The bearer token is missing, malformed, expired or not issued for this API."""

    code = "auth.invalid_token"


@dataclass(frozen=True)
class Claims:
    """The verified claims the API relies on."""

    issuer: str
    subject: str
    email: str | None


class TokenVerifier:
    """Verify JWTs for one issuer and audience; `resolve_key` maps a token to its signing key."""

    def __init__(self, issuer: str, audience: str, resolve_key: Callable[[str], jwt.PyJWK]) -> None:
        self._issuer = issuer
        self._audience = audience
        self._resolve_key = resolve_key

    async def verify(self, token: str) -> Claims:
        """Return the token's claims, or raise InvalidToken without saying why."""
        try:
            # The key lookup may fetch the JWKS over HTTP, so keep it off the event loop.
            key = await asyncio.to_thread(self._resolve_key, token)
            claims = jwt.decode(
                token,
                key,
                algorithms=ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                leeway=LEEWAY_S,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken("invalid bearer token") from exc
        return Claims(issuer=claims["iss"], subject=claims["sub"], email=claims.get("email"))


def jwks_verifier(issuer: str, audience: str, jwks_url: str) -> TokenVerifier:
    """A verifier that fetches and caches the issuer's JWKS from `jwks_url`."""
    client = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=300, timeout=5)
    return TokenVerifier(issuer, audience, client.get_signing_key_from_jwt)
