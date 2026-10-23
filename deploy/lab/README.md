# Lab

A local kind cluster that plays the customer's side: a small shop app plus the
Prometheus/Alertmanager/Loki stack a customer would already run. Named faults turn
real Kubernetes changes into incidents with a known root cause.

```
make lab-up                     # create/refresh the cluster (~5 min from scratch)
make lab-fault FAULT=<name>     # break something
make lab-heal                   # re-apply the healthy baseline
make lab-verify [SCENARIO=<id>] # = make eval LAB=1: run each eval scenario's fault, time its alerts, heal
make lab-down                   # delete the cluster
```

| UI / API | URL |
| --- | --- |
| Shop frontend | http://127.0.0.1:30080/checkout |
| Prometheus | http://127.0.0.1:30090 |
| Alertmanager | http://127.0.0.1:30093 |
| Loki (LogQL API) | http://127.0.0.1:30100 |

All ports bind 127.0.0.1. Every `kubectl`/`helm` call pins the `kind-opsteam-lab` context.

## Shop (namespace `shop`)

```
loadgen --30 rps--> frontend --> checkout --> payment
                                    |
                                 Postgres
```

One Go binary (`demo-app/`), role picked by `ROLE`. Each service exposes
`http_requests_total{route,code}` and `http_request_duration_seconds`; checkout also
exposes `db_pool_*`. Pod labels `service` and `version` become Prometheus and Loki labels.
Logs are one JSON line per request with a `trace_id` carried across services.

Baseline: ~30 rps per service, p95 ~0.1–0.2 s, no 5xx, no alerts.

## Alerts (`observability/prometheus-values.yaml`)

| Alert | Fires when | Severity |
| --- | --- | --- |
| `HighLatencyP95` | p95 of a service > 500 ms for 2 m | warning |
| `HighErrorRate` | 5xx > 5 % of a service's requests for 2 m | critical |
| `PodRestarting` | > 2 container restarts in 10 m | warning |
| `PodCrashLooping` | a container sits in `CrashLoopBackOff` for 1 m | critical |
| `ContainerOOMKilled` | last termination was `OOMKilled` and it restarted in the last 10 m | warning |

Alertmanager routes everything to a `null` receiver until the Event Gateway exists (T-2.1).

## Faults (`faults/<name>/`)

Each fault is a kustomize overlay on `base/`; healing re-applies `base/`.
Expected alerts and deadlines live in `evals/scenarios/*.yaml` (the single source used by
`lab-verify`); "measured" is one run on a 12-thread laptop.

| Fault | Change | Expected alerts | Measured to alert | Quiet after heal | Ground truth |
| --- | --- | --- | --- | --- | --- |
| `bad_release_db_pool` | checkout rolled to `version: v2` with `DB_MAX_CONNS=1`; 15 rps × 80 ms per pod overloads one connection | `HighLatencyP95` checkout | 170 s | ~2.5 min | `deployment_regression`; checkout, postgres |
| `oom_killed_service` | payment `LEAK_MB_PER_SEC=8` under a 64 Mi limit | `ContainerOOMKilled` + `PodRestarting` payment | 30 s / 50 s | ~9.5 min | `resource_exhaustion`; payment |
| `dependency_slow` | payment `EXTRA_LATENCY_MS=900` | `HighLatencyP95` checkout and frontend | 160 s / 170 s | ~2 min | `dependency_failure`; payment |
| `crashloop_config` | ConfigMap drops `REQUIRED_CONFIG`; checkout rolled out with `Recreate` | `PodCrashLooping` checkout + `HighErrorRate` frontend | 80 s / 150 s | ~8 min | `config_change`; checkout |
| `benign_noise` | frontend rolled to `version: v1.1`, same behaviour | none during 5 min | — | — | `benign: true` |

Other alerts may fire alongside the expected ones (e.g. `HighErrorRate` during `oom_killed_service`);
`lab-verify` only requires the expected set, except for `benign_noise`, which must stay silent.

Ground truth is in the matching `evals/scenarios/*.yaml` (`root_cause.category`, `entities`).

## Notes

- Postgres uses an emptyDir; its schema comes from the `postgres-init` ConfigMap on every start.
- Checkout bounds each query (2 s) and ping (0.5 s) so connections to a replaced Postgres pod
  are dropped within seconds instead of hanging.
- Restart-based alerts look back 10 minutes, so after healing `oom_killed_service` or
  `crashloop_config` it can take ~10 minutes for every alert to clear.
