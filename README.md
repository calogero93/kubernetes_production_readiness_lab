# KubeProof

The composable probe baseline uses the same frozen plan and executor for
deterministic and AI-proposed evaluations.

KubeProof qualifies Kubernetes products against explicit organization constraints
and preserves the observations that support every finding.

The default inspection renders a Helm chart, inspects its security, RBAC,
resource and endpoint footprint, applies the local safety admission policy, and
writes a JSON/Markdown evidence bundle. An explicit flag also runs a known chart
in a disposable kind cluster. Neither path requires an LLM.

```bash
uv sync --extra dev
uv run kubeproof inspect oci://registry.example/product \
  --version 1.2.3 \
  --profile examples/profiles/enterprise-strict.yaml \
  --output ./kubeproof-run
```

The command exits with `2` when deterministic profile violations are found,
`1` for tool/input errors, and `0` otherwise. An admission result of
`static_only` means runtime tests did not execute. For the four local host-access
rules below, an operator can explicitly approve an exact input for a later kind
run. It is not itself a product failure.

To run a deliberately selected, pinned chart in a disposable kind cluster:

```bash
uv run kubeproof inspect examples/corpus/charts/cert-manager-v1.21.2.tgz \
  --values examples/corpus/cert-manager-kind.yaml \
  --profile examples/profiles/enterprise-strict.yaml \
  --output ./kubeproof-run \
  --execute-known-chart
```

Docker, kind, kubectl and Helm must be available. The command records
installation, readiness, sampled resources, Deployment Pod replacement and DNS
observations, then uninstalls the release and deletes its kind cluster. Runtime
execution accepts only a local `.tgz` chart archive. See the pinned
[reference corpus](examples/corpus/README.md).

For a chart that matches only `KP-SAFE-002`–`005` (`hostNetwork`, `hostPID`,
`hostIPC`, `hostPath`), first run the command without `--execute-known-chart`.
Review the findings and copy the printed approval scope SHA-256. Then run the
same chart, profile, values, release and namespace with:

```bash
uv run kubeproof inspect ./selected-chart.tgz \
  --profile examples/profiles/enterprise-strict.yaml \
  --output ./approved-run \
  --execute-known-chart \
  --approve-local-risk SCOPE_SHA256_FROM_PREFLIGHT \
  --operator-label lab-operator \
  --approval-reason "Approved local host-access test"
```

The scope binds the chart package, rendered manifest, profile, ordered values,
release, namespace and matched rules, plus HTTP probe parameters when requested.
A mismatch stops before kind starts.
Privileged containers, unmasked proc mounts and dangerous added capabilities
remain non-approvable locally. The label is self-declared; the local UI does not
authenticate the operator. Approved host categories may also appear in dynamic
product Pods; new unapproved categories still cancel the run. kind shares the
Docker host kernel, so use a deliberately selected chart on a machine dedicated
to the test.

## Local lab with Docker Compose

On Linux with a running Docker daemon, build and start the CLI/UI environment:

```bash
docker compose build local-lab
docker compose up -d local-lab
```

Open `http://127.0.0.1:8000` and use **Review chart** for generic `.tgz`
preflight and explicit execution approval. The same container supplies Helm,
kind, kubectl and the Docker client for CLI evaluations:

```bash
docker compose run --rm local-lab kubeproof inspect \
  /workspace/examples/corpus/charts/kube-prometheus-stack-91.4.1.tgz \
  --profile /workspace/examples/profiles/enterprise-strict.yaml \
  --output /data/host-preflight
docker compose run --rm local-lab cat /data/host-preflight/report.md
```

To execute after review, repeat the same `inspect` command with a new output
path, `--execute-known-chart`, and the three approval flags shown above.
Chart/profile files are read-only under `/workspace`; bundles and history live
in the persistent `kubeproof-data` volume. `docker compose down` stops the UI.
The local-lab service uses host networking and mounts the Docker socket so kind
can create disposable clusters; this grants broad control over the host Docker
daemon. Use it only on a workstation dedicated to this test. This Compose
profile is Linux-specific; the normal host CLI path remains available elsewhere.

## Local evaluation history

Build the React frontend once, then import a completed bundle into the local
immutable store and SQLite index and start the single-user history UI:

```bash
cd frontend
npm ci
npm run build
cd ..
uv run kubeproof history import ./kubeproof-run --data-dir ./.kubeproof
uv run kubeproof serve --data-dir ./.kubeproof
```

Open `http://127.0.0.1:8000`. The **Review chart** view accepts a generic `.tgz`,
optional values YAML and a profile JSON. It performs static preflight before
showing an explicit execution decision and records any host-access approval in
the evaluation bundle. The history page shows each evaluation and drills
down from checks to findings and their supporting observations. For live
frontend development, see [frontend/README.md](frontend/README.md). A new inspection
can be indexed immediately with `--history-dir`:

```bash
uv run kubeproof inspect examples/charts/demo \
  --profile examples/profiles/enterprise-strict.yaml \
  --output ./kubeproof-run-2 \
  --history-dir ./.kubeproof
```

The filesystem bundles remain authoritative. SQLite is a WAL-backed query
projection and can be rebuilt at any time:

