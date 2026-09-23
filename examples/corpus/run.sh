#!/usr/bin/env bash
set -uo pipefail

corpus_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(cd "${corpus_dir}/../.." && pwd)"
output_dir="${2:-${project_dir}/kubeproof-corpus-runs}"
mode="${1:-static}"

if [[ "$mode" != "static" && "$mode" != "runtime" ]]; then
  printf 'usage: %s [static|runtime] [output-directory]\n' "$0" >&2
  exit 1
fi

cd "$corpus_dir" || exit 1
sha256sum -c SHA256SUMS || exit 1
mkdir -p "$output_dir" || exit 1

run_chart() {
  local name="$1"
  local chart="$2"
  shift 2
  local -a command=(
    uv run --project "$project_dir" kubeproof inspect
    "$corpus_dir/charts/$chart"
    --profile "$project_dir/examples/profiles/enterprise-strict.yaml"
    --output "$output_dir/$name"
    "$@"
  )
  "${command[@]}"
  local status=$?
  if [[ $status -ne 0 && $status -ne 2 ]]; then
    printf '%s failed with exit code %s\n' "$name" "$status" >&2
    return "$status"
  fi
}

if [[ "$mode" == "static" ]]; then
  run_chart cert-manager-default cert-manager-v1.21.2.tgz || exit 1
  run_chart argo-cd-default argo-cd-10.9.1.tgz || exit 1
  run_chart kube-prometheus-stack-default kube-prometheus-stack-91.4.1.tgz || exit 1
  run_chart kube-prometheus-stack-local kube-prometheus-stack-91.4.1.tgz \
    --values "$corpus_dir/kube-prometheus-stack-local.yaml" || exit 1
else
  run_chart cert-manager-kind cert-manager-v1.21.2.tgz \
    --values "$corpus_dir/cert-manager-kind.yaml" --execute-known-chart \
    --install-timeout 300 --steady-state-window 60 --max-recovery-targets 2 || exit 1
  run_chart argo-cd argo-cd-10.9.1.tgz \
    --execute-known-chart --install-timeout 360 \
    --steady-state-window 60 --max-recovery-targets 3 || exit 1
  run_chart kube-prometheus-stack-local kube-prometheus-stack-91.4.1.tgz \
    --values "$corpus_dir/kube-prometheus-stack-local.yaml" --execute-known-chart \
    --install-timeout 600 --steady-state-window 60 --max-recovery-targets 2 || exit 1
fi
