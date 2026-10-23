#!/usr/bin/env bash
# Creates or deletes the kind lab. Every command pins the lab's kube context so other clusters are never touched.
set -euo pipefail
cd "$(dirname "$0")"

CLUSTER=opsteam-lab
CTX="kind-$CLUSTER"
IMAGE=opsteam-lab/demo-app:dev
PROMETHEUS_CHART=29.37.1
LOKI_CHART=7.5.0
ALLOY_CHART=1.14.2

need() {
  local missing=""
  for t in "$@"; do command -v "$t" >/dev/null 2>&1 || missing="$missing $t"; done
  if [ -n "$missing" ]; then echo "missing tools:$missing" >&2; exit 1; fi
}

chart() { # release repo chart version values
  helm upgrade --install "$1" "$3" --repo "$2" --version "$4" --kube-context "$CTX" \
    --namespace monitoring --create-namespace -f "observability/$5" --wait --timeout 10m
}

up() {
  need docker kind kubectl helm
  kind get clusters | grep -qx "$CLUSTER" || kind create cluster --config kind.yaml --wait 120s
  docker build -t "$IMAGE" demo-app
  kind load docker-image "$IMAGE" --name "$CLUSTER"
  chart prometheus https://prometheus-community.github.io/helm-charts prometheus "$PROMETHEUS_CHART" prometheus-values.yaml
  chart loki https://grafana.github.io/helm-charts loki "$LOKI_CHART" loki-values.yaml
  chart alloy https://grafana.github.io/helm-charts alloy "$ALLOY_CHART" alloy-values.yaml
  kubectl --context "$CTX" apply -k base
  # Pods built from a new local image keep the same tag, so restart them to pick it up.
  kubectl --context "$CTX" -n shop rollout restart deployment frontend checkout payment loadgen
  kubectl --context "$CTX" -n shop rollout status deployment --timeout 5m
  echo "lab ready: frontend http://127.0.0.1:30080  prometheus http://127.0.0.1:30090  alertmanager http://127.0.0.1:30093  loki http://127.0.0.1:30100"
}

fault() {
  need kubectl
  if [ -z "${1:-}" ] || [ ! -f "faults/$1/kustomization.yaml" ]; then
    echo "unknown fault '${1:-}'; one of: $(ls faults | tr '\n' ' ')" >&2
    exit 2
  fi
  kubectl --context "$CTX" apply -k "faults/$1"
}

# The baseline is the healthy state, so healing any fault is re-applying it.
heal() {
  need kubectl
  kubectl --context "$CTX" apply -k base
  kubectl --context "$CTX" -n shop rollout status deployment --timeout 5m
}

down() {
  need kind
  kind delete cluster --name "$CLUSTER"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  fault) fault "${2:-}" ;;
  heal) heal ;;
  *) echo "usage: $0 up|down|fault <name>|heal" >&2; exit 2 ;;
esac
