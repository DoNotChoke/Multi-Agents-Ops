package contracts

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"sort"
	"strings"
	"testing"
)

const contractsDir = "../../../contracts"

func readJSON(t *testing.T, path string) map[string]any {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var v map[string]any
	if err := json.Unmarshal(data, &v); err != nil {
		t.Fatal(err)
	}
	return v
}

// TestTaskEnvelopeMatchesSchema keeps the hand-written struct in lockstep with
// contracts/jsonschema/task_envelope.json: same field names, same required/optional split.
func TestTaskEnvelopeMatchesSchema(t *testing.T) {
	schema := readJSON(t, filepath.Join(contractsDir, "jsonschema", "task_envelope.json"))
	compareObject(t, "TaskEnvelope", reflect.TypeFor[TaskEnvelope](), schema)
}

func compareObject(t *testing.T, path string, typ reflect.Type, schema map[string]any) {
	t.Helper()
	props, _ := schema["properties"].(map[string]any)
	required := map[string]bool{}
	if req, ok := schema["required"].([]any); ok {
		for _, r := range req {
			required[r.(string)] = true
		}
	}
	var fields []string
	for i := range typ.NumField() {
		f := typ.Field(i)
		name, opts, _ := strings.Cut(f.Tag.Get("json"), ",")
		fields = append(fields, name)
		prop, ok := props[name].(map[string]any)
		if !ok {
			t.Errorf("%s.%s: not in schema", path, name)
			continue
		}
		if optional := strings.Contains(opts, "omitempty"); optional == required[name] {
			t.Errorf("%s.%s: schema required=%v but struct omitempty=%v", path, name, required[name], optional)
		}
		ft := f.Type
		if ft.Kind() == reflect.Pointer {
			ft = ft.Elem()
		}
		if _, nested := prop["properties"]; nested && ft.Kind() == reflect.Struct {
			compareObject(t, path+"."+name, ft, prop)
		}
	}
	var want []string
	for name := range props {
		want = append(want, name)
	}
	sort.Strings(want)
	sort.Strings(fields)
	if !slices.Equal(want, fields) {
		t.Errorf("%s: schema fields %v, struct fields %v", path, want, fields)
	}
}

func decodeStrict(data []byte) error {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	var env TaskEnvelope
	return dec.Decode(&env)
}

// Conditional rules (approval iff kind=action) are not a decoding concern; envelope
// verification enforces them, so invalid-* examples are not used here.
func TestTaskEnvelopeExamplesDecodeStrictly(t *testing.T) {
	paths, err := filepath.Glob(filepath.Join(contractsDir, "examples", "task_envelope", "valid-*.json"))
	if err != nil || len(paths) == 0 {
		t.Fatalf("no examples: %v", err)
	}
	for _, p := range paths {
		t.Run(filepath.Base(p), func(t *testing.T) {
			data, err := os.ReadFile(p)
			if err != nil {
				t.Fatal(err)
			}
			if err := decodeStrict(data); err != nil {
				t.Fatalf("valid example rejected: %v", err)
			}
			doc := readJSON(t, p)
			doc["tenant_override"] = "x"
			mutated, _ := json.Marshal(doc)
			if decodeStrict(mutated) == nil {
				t.Fatal("unknown field accepted")
			}
		})
	}
}
