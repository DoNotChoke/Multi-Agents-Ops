// Command demo-app is the lab's small shop: one binary whose ROLE picks frontend, checkout,
// payment or loadgen. Fault knobs are plain env vars so every fault is a real K8s change.
package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"runtime"
	"strconv"
	"syscall"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

const (
	traceHeader        = "X-Trace-Id"
	paymentBaseLatency = 20 * time.Millisecond
)

type config struct {
	role, version, port                  string
	frontendURL, checkoutURL, paymentURL string
	databaseURL, requiredConfig          string
	dbMaxConns                           int32
	dbQuery, extraLatency                time.Duration
	leakMBPerSec, rps                    int
}

// loadConfig reads and validates the env; a missing REQUIRED_CONFIG on checkout is fatal on purpose.
func loadConfig(getenv func(string) string) (config, error) {
	or := func(key, def string) string {
		if v := getenv(key); v != "" {
			return v
		}
		return def
	}
	num := func(key string, def int) (int, error) {
		n, err := strconv.Atoi(or(key, strconv.Itoa(def)))
		if err != nil || n < 0 {
			return 0, fmt.Errorf("%s must be a non-negative integer", key)
		}
		return n, nil
	}
	cfg := config{
		role: getenv("ROLE"), version: or("VERSION", "dev"), port: or("PORT", "8080"),
		frontendURL: getenv("FRONTEND_URL"), checkoutURL: getenv("CHECKOUT_URL"),
		paymentURL: getenv("PAYMENT_URL"), databaseURL: getenv("DATABASE_URL"),
		requiredConfig: getenv("REQUIRED_CONFIG"),
	}
	var maxConns, queryMS, latencyMS int
	for _, f := range []struct {
		key string
		def int
		dst *int
	}{
		{"DB_MAX_CONNS", 10, &maxConns}, {"DB_QUERY_MS", 40, &queryMS}, {"EXTRA_LATENCY_MS", 0, &latencyMS},
		{"LEAK_MB_PER_SEC", 0, &cfg.leakMBPerSec}, {"RPS", 30, &cfg.rps},
	} {
		n, err := num(f.key, f.def)
		if err != nil {
			return config{}, err
		}
		*f.dst = n
	}
	if maxConns < 1 || maxConns > 100 {
		return config{}, errors.New("DB_MAX_CONNS must be between 1 and 100")
	}
	cfg.dbMaxConns = int32(maxConns) // #nosec G115 -- bounded above
	cfg.dbQuery = time.Duration(queryMS) * time.Millisecond
	cfg.extraLatency = time.Duration(latencyMS) * time.Millisecond

	switch {
	case cfg.role == "frontend" && cfg.checkoutURL == "":
		return config{}, errors.New("frontend needs CHECKOUT_URL")
	case cfg.role == "checkout" && cfg.requiredConfig == "":
		return config{}, errors.New("checkout needs REQUIRED_CONFIG")
	case cfg.role == "checkout" && (cfg.databaseURL == "" || cfg.paymentURL == ""):
		return config{}, errors.New("checkout needs DATABASE_URL and PAYMENT_URL")
	case cfg.role == "loadgen" && cfg.frontendURL == "":
		return config{}, errors.New("loadgen needs FRONTEND_URL")
	case cfg.role != "frontend" && cfg.role != "checkout" && cfg.role != "payment" && cfg.role != "loadgen":
		return config{}, fmt.Errorf("unknown ROLE %q", cfg.role)
	}
	return cfg, nil
}

type orderStore interface {
	PlaceOrder(ctx context.Context, amount int) error
	Ping(ctx context.Context) error
}

type pgStore struct {
	pool  *pgxpool.Pool
	query time.Duration
}

// PlaceOrder holds a pooled connection for the configured query time, so a small pool queues requests.
func (s pgStore) PlaceOrder(ctx context.Context, amount int) error {
	_, err := s.pool.Exec(ctx, "INSERT INTO orders (amount) SELECT $1 FROM pg_sleep($2)", amount, s.query.Seconds())
	return err
}

func (s pgStore) Ping(ctx context.Context) error { return s.pool.Ping(ctx) }

