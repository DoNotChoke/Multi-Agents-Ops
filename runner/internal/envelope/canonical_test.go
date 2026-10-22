package envelope

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

type canonicalVector struct {
	Name      string `json:"name"`
	Input     string `json:"input"`
	Canonical string `json:"canonical"`
	Error     bool   `json:"error"`
}

// Same vectors as packages/core/tests/test_canonical.py: both sides must agree byte for byte.
func TestCanonicalizeSharedVectors(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("..", "..", "..", "contracts", "testvectors", "canonical.json"))
	if err != nil {
		t.Fatal(err)
	}
	var vectors []canonicalVector
	if err := json.Unmarshal(data, &vectors); err != nil {
		t.Fatal(err)
	}
	for _, v := range vectors {
		t.Run(v.Name, func(t *testing.T) {
			got, err := Canonicalize([]byte(v.Input))
			if v.Error {
				if err == nil {
					t.Fatalf("accepted %q as %q", v.Input, got)
				}
				return
			}
			if err != nil {
				t.Fatalf("rejected %q: %v", v.Input, err)
			}
			if string(got) != v.Canonical {
				t.Fatalf("got %q, want %q", got, v.Canonical)
			}
		})
	}
}
