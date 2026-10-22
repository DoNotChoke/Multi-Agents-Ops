package envelope

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strconv"
	"strings"

	"github.com/gowebpki/jcs"
)

// maxSafeInteger is 2^53-1, the largest integer a JSON number can carry exactly.
const maxSafeInteger = 1<<53 - 1

var errUnsafeInteger = errors.New("integer literal outside ±(2^53-1)")

// Canonicalize returns the RFC 8785 form of raw JSON as received, rejecting duplicate keys,
// NaN, invalid text and integers beyond ±(2^53-1), which jcs would otherwise round silently.
func Canonicalize(raw []byte) ([]byte, error) {
	if err := rejectUnsafeIntegers(raw); err != nil {
		return nil, fmt.Errorf("canonicalize: %w", err)
	}
	out, err := jcs.Transform(raw)
	if err != nil {
		return nil, fmt.Errorf("canonicalize: %w", err)
	}
	return out, nil
}

func rejectUnsafeIntegers(raw []byte) error {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	for {
		tok, err := dec.Token()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("scan numbers: %w", err)
		}
		n, ok := tok.(json.Number)
		if !ok || strings.ContainsAny(string(n), ".eE") {
			continue
		}
		v, err := strconv.ParseInt(string(n), 10, 64)
		if err != nil || v > maxSafeInteger || v < -maxSafeInteger {
			return errUnsafeInteger
		}
	}
}
