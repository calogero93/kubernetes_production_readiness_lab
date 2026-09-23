# KubeProof v0.1 Decision Log

This is a lightweight decision log for the current implementation. The earlier
ADR directory is retained as historical design exploration.

## Accepted decisions

### D-001 — Conservative truth contract

- Status: Accepted
- Date: 2026-09-21
- Requirement: produce useful findings without overstating evidence.
- Decision: use the semantics in [`truth-contract.md`](truth-contract.md),
  including independent execution/assessment axes and scoped wording.
- Alternatives: optimistic prose or generic pass/fail statuses.
- Why: credibility is the product's primary differentiator.
- Reversal criterion: none for evidence grounding; individual claim scopes may
  expand only when a new collector/experiment supplies the missing evidence.

### D-002 — Kubernetes Metrics API for initial resource observations

- Status: Accepted
- Date: 2026-09-21
- Requirement: provide useful observed CPU/memory data without deploying a full
  monitoring stack.
- Decision: install pinned Metrics Server and collect timestamped CPU/memory
  samples through the Metrics API at explicit 15-second resolution.
- Alternatives: static-only reporting, direct Kubelet metrics, cAdvisor scraping,
  Prometheus.
- Trade-off: simple and Kubernetes-native, but it can miss transient peaks and is
  unsuitable as a long-term monitoring store.
- Reversal criterion: a validated need for sub-resolution startup peaks,
  historical queries, percentiles or metrics beyond CPU/memory.

### D-003 — Initial real-product corpus

- Status: Accepted
- Date: 2026-09-21
- Requirement: validate usefulness against products with meaningfully different
  Kubernetes footprints.
- Decision: use pinned releases of cert-manager, Argo CD and
  kube-prometheus-stack as the first public evaluation corpus.
- Coverage: CRDs/webhooks and RBAC; multi-component controllers and cluster-wide
  permissions; resource-heavy monitoring and host-level components.
- Special handling: evaluate kube-prometheus-stack's default rendered footprint;
  if local safety policy blocks runtime execution, also evaluate a documented
  values configuration that disables the blocking component rather than weakening
  safety admission.
- Reversal criterion: replace a product only if it is unavailable, cannot be
  pinned/reproduced, or fails to exercise the intended check area.

### D-004 — Minimal YAML company profile

- Status: Accepted
- Date: 2026-09-21
- Requirement: represent organization constraints without building a policy
  language or mixing them with test configuration.
- Decision: use the strict, versioned YAML schema in [`profile.md`](profile.md).
  Omitted fields define no company constraint. Unknown fields fail validation.
- Boundary: Helm values, timeouts, Kubernetes version, selectors and collection
  windows are execution options persisted separately in the evaluation result.
- Alternatives: Python DSL, profile inheritance/composition, one mixed run file.
- Reversal criterion: introduce composition or a separate evaluation-spec file
  only after real profiles or CLI invocations demonstrate repeated duplication.

### D-005 — Pragmatic local kind admission policy

- Status: Accepted
- Date: 2026-09-21
- Requirement: inspect realistic Kubernetes products without presenting kind as
  a hostile-code sandbox.
- Decision: direct host-access primitives such as privileged mode, host
  namespaces, hostPath, unmasked proc and high-impact capabilities force a
  `static_only` result. Broad cluster-scoped RBAC, wildcard permissions and
  cluster-admin bindings remain executable only for explicitly acknowledged,
  known products and remain visible as high-severity observations/findings.
- Trade-off: this admits useful controller products but a broadly privileged
  controller can create resources absent from the original Helm render.
- Reversal criterion: unknown inputs, shared execution, privileged/CNI/CSI
  runtime testing, or evidence that products create unsafe workloads before
  bounded monitoring can intervene requires disposable-VM isolation or a more
  restrictive admission rule.

### D-006 — Local history as a rebuildable projection

- Status: Accepted
- Date: 2026-09-22
- Requirement: browse multiple evaluations and explain every check outcome while
  retaining portable, independently verifiable evidence bundles.
- Decision: keep immutable filesystem bundles authoritative and maintain a
  single-user SQLite/WAL index for listing and summary queries. Validate and copy
  bundles atomically under their evaluation ID; make imports idempotent and allow
  the index to be rebuilt from stored bundles. Serve the local UI and JSON API
  without authentication on loopback by default.
- Boundary: storage and indexing are separate ports. A future remote deployment
  may use an OCI registry for immutable bundles and PostgreSQL for the query
  projection; tags aid discovery, while immutable manifest digests identify
  bundle content.
- Alternatives: SQLite as the only source of truth would simplify local reads but
  couple evaluation success to index health and weaken export/recovery. Building
  PostgreSQL, registry and authentication adapters now would add unused services.
- Reversal criterion: remote operation, multiple writers or shared access justifies
  the PostgreSQL/OCI adapters and an authenticated service boundary.

### D-007 — Indicative CPU samples in repeatability comparisons

- Status: Accepted
- Date: 2026-09-23
- Requirement: test whether repeated runtime evaluations support stable outcomes
  without treating low-load Metrics API CPU samples as exact measurements.
- Decision: runtime check execution status and assessment must match. For
  comparable complete Pod-group samples, the difference between sampled CPU
  peaks may not exceed the larger of 25% of the larger peak and the smaller of
  20 millicores or 1% of the profile's sampled-CPU limit. Without that profile
  limit, numeric CPU repeatability is inconclusive. This comparison rule does
  not alter the product's sampled-CPU constraint or suppress a blocker.
- Why: a relative-only tolerance is overly sensitive near zero, while the
  bounded absolute allowance keeps differences small relative to the declared
  policy limit. The first relative-only comparison failed; two new pinned
  product pairs passed after the revised rule was fixed in advance.
- Reversal criterion: repeated independent pairs outside this range, or
  evidence that the allowance hides materially different resource behavior,
  requires a revised sampling experiment and a new predeclared criterion.

## Current default recommendations, not yet accepted

| Decision | Default | Reversal signal |
|---|---|---|
| Persistence | Filesystem bundles + rebuildable SQLite index | Remote/multi-writer operation |
| Scenarios | Five internal checks in an explicit list | Independently packaged third-party scenario is requested |
| AI topology | One Reasoner in Milestone 3 | Specialization materially improves controlled evals |
| Agent framework | None initially | A concrete orchestration responsibility is demonstrably costly |
| MCP | Selected external adapter only | Two AI clients need the same stable capability |
| Isolation | Known products in kind; unsafe inputs static-only | Unknown/privileged/shared execution is required |

## Next decision

Define the runtime environment lifecycle and fingerprint required before the
first kind-based installation experiment is added.
