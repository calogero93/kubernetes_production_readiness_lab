# Milestone 2 — Evidence

Milestone 2 turns a useful evaluation into an *auditable* evaluation. It does
not change the product thesis or introduce an AI decision-maker: deterministic
checks still decide findings from observations. The acceptance criteria are in
[architecture.md](architecture.md); the precise bundle and repeatability rules
are in [evidence.md](evidence.md).

## What changed from Milestone 1

| Concern | Milestone 1 — Proof | Milestone 2 — Evidence |
|---|---|---|
| Main question | Can selected products be evaluated safely, with supported findings? | Can a reviewer validate and compare the evidence after the cluster is gone? |
| Result | Typed checks, observations, findings, explicit untested outcomes | Same result, with schema 0.2 identity, provenance and experiment versions |
| Source classes | Static and runtime facts are distinguished | Source class is validated against each producing check; provenance identifies manifest or runtime cluster |
| Input identity | Chart/profile options are recorded | Chart package, rendered manifest, ordered values and `--set` digests, plus normalized profile digest |
| Environment | Disposable kind, bounded runtime and cleanup | Tool/platform fingerprint, Kubernetes server version and pinned Metrics Server manifest digest |
| Bundle | JSON, Markdown, redacted manifest and runtime artifacts | `bundle-manifest.json` seals every file and serialized observation with SHA-256 |
| Validation | Review the produced report | `kubeproof verify` checks hashes, source relationships, profile and deterministic report offline |
| History | Bundle is authoritative | SQLite is only a rebuildable local query index; import and reading verify sealed bundles |
| Repeatability | Static facts can be rerun and compared manually | `kubeproof compare` compares two verified bundles with explicit runtime tolerances |

The result schema remains readable for historical 0.1 bundles, but those are
marked *legacy, unsealed*. Verification cannot retroactively prove their
integrity. The current 0.2 bundle layout is:

```text
run/
├── evaluation.json                 # typed result
├── bundle-manifest.json            # file and observation digests
├── report.md                       # deterministic view of evaluation.json
├── profile.normalized.json
├── input/rendered-manifests.redacted.yaml
└── artifacts/                     # optional runtime evidence
```

## How the evidence chain works

1. The inspection pins the identifiable inputs and records their digests.
2. A check emits observations and any findings reference those observation IDs.
3. Each observation records a source class, source reference and producing
   check; each check has an experiment version.
4. The bundle writer renders the report from the typed result, hashes all
   included files and observations, and publishes the completed directory.
5. Offline verification rechecks the hashes, model consistency, provenance and
   report. History import uses the same verification path.

This detects accidental alteration or partial corruption. The manifest is **not
a signature**: a party able to replace both content and manifest can produce a
new internally consistent bundle. The original chart and values files are
identified by digest, not copied; the rendered manifest in the bundle is
redacted. Runtime provenance currently identifies the check and cluster, not
always the exact Kubernetes API response. These are explicit limits, not hidden
claims of stronger authenticity.

## Repeatability and the CPU decision

For identical inputs, static facts, findings and admission must match exactly.
Runtime check status and assessment must also match. Timing, memory and sampled
CPU are measurements, so `compare` uses the documented review tolerances.
For CPU peaks the permitted difference is the larger of 25% of the higher peak
and an absolute allowance capped by both 20 millicores and 1% of the profile's
`max_sampled_cpu_per_pod`. Without that profile limit, numeric CPU comparison is
inconclusive. **This tolerance applies only between runs**: one complete CPU
sample above the profile limit still produces the existing blocker finding.

The first cert-manager pair exposed a failure of the original relative-only CPU
criterion. That negative result remains in [evidence.md](evidence.md). The
revised rule was fixed *before* new runtime samples were collected. Subsequent
cert-manager and Argo CD pairs passed `kubeproof compare` with stable check
outcomes. Network remained `inconclusive` in those runs: repeatability does not
turn an inconclusive product assessment into a pass. These examples validate
the criterion for the tested environment and products, not a universal bound
on Kubernetes CPU sampling.

## Reproduce the checks

```bash
uv run kubeproof verify ./run
uv run kubeproof compare ./first-run ./second-run
uv run kubeproof history import ./run --data-dir ./.kubeproof
uv run kubeproof history sync --data-dir ./.kubeproof
```

`verify` needs no cluster. `compare` accepts only sealed bundles and exits `0`
for repeatable, `2` for differences, and `1` for invalid or inconclusive input.
See [decisions.md](decisions.md) for the recorded design decisions and
[evidence.md](evidence.md) for the exercised corpus and measured values.

The React frontend, CI workflow and source/test folder reorganization were
added after this milestone to make the completed evidence capabilities easier
to use and maintain; they are not additional evidence acceptance criteria.
