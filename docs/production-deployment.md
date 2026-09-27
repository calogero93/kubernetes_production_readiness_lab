# Kubernetes deployment and observability

The Helm chart in `deploy/charts/kubeproof` deploys the history UI and the
non-executing AI CLI on one Pod. It uses a persistent volume for sealed bundles
and SQLite, a ClusterIP Service, health probes, non-root execution and no
Kubernetes API token. Keep the Service private: this version has no user
authentication. Use `kubectl port-forward` or an authenticated gateway.

The local live CPU workflow still creates a disposable kind cluster using Docker.
It is disabled on the Kubernetes Service. A portable, namespace-scoped execution
backend is on the roadmap; the chart does not claim to run live trials in-cluster.

A separate `probe-service` command runs a bounded HTTP Job inside any target
cluster. It exercises the Service DNS and virtual IP from a Pod, then reports
status counts and p95 latency. It does not measure backend distribution or CPU.

## Local kind

```bash
kind create cluster --name kubeproof-local
docker build -t kubeproof:local .
kind load docker-image kubeproof:local --name kubeproof-local
helm upgrade --install kubeproof deploy/charts/kubeproof \
  --namespace kubeproof-system --create-namespace \
  --set image.repository=kubeproof --set image.tag=local \
  --set image.pullPolicy=Never --wait
kubectl -n kubeproof-system port-forward svc/kubeproof 8000:80
```

Open `http://127.0.0.1:8000`. A default StorageClass must support a 2 GiB
ReadWriteOnce PVC. Set `persistence.storageClassName` when your cluster needs an
explicit class. The chart uses one replica because SQLite and the local bundle
store are single-writer. For an existing cluster, publish the image to a registry
that its nodes can pull, then use the same Helm command with
`image.repository` and `image.digest=sha256:...`. The image reference is pinned
to the digest when that value is set.

To test Service traffic from inside the cluster using the same image:

```bash
uv run kubeproof probe-service kubeproof \
  --namespace kubeproof-system --port 80 --path /healthz \
  --requests 10 --image kubeproof:local --context kind-kubeproof-local
```

The command creates one short-lived Job in the target namespace, collects its
JSON observation, and deletes it. The caller needs `get` on the Service,
`create/get/delete` on Jobs, `list` on Pods and `get` on Pod logs in that
namespace. The Job has no API token, no retries, a three-minute deadline and a
50-request cap. Use the published KubeProof image reference for other clusters.
Exit code `0` means all requests returned HTTP 200; `2` means observed failures;
`1` means the probe could not produce a valid observation.

## Release workflow

Pull requests and pushes to `main` run Python, frontend, Helm and container
checks. A `v*` tag reruns those checks and publishes the image to GHCR with
provenance and SBOM. Deployment is a separate `workflow_dispatch` action that
accepts the published SHA-256 digest and namespace. Configure the GitHub
`production` environment and its `KUBECONFIG_B64` secret before using it. The
credential must have the rights required by this namespaced chart; the workflow
can create the namespace if it does not exist. A private GHCR image also needs
an image pull secret configured on the target cluster. The workflow performs a
Helm `--atomic --wait` upgrade, so readiness failure rolls the release back.

## Model endpoint: local or vLLM

KubeProof uses the same OpenAI-compatible adapter for local servers and vLLM.
For a remotely reachable or in-cluster vLLM Service, terminate TLS at a trusted
gateway or service mesh and provide an API key. The model base URL includes
`/v1` and must be HTTPS unless it is loopback. For example:

```bash
kubectl -n kubeproof-system create secret generic kubeproof-model \
  --from-literal=KUBEPROOF_AI_API_KEY="$MODEL_API_KEY"
helm upgrade kubeproof deploy/charts/kubeproof -n kubeproof-system \
  --reuse-values \
  --set model.provider=openai_compatible \
  --set model.name="$MODEL_ID" \
  --set model.baseUrl=https://vllm.example.internal/v1 \
  --set model.existingSecret=kubeproof-model
kubectl -n kubeproof-system exec deploy/kubeproof -- kubeproof ai doctor
```

`ai doctor` checks `/v1/models` and the exact model ID without inference. It
does not prove that the selected model follows KubeProof's JSON schema. Run
`kubeproof ai draft` and `kubeproof ai plan` against representative confirmed
requests before relying on it. The model only proposes plans; deterministic
validation and operator approval govern execution. The chart does not deploy
vLLM or a TLS gateway. Size and deploy those components for the actual GPU,
model, ingress and certificate environment.

## Prometheus, Grafana and Langfuse

The application serves Prometheus metrics on `/metrics`: HTTP request counts and
latency, model-call outcomes and latency, and local live-run outcomes. Labels
use fixed route templates and operation names; evaluation IDs and prompts are
never metric labels. The Pod has Prometheus scrape annotations. If the cluster
uses Prometheus Operator, set `monitoring.serviceMonitor.enabled=true` and set
`monitoring.serviceMonitor.labels` to match the Prometheus selector. The CRD must
already exist. Import `deploy/grafana/kubeproof-dashboard.json` and select the
Prometheus data source. Prometheus and Grafana are external to this chart.

Langfuse tracing is opt-in. It captures model prompts and outputs, including
submitted descriptions and confirmed constraints, so point it at an approved
Langfuse instance with suitable retention and access controls. Install the
Langfuse server separately (its maintained Helm chart is appropriate for
Kubernetes). Configure its public and secret keys in a Kubernetes Secret and
enable the chart values:

```bash
kubectl -n kubeproof-system create secret generic kubeproof-langfuse \
  --from-literal=LANGFUSE_PUBLIC_KEY="$LANGFUSE_PUBLIC_KEY" \
  --from-literal=LANGFUSE_SECRET_KEY="$LANGFUSE_SECRET_KEY"
helm upgrade kubeproof deploy/charts/kubeproof -n kubeproof-system \
  --reuse-values \
  --set langfuse.enabled=true \
  --set langfuse.baseUrl=https://langfuse.example.internal \
  --set langfuse.existingSecret=kubeproof-langfuse
```

The application attaches a Langfuse callback to `ai draft` and `ai plan` model
calls only when all three Langfuse settings are present. Prometheus metrics stay
local to the process; Langfuse receives trace data over the configured URL.

## Current operational limits

- The UI is a single-operator history view. It has no authentication, tenant
  isolation, HA storage, or remote run queue.
- The in-process live job manager is available only on a loopback bind and
  still depends on Docker/kind. Deploying the chart does not make it portable.
- The Service probe is an independent CLI observation, not yet a sealed
  evaluation bundle or an AI planning capability. HPA and CNI or service-mesh
  experiments need separate, bounded designs.
- The CI jobs verify the chart and image build. A real rollout, external
  Prometheus scrape, Langfuse export and vLLM plan quality require environment
  specific integration checks and are tracked in the roadmap.
