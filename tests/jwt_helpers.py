"""Locally signed tokens and an in-memory JWKS, so auth tests need no IdP."""

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from opsteam_api.auth import TokenVerifier

ISSUER = "https://idp.test/realms/opsteam"
AUDIENCE = "opsteam-api"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Signer:
    """An RSA key pair that signs test tokens under a fixed `kid`."""

    def __init__(self, kid: str = "k1") -> None:
        self.kid = kid
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def claims(self, **overrides: Any) -> dict[str, Any]:
        now = int(time.time())
        base = {"iss": ISSUER, "aud": AUDIENCE, "sub": "subject-1", "iat": now, "exp": now + 300}
        return {k: v for k, v in (base | overrides).items() if v is not None}

    def token(self, **overrides: Any) -> str:
        return jwt.encode(self.claims(**overrides), self.key, "RS256", headers={"kid": self.kid})

    def jwk(self) -> dict[str, Any]:
        public = RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        return public | {"kid": self.kid, "alg": "RS256", "use": "sig"}

    def public_pem(self) -> bytes:
        return self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    def hs256_with_public_key(self) -> str:
        """A token 'signed' with HMAC using the public key as secret (algorithm confusion)."""
        header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": self.kid}).encode())
        payload = _b64(json.dumps(self.claims()).encode())
        mac = hmac.new(self.public_pem(), f"{header}.{payload}".encode(), hashlib.sha256)
        return f"{header}.{payload}.{_b64(mac.digest())}"

    def unsigned(self) -> str:
        header = _b64(json.dumps({"alg": "none", "typ": "JWT", "kid": self.kid}).encode())
        return f"{header}.{_b64(json.dumps(self.claims()).encode())}."


def verifier(*signers: Signer) -> TokenVerifier:
    """A verifier whose JWKS holds the public keys of `signers`."""
    jwks = jwt.PyJWKSet.from_dict({"keys": [s.jwk() for s in signers]})

    def resolve(token: str) -> jwt.PyJWK:
        kid = jwt.get_unverified_header(token).get("kid")
        for key in jwks.keys:
            if key.key_id == kid:
                return key
        raise jwt.PyJWKClientError(f"unknown kid {kid!r}")

    return TokenVerifier(ISSUER, AUDIENCE, resolve)
