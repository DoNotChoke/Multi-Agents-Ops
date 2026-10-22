package main

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

type fakeStore struct{ err error }

func (f fakeStore) PlaceOrder(context.Context, int) error { return f.err }
func (f fakeStore) Ping(context.Context) error            { return f.err }

func env(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

func newTestServer(t *testing.T, cfg config, store orderStore) (*server, http.Handler) {
	t.Helper()
	s := newServer(cfg, slog.New(slog.NewJSONHandler(io.Discard, nil)), prometheus.NewRegistry(), store)
	return s, s.routes()
}

func get(t *testing.T, h http.Handler, path string) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequestWithContext(t.Context(), http.MethodGet, path, nil))
	return rec
}

func TestLoadConfigRejectsBadInput(t *testing.T) {
	cases := map[string]map[string]string{
		"unknown role":              {"ROLE": "cart"},
		"checkout without config":   {"ROLE": "checkout", "DATABASE_URL": "postgres://x", "PAYMENT_URL": "http://p"},
		"checkout without database": {"ROLE": "checkout", "REQUIRED_CONFIG": "on", "PAYMENT_URL": "http://p"},
		"frontend without checkout": {"ROLE": "frontend"},
		"bad integer":               {"ROLE": "payment", "LEAK_MB_PER_SEC": "lots"},
		"negative latency":          {"ROLE": "payment", "EXTRA_LATENCY_MS": "-5"},
	}
	for name, vars := range cases {
		if _, err := loadConfig(env(vars)); err == nil {
			t.Errorf("%s: expected an error", name)
		}
	}
}

func TestLoadConfigDefaults(t *testing.T) {
	cfg, err := loadConfig(env(map[string]string{"ROLE": "payment"}))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.port != "8080" || cfg.version != "dev" || cfg.dbMaxConns != 10 {
		t.Fatalf("unexpected defaults: %+v", cfg)
	}
}

func TestFrontendReturns502WhenCheckoutFails(t *testing.T) {
	down := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer down.Close()

	s, h := newTestServer(t, config{role: "frontend", checkoutURL: down.URL}, nil)
	if rec := get(t, h, "/checkout"); rec.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502", rec.Code)
	}
	if got := testutil.ToFloat64(s.requests.WithLabelValues("/checkout", "502")); got != 1 {
		t.Fatalf("requests_total{code=502} = %v, want 1", got)
	}
}

func TestCheckoutPlacesOrderThenPays(t *testing.T) {
	var paid bool
	payment := httptest.NewServer(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		paid = r.URL.Path == "/pay" && r.Header.Get(traceHeader) == "abc"
	}))
	defer payment.Close()

	_, h := newTestServer(t, config{role: "checkout", paymentURL: payment.URL}, fakeStore{})
	rec := httptest.NewRecorder()
	req := httptest.NewRequestWithContext(t.Context(), http.MethodGet, "/checkout", nil)
	req.Header.Set(traceHeader, "abc")
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK || !paid {
		t.Fatalf("status = %d, paid = %v", rec.Code, paid)
	}
}

func TestCheckoutFailsWhenDatabaseFails(t *testing.T) {
	_, h := newTestServer(t, config{role: "checkout", paymentURL: "http://unused"}, fakeStore{err: errors.New("pool exhausted")})
	if rec := get(t, h, "/checkout"); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("status = %d, want 503", rec.Code)
	}
	if rec := get(t, h, "/readyz"); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("readyz = %d, want 503", rec.Code)
	}
}

func TestPaymentAddsConfiguredLatency(t *testing.T) {
	_, h := newTestServer(t, config{role: "payment", extraLatency: 50 * time.Millisecond}, nil)
	start := time.Now()
	if rec := get(t, h, "/pay"); rec.Code != http.StatusOK {
		t.Fatalf("status = %d", rec.Code)
	}
	if took := time.Since(start); took < 50*time.Millisecond {
		t.Fatalf("took %v, want >= 50ms", took)
	}
}

func TestRequestLogIsJSONWithTraceID(t *testing.T) {
	var buf strings.Builder
	s := newServer(config{role: "payment"}, slog.New(slog.NewJSONHandler(&buf, nil)), prometheus.NewRegistry(), nil)
	get(t, s.routes(), "/pay")
	for _, want := range []string{`"route":"/pay"`, `"status":200`, `"trace_id":"`, `"duration_ms":`} {
		if !strings.Contains(buf.String(), want) {
			t.Errorf("log %q misses %s", buf.String(), want)
		}
	}
}

func TestMetricsEndpointExposesHistogram(t *testing.T) {
	_, h := newTestServer(t, config{role: "payment"}, nil)
	get(t, h, "/pay")
	body := get(t, h, "/metrics").Body.String()
	if !strings.Contains(body, "http_request_duration_seconds_bucket") {
		t.Fatal("histogram missing from /metrics")
	}
}
