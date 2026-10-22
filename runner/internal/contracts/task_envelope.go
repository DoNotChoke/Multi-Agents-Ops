// Package contracts is the Go side of contracts/jsonschema that the Runner needs.
// Hand-written, not generated: contracts_test.go fails if it drifts from the schema.
package contracts

import "encoding/json"

// TaskEnvelope mirrors contracts/jsonschema/task_envelope.json. Timestamps stay RFC 3339
// strings and arguments stay raw: the signature covers the JSON as received, so nothing is
// re-encoded, and callers parse what they need explicitly.
type TaskEnvelope struct {
	SchemaVersion  int             `json:"schema_version"`
	TaskID         string          `json:"task_id"`
	TenantID       string          `json:"tenant_id"`
	RunnerID       string          `json:"runner_id"`
	Kind           string          `json:"kind"`
	Tool           string          `json:"tool"`
	IntegrationRef string          `json:"integration_ref"`
	Arguments      json.RawMessage `json:"arguments"`
	ResourceScope  ResourceScope   `json:"resource_scope"`
	Limits         Limits          `json:"limits"`
	IdempotencyKey string          `json:"idempotency_key"`
	IssuedAt       string          `json:"issued_at"`
	ExpiresAt      string          `json:"expires_at"`
	PolicyVersion  string          `json:"policy_version"`
	Approval       *Approval       `json:"approval,omitempty"`
	KeyID          string          `json:"key_id"`
	Signature      string          `json:"signature"`
}

// ResourceScope lists what the task may touch.
type ResourceScope struct {
	Namespaces  []string `json:"namespaces"`
	Services    []string `json:"services"`
	Datasources []string `json:"datasources"`
}

// Limits are the control plane's caps; the Runner clamps them to its own.
type Limits struct {
	MaxBytes         int64 `json:"max_bytes"`
	MaxRows          int64 `json:"max_rows"`
	MaxSeries        int64 `json:"max_series"`
	TimeoutMs        int64 `json:"timeout_ms"`
	MaxWindowSeconds int64 `json:"max_window_seconds"`
}

// Approval is present only on kind=action envelopes.
type Approval struct {
	ApprovalID string `json:"approval_id"`
	ActionHash string `json:"action_hash"`
	ApproverID string `json:"approver_id"`
	ApprovedAt string `json:"approved_at"`
	ExpiresAt  string `json:"expires_at"`
}
