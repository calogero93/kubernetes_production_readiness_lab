# KubeProof

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
`static_only` means runtime tests must not execute in the local kind environment;
it is not itself a product failure.

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
execution accepts only a local `.tgz` chart archive and never bypasses a
`static_only` safety decision. See the pinned [reference corpus](examples/corpus/README.md).

Current design decisions and truth semantics live in [`docs/v0.1`](docs/v0.1).

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

Open `http://127.0.0.1:8000`. The history page shows each evaluation and drills
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

See the [evidence bundle contract](docs/v0.1/evidence.md) for provenance,
integrity limits and runtime reproducibility criteria.
The [Milestone 2 guide](docs/v0.1/milestone-2.md) compares the Evidence milestone
with Milestone 1 and explains the verification workflow.

Source modules are grouped into `core`, `execution`, `evidence`, `application`
and `interfaces` under `src/kubeproof`; `tests/` mirrors those responsibilities.
Every push and pull request runs Python lint, formatting, type checks and tests,
plus the frontend test and typechecked build, in GitHub Actions.