```bash
uv run kubeproof history sync --data-dir ./.kubeproof
```

New bundles include file and observation digests. Check one offline with:

```bash
uv run kubeproof verify ./kubeproof-run
```

Compare two sealed evaluations under the documented repeatability tolerances:

```bash
uv run kubeproof compare ./first-run ./second-run
```

Bundle digests detect changes relative to the recorded seal; they do not
authenticate the operator or provide an independent signature.

Source modules are grouped into `core`, `execution`, `evidence`, `application`,
`intelligence` and `interfaces` under `src/kubeproof`; `tests/` mirrors those
responsibilities.
Every push and pull request runs Python lint, formatting, type checks and tests,
plus the frontend test and typechecked build, in GitHub Actions.

## Controlled CPU benchmark

The [CPU HTTP fixture](src/kubeproof/benchmarks/cpu_http/README.md) provides a
bounded local service and fixed-rate load driver. Its Helm chart is under
`examples/charts/cpu-fixture`. The UI can now package and upload this chart,
confirm requirements, and—after explicit approval—run real load against one
selected Pod in a disposable kind cluster. Load is supported only for the
reviewed CPU fixture contract, with bounded request rate and duration.

## Composed plans and AI

Plans can compose HTTP Service probes, CPU fixture load, resource sampling,
retained DNS observation and Pod recovery. They support fixed dependencies,
conditions and bounded concurrency. Storage and further fault-injection probes
remain future catalog additions.

Use `kubeproof catalog` to inspect the schema and `inspect --plan plan.json` to
review or execute a composition. The local **Review chart** UI accepts the same
plan JSON. [HTTP](examples/plans/http-composition.json) and
[CPU + HTTP](examples/plans/cpu-and-http.json) examples are included.

`inspect --ai-plan --objective ...` proposes a plan during static preflight;
execution uses the reviewed plan file and never replans during the run.
`--ai-interpret` optionally adds evidence-linked explanations, hypotheses and
manifest suggestions afterward. Suggestions do not alter findings or apply
configuration changes. Set `KUBEPROOF_AI_PROVIDER=llama_cpp` or
`openai_compatible`, `KUBEPROOF_AI_MODEL`, and `KUBEPROOF_AI_BASE_URL` to the
server's `/v1` endpoint. Remote endpoints also require `KUBEPROOF_AI_API_KEY`.
The host CLI needs the Python `ai` extra; the Compose lab includes it.

The earlier `ai schema`, `ai draft`, `ai plan` commands and adaptive CPU graph
remain a separate prototype and never execute through those preview commands.

## Kubernetes deployment and Service probe

The production `Dockerfile` target runs as a non-root user. The Helm chart is
under `deploy/charts/kubeproof`; GitHub workflows publish GHCR images and support
manual digest-pinned deployment. The deployed UI is currently a private,
single-operator history service. It exposes Prometheus metrics, supports an
optional Prometheus Operator ServiceMonitor and an importable Grafana dashboard.
Optional Langfuse callbacks trace AI model calls; the same OpenAI-compatible
adapter connects to a TLS-protected vLLM endpoint. The CLI's `probe-service`
command runs bounded HTTP traffic *from a Job inside Kubernetes* through a
selected Service and reports observed status counts and p95 latency.

General local evaluations can now include the probe with `inspect --http-service`
or **Include an HTTP Service probe** in **Review chart**. Target, requirements,
per-request timings and findings are saved in the sealed bundle and history.
The test chart is under `examples/charts/http-fixture`. HTTP latency describes
traffic generated by the probe inside the cluster, with its configured target
and rate; it is not a guarantee about production client traffic.

## Roadmap

- [x] Package the history service as a non-root container and portable Helm
  chart; verify a local kind rollout and Service endpoints.
- [x] Run Python, frontend, Helm and image checks in CI; publish GHCR images
  with provenance on tags and deploy a reviewed digest manually.
- [x] Expose low-cardinality Prometheus metrics, Grafana dashboard and opt-in
  Langfuse model traces; add model discovery for OpenAI-compatible/vLLM servers.
- [x] Add a bounded, in-cluster HTTP Service probe with measured status and p95.
- [x] Integrate the Service probe into sealed evidence bundles with cluster,
  local probe-image fingerprint, endpoint, timings and explicit acceptance criteria.
- [ ] Replace Docker/kind coupling for live evaluations with a namespace-scoped
  Kubernetes runner, dedicated RBAC, durable queue and recovery after Pod restart.
- [ ] Add an HPA experiment: generate bounded load, observe metrics, desired and
  ready replicas over time, then verify scale-up, scale-down and latency limits.
- [ ] Add CNI/NetworkPolicy and service-mesh cases: use isolated source and
  destination Pods to test declared allow/deny paths, mTLS and routing behavior;
  report unsupported enforcement distinctly from a product failure.
- [ ] Add model plan-quality evaluations against a versioned dataset for local,
  on-prem and vLLM endpoints; track schema compliance and unsafe proposals.
- [ ] Add authenticated multi-user access, external durable storage and a
  production run queue before exposing the UI beyond trusted operators.
