import time
from collections.abc import Callable

import pytest
from opsteam_api.auth import InvalidToken

from tests.jwt_helpers import ISSUER, Signer, verifier

SIGNER = Signer()
VERIFY = verifier(SIGNER)
NOW = int(time.time())


async def test_valid_token_yields_claims() -> None:
    claims = await VERIFY.verify(SIGNER.token(email="a@example.com"))
    assert (claims.issuer, claims.subject, claims.email) == (ISSUER, "subject-1", "a@example.com")


REJECTED: dict[str, Callable[[], str]] = {
    "garbage": lambda: "not-a-jwt",
    "expired": lambda: SIGNER.token(exp=NOW - 120),
    "not_yet_valid": lambda: SIGNER.token(nbf=NOW + 300),
    "wrong_issuer": lambda: SIGNER.token(iss="https://evil.test/realms/opsteam"),
    "wrong_audience": lambda: SIGNER.token(aud="some-other-client"),
    "no_audience": lambda: SIGNER.token(aud=None),
    "no_subject": lambda: SIGNER.token(sub=None),
    "no_expiry": lambda: SIGNER.token(exp=None),
    "alg_none": SIGNER.unsigned,
    "hs256_with_public_key": SIGNER.hs256_with_public_key,
    "other_key_same_kid": lambda: Signer(kid=SIGNER.kid).token(),
    "unknown_kid": lambda: Signer(kid="k2").token(),
}


@pytest.mark.parametrize("case", sorted(REJECTED))
async def test_rejected_tokens(case: str) -> None:
    with pytest.raises(InvalidToken):
        await VERIFY.verify(REJECTED[case]())
