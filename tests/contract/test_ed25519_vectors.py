"""Ed25519 over RFC 8785 canonical JSON: Python half of the shared vector.

runner/internal/envelope/signature_test.go runs the same file in Go.
"""

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from opsteam_core.canonical import canonical_json, canonicalize_text

VECTOR: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[2] / "contracts/testvectors/ed25519.json").read_text(
        encoding="utf-8"
    )
)
PUBLIC_KEY = Ed25519PublicKey.from_public_bytes(bytes.fromhex(VECTOR["public_key_hex"]))


def _signing_input(raw: str) -> bytes:
    canonicalize_text(raw)  # rejects duplicate keys etc. before we trust json.loads
    doc = json.loads(raw)
    doc.pop("signature")
    return canonical_json(doc)


def _signature(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def test_signing_input_is_recomputed_from_the_raw_envelope() -> None:
    assert _signing_input(VECTOR["envelope_json"]) == VECTOR["signing_input"].encode("utf-8")


def test_signature_verifies() -> None:
    PUBLIC_KEY.verify(_signature(VECTOR["signature"]), VECTOR["signing_input"].encode("utf-8"))


def test_signing_is_deterministic() -> None:
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(VECTOR["private_key_seed_hex"]))
    signature = key.sign(VECTOR["signing_input"].encode("utf-8"))
    assert base64.urlsafe_b64encode(signature).rstrip(b"=").decode() == VECTOR["signature"]


def test_changed_tenant_breaks_the_signature() -> None:
    doc = json.loads(VECTOR["envelope_json"])
    doc["tenant_id"] = "tnt_01JBQ2Z8Y3K4M5N6P7Q8R9S0T2"
    with pytest.raises(InvalidSignature):
        PUBLIC_KEY.verify(_signature(doc["signature"]), _signing_input(json.dumps(doc)))


def test_flipped_signature_byte_fails() -> None:
    signature = bytearray(_signature(VECTOR["signature"]))
    signature[0] ^= 1
    with pytest.raises(InvalidSignature):
        PUBLIC_KEY.verify(bytes(signature), VECTOR["signing_input"].encode("utf-8"))
