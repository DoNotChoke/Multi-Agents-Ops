package envelope

import (
	"crypto/ed25519"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

type signatureVector struct {
	PublicKeyHex string `json:"public_key_hex"`
	EnvelopeJSON string `json:"envelope_json"`
	SigningInput string `json:"signing_input"`
	Signature    string `json:"signature"`
}

// Go half of contracts/testvectors/ed25519.json (Python: tests/contract/test_ed25519_vectors.py).
func loadSignatureVector(t *testing.T) (signatureVector, ed25519.PublicKey, []byte) {
	t.Helper()
	data, err := os.ReadFile(filepath.Join("..", "..", "..", "contracts", "testvectors", "ed25519.json"))
	if err != nil {
		t.Fatal(err)
	}
	var v signatureVector
	if err := json.Unmarshal(data, &v); err != nil {
		t.Fatal(err)
	}
	pub, err := hex.DecodeString(v.PublicKeyHex)
	if err != nil {
		t.Fatal(err)
	}
	sig, err := base64.RawURLEncoding.DecodeString(v.Signature)
	if err != nil {
		t.Fatal(err)
	}
	return v, ed25519.PublicKey(pub), sig
}

// signingInput drops "signature" from the raw envelope and canonicalizes the rest as received.
func signingInput(t *testing.T, raw []byte) []byte {
	t.Helper()
	if _, err := Canonicalize(raw); err != nil { // duplicate keys etc. before map decoding
		t.Fatal(err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		t.Fatal(err)
	}
	delete(fields, "signature")
	rest, err := json.Marshal(fields)
	if err != nil {
		t.Fatal(err)
	}
	out, err := Canonicalize(rest)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func TestSignatureVector(t *testing.T) {
	v, pub, sig := loadSignatureVector(t)
	got := signingInput(t, []byte(v.EnvelopeJSON))
	if string(got) != v.SigningInput {
		t.Fatalf("signing input mismatch:\n got %s\nwant %s", got, v.SigningInput)
	}
	if !ed25519.Verify(pub, got, sig) {
		t.Fatal("valid signature rejected")
	}
}

func TestSignatureRejectsChangedTenant(t *testing.T) {
	v, pub, sig := loadSignatureVector(t)
	var fields map[string]json.RawMessage
	if err := json.Unmarshal([]byte(v.EnvelopeJSON), &fields); err != nil {
		t.Fatal(err)
	}
	fields["tenant_id"] = json.RawMessage(`"tnt_01JBQ2Z8Y3K4M5N6P7Q8R9S0T2"`)
	tampered, _ := json.Marshal(fields)
	if ed25519.Verify(pub, signingInput(t, tampered), sig) {
		t.Fatal("signature accepted for a different tenant")
	}
}

func TestSignatureRejectsFlippedByte(t *testing.T) {
	v, pub, sig := loadSignatureVector(t)
	sig[0] ^= 1
	if ed25519.Verify(pub, []byte(v.SigningInput), sig) {
		t.Fatal("corrupted signature accepted")
	}
}