type server struct {
	cfg      config
	log      *slog.Logger
	reg      *prometheus.Registry
	client   *http.Client
	store    orderStore
	requests *prometheus.CounterVec
	duration *prometheus.HistogramVec
}

func newServer(cfg config, log *slog.Logger, reg *prometheus.Registry, store orderStore) *server {
	s := &server{
		cfg: cfg, log: log, reg: reg, store: store,
		client: &http.Client{Timeout: 3 * time.Second},
		requests: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "http_requests_total", Help: "HTTP requests served.",
		}, []string{"route", "code"}),
		duration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name: "http_request_duration_seconds", Help: "HTTP request latency.",
			Buckets: []float64{.01, .025, .05, .1, .25, .5, 1, 2.5, 5},
		}, []string{"route"}),
	}
	reg.MustRegister(s.requests, s.duration, collectors.NewGoCollector(), collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}))
	return s
}

func (s *server) routes() http.Handler {
	mux := http.NewServeMux()
	mux.Handle("GET /metrics", promhttp.HandlerFor(s.reg, promhttp.HandlerOpts{}))
	mux.HandleFunc("GET /healthz", func(http.ResponseWriter, *http.Request) {})
	mux.HandleFunc("GET /readyz", s.ready)
	switch s.cfg.role {
	case "frontend":
		mux.Handle("GET /checkout", s.instrument("/checkout", s.frontendCheckout))
	case "checkout":
		mux.Handle("GET /checkout", s.instrument("/checkout", s.checkout))
	case "payment":
		mux.Handle("GET /pay", s.instrument("/pay", s.pay))
	}
	return mux
}

func (s *server) ready(w http.ResponseWriter, r *http.Request) {
	if s.store != nil && s.store.Ping(r.Context()) != nil {
		w.WriteHeader(http.StatusServiceUnavailable)
	}
}

type statusRecorder struct {
	http.ResponseWriter
	status int
}

func (r *statusRecorder) WriteHeader(code int) {
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

// instrument records metrics and one JSON log line per request, minting a trace id when absent.
func (s *server) instrument(route string, next http.HandlerFunc) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get(traceHeader) == "" {
			r.Header.Set(traceHeader, newTraceID())
		}
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		start := time.Now()
		next(rec, r)
		took := time.Since(start)
		s.requests.WithLabelValues(route, strconv.Itoa(rec.status)).Inc()
		s.duration.WithLabelValues(route).Observe(took.Seconds())
		level := slog.LevelInfo
		if rec.status >= 500 {
			level = slog.LevelError
		}
		s.log.Log(r.Context(), level, "request", "route", route, "status", rec.status,
			"duration_ms", took.Milliseconds(), "trace_id", r.Header.Get(traceHeader))
	})
}

func (s *server) frontendCheckout(w http.ResponseWriter, r *http.Request) {
	if err := s.call(r, s.cfg.checkoutURL+"/checkout"); err != nil {
		s.log.ErrorContext(r.Context(), "checkout call failed", "error", err.Error(), "trace_id", r.Header.Get(traceHeader))
		w.WriteHeader(http.StatusBadGateway)
	}
}

func (s *server) checkout(w http.ResponseWriter, r *http.Request) {
	if err := s.store.PlaceOrder(r.Context(), 42); err != nil {
		s.log.ErrorContext(r.Context(), "place order failed", "error", err.Error(), "trace_id", r.Header.Get(traceHeader))
		w.WriteHeader(http.StatusServiceUnavailable)
		return
	}
	if err := s.call(r, s.cfg.paymentURL+"/pay"); err != nil {
		s.log.ErrorContext(r.Context(), "payment call failed", "error", err.Error(), "trace_id", r.Header.Get(traceHeader))
		w.WriteHeader(http.StatusBadGateway)
	}
}

func (s *server) pay(http.ResponseWriter, *http.Request) {
	time.Sleep(paymentBaseLatency + s.cfg.extraLatency)
}

func (s *server) call(r *http.Request, url string) error {
	req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	req.Header.Set(traceHeader, r.Header.Get(traceHeader))
	resp, err := s.client.Do(req)
	if err != nil {
		return err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode >= 400 {
		return fmt.Errorf("%s returned %d", url, resp.StatusCode)
	}
	return nil
}

func newTraceID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

// leak grows resident memory by mb MiB per second until the container is OOM-killed.
func leak(ctx context.Context, mb int) {
	var held [][]byte
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			chunk := make([]byte, mb<<20)
			for i := range chunk {
				chunk[i] = 1
			}
			held = append(held, chunk)
			runtime.KeepAlive(held)
		}
	}
}

