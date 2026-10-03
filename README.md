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

## Continuous integration

The `Verify` workflow runs on pushes to `main`, pull requests and manual starts.
It validates the workflow and Compose configuration, runs Python lint, formatting,
type checks and tests, and builds and tests the frontend. Real Helm tests cover
the production chart and fixtures, persistence modes, model/tracing secrets and
ServiceMonitor wiring. An isolated production container checks the shipped CLI,
frontend assets, API, static inspection, frozen plan, sealed-bundle verification,
tamper detection and history import while running as non-root with a read-only
root filesystem. Python/frontend JUnit reports and service logs are retained as
GitHub artifacts for seven days. Superseded runs are cancelled and jobs have
explicit timeouts. There is currently no release or deployment workflow.

Run the same checks locally with Compose:

```bash
docker compose --profile checks config --quiet
docker compose run --rm workflow-checks
docker compose build checks frontend-checks image-checks
docker compose run --rm checks ruff check src/kubeproof tests scripts
docker compose run --rm checks ruff format --check src/kubeproof tests scripts
docker compose run --rm checks mypy --cache-dir /tmp/mypy src/kubeproof scripts
docker compose run --rm checks
docker compose run --rm frontend-checks
docker compose up -d --wait --wait-timeout 60 image-checks
docker compose exec -T image-checks python - < scripts/ci/smoke_image.py
docker compose rm --stop --force image-checks
```

The smoke container has no external network or Docker socket; its test data is
temporary. The existing live kind test remains opt-in. Model downloads and real
LLM inference are manual local experiments, outside the standard CI.

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

## Run a local model with llama.cpp

From a fresh clone, install Docker with Compose. The local lab currently requires
Linux. This CPU setup uses the official [llama.cpp server image](https://github.com/ggml-org/llama.cpp/blob/master/docs/docker.md)
and [Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507)
in the [LM Studio community Q4_K_M GGUF](https://huggingface.co/lmstudio-community/Qwen3-4B-Instruct-2507-GGUF)
conversion. The download is approximately 2.5 GB; leave additional RAM for the
context cache, Docker and kind. The tested CPU configuration uses eight threads
and a 16,384-token context; generation speed depends on your hardware.

```bash
cp .env.example .env
docker compose run --rm model-download
docker compose up -d llama-server
docker compose build local-lab
docker compose up -d local-lab
docker compose logs --tail=30 llama-server
docker compose run --rm local-lab kubeproof ai doctor
```

If you already have `.env`, add the values from `.env.example` instead of
overwriting it. The downloader fixes the model revision, size and SHA-256 using
`examples/ai/model.json`; it reuses an existing verified download. Model weights
live in the `kubeproof-models` Docker volume, outside Git. `.env` stays local.
The server exposes only `127.0.0.1:8080`; the lab accesses its `/v1` endpoint
through Linux host networking. `ai doctor` checks readiness and the model alias
`kubeproof-local` without generating text. If the model is still loading, retry
after checking its logs. CPU inference uses a 600-second HTTP timeout, configurable
with `KUBEPROOF_AI_TIMEOUT_SECONDS` (5–600 seconds). On the tested CPU, the first
three-probe proposal took about four minutes; readiness alone does not test
generation speed or plan quality.

Package the HTTP fixture and ask the model for a small fixed plan:

```bash
docker compose run --rm local-lab helm package \
  /workspace/examples/charts/http-fixture --destination /data
docker compose run --rm local-lab kubeproof inspect /data/http-fixture-0.1.0.tgz \
  --profile /workspace/examples/profiles/enterprise-strict.yaml \
  --ai-plan \
  --objective "Probe the kubeproof-target HTTP Service on port 8080 with 5 requests, sample resources for 10 seconds, and observe retained DNS. Use only these three probes, with no Pod recovery." \
  --plan-max-tasks 3 --plan-max-seconds 120 --plan-parallelism 1 \
  --output /data/ai-preflight
docker compose run --rm local-lab cat /data/ai-preflight/artifacts/plan/plan.json
```

Review the task targets, parameters, dependencies and budget before execution.
Planning performs static inspection only. Execute exactly the saved plan and
request an interpretation afterward:

```bash
docker compose run --rm local-lab kubeproof inspect /data/http-fixture-0.1.0.tgz \
  --profile /workspace/examples/profiles/enterprise-strict.yaml \
  --plan /data/ai-preflight/artifacts/plan/plan.json \
  --execute-known-chart --ai-interpret \
  --output /data/ai-evaluation --history-dir /data
docker compose run --rm local-lab cat /data/ai-evaluation/artifacts/ai/interpretation.json
docker compose run --rm local-lab kubeproof verify /data/ai-evaluation
```

A strict-profile violation makes `inspect` exit with code `2`; examine the bundle
to distinguish that outcome from a tool error. Invalid AI plans are rejected.
An inference or interpretation failure is not evidence about the product: check
`ai_interpretation.status` and the saved error. Explanations must cite existing
checks or observations; hypotheses and manifest suggestions are separate from
measured findings. Suggestions are never applied automatically. In **Review chart**
you can also request an AI proposal, review its frozen plan and enable final
interpretation using the same local server.

You can request fresh commentary on an existing sealed bundle without repeating
the runtime tests:

```bash
docker compose run --rm local-lab python /workspace/scripts/ai/interpret_bundle.py \
  /data/ai-evaluation --output /data/ai-commentary.json
docker compose run --rm local-lab cat /data/ai-commentary.json
```

This script verifies the source seal, checks every referenced evidence ID and
writes a separate commentary file with the source evaluation ID, bundle digest
and selected model. It preserves the original bundle and refuses to overwrite
an existing commentary file. The commentary is not a new measurement or a sealed
evaluation bundle and is not imported into the UI history automatically.

The first real fixture run passed installation and all five HTTP requests.
The ten-second resource window and retained DNS observation were inconclusive;
absence of a complete metrics sample or external queries is not a product pass.
The first AI interpretation was rejected for ungrounded recommendations and
manifest suggestions inside hypotheses. Such failures remain recorded; enforcing
the response contract does not establish that every accepted explanation is
factually correct. Review the evidence and verify proposed changes in a new run.
After clarifying the response rules, a second interpretation of the same sealed
evaluation produced six valid evidence-linked points in about four minutes.
Manual review still found a memory-unit error and an overstatement about the
availability provided by two declared replicas. Treat accepted AI commentary as
a review aid and check its claims against the underlying observations.

Stop inference with `docker compose stop llama-server`. Downloaded weights remain
in the Docker volume. `docker compose down -v` deletes the model and evidence
volumes, so use it only when deliberately discarding that data.

## Kubernetes deployment and Service probe

The production `Dockerfile` target runs as a non-root user. The Helm chart is
under `deploy/charts/kubeproof` for a future manually managed deployment. Release
and deploy automation are currently disabled. The deployed UI is a private,
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
- [x] Run Python, frontend, Helm and production-image smoke checks in CI.
- [ ] Reintroduce image publishing and digest-pinned deployment when a target
  environment is available.
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