// loadgen sends rps requests per second to the frontend and never waits on them.
func loadgen(ctx context.Context, s *server) {
	tick := time.NewTicker(time.Second / time.Duration(max(s.cfg.rps, 1)))
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
			go func() {
				// FRONTEND_URL comes from the lab manifest, not from request input.
				req, err := http.NewRequestWithContext(ctx, http.MethodGet, s.cfg.frontendURL+"/checkout", nil) // #nosec G704
				if err != nil {
					return
				}
				if resp, err := s.client.Do(req); err == nil { // #nosec G704
					_ = resp.Body.Close()
				}
			}()
		}
	}
}

func openStore(ctx context.Context, cfg config, reg *prometheus.Registry) (pgStore, error) {
	pc, err := pgxpool.ParseConfig(cfg.databaseURL)
	if err != nil {
		return pgStore{}, fmt.Errorf("parse DATABASE_URL: %w", err)
	}
	pc.MaxConns = cfg.dbMaxConns
	pool, err := pgxpool.NewWithConfig(ctx, pc)
	if err != nil {
		return pgStore{}, fmt.Errorf("open pool: %w", err)
	}
	if _, err := pool.Exec(ctx, "CREATE TABLE IF NOT EXISTS orders (id bigserial PRIMARY KEY, amount int NOT NULL, created_at timestamptz NOT NULL DEFAULT now())"); err != nil {
		return pgStore{}, fmt.Errorf("create table: %w", err)
	}
	reg.MustRegister(
		prometheus.NewGaugeFunc(prometheus.GaugeOpts{Name: "db_pool_max_conns", Help: "Pool size."},
			func() float64 { return float64(pool.Stat().MaxConns()) }),
		prometheus.NewGaugeFunc(prometheus.GaugeOpts{Name: "db_pool_acquired_conns", Help: "Connections in use."},
			func() float64 { return float64(pool.Stat().AcquiredConns()) }),
		prometheus.NewCounterFunc(prometheus.CounterOpts{Name: "db_pool_empty_acquire_total", Help: "Acquires that had to wait for a free connection."},
			func() float64 { return float64(pool.Stat().EmptyAcquireCount()) }),
		prometheus.NewCounterFunc(prometheus.CounterOpts{Name: "db_pool_acquire_wait_seconds_total", Help: "Total time spent waiting for a connection."},
			func() float64 { return pool.Stat().AcquireDuration().Seconds() }),
	)
	return pgStore{pool: pool, query: cfg.dbQuery}, nil
}

func run(ctx context.Context, log *slog.Logger) error {
	cfg, err := loadConfig(os.Getenv)
	if err != nil {
		return err
	}
	reg := prometheus.NewRegistry()
	var store orderStore
	if cfg.role == "checkout" {
		pg, err := openStore(ctx, cfg, reg)
		if err != nil {
			return err
		}
		defer pg.pool.Close()
		store = pg
	}
	s := newServer(cfg, log, reg, store)
	switch {
	case cfg.role == "loadgen":
		go loadgen(ctx, s)
	case cfg.role == "payment" && cfg.leakMBPerSec > 0:
		go leak(ctx, cfg.leakMBPerSec)
	}

	srv := &http.Server{Addr: ":" + cfg.port, Handler: s.routes(), ReadHeaderTimeout: 5 * time.Second}
	errc := make(chan error, 1)
	go func() { errc <- srv.ListenAndServe() }()
	log.InfoContext(ctx, "started", "port", cfg.port)
	select {
	case err := <-errc:
		return err
	case <-ctx.Done():
		shutdown, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer cancel()
		return srv.Shutdown(shutdown)
	}
}

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil)).With("service", os.Getenv("ROLE"), "version", os.Getenv("VERSION"))
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	if err := run(ctx, log); err != nil {
		log.ErrorContext(ctx, "exiting", "error", err.Error())
		stop()
		os.Exit(1)
	}
}
