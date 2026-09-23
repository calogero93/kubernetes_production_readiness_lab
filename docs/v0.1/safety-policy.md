# KubeProof v0.1 Local Safety Policy

| Field | Value |
|---|---|
| Status | **Accepted** |
| Scope | Milestone 1 execution on a developer-owned kind environment |
| Last updated | 2026-09-21 |
| Accepted | 2026-09-21 |

## 1. Purpose

This policy answers:

> May KubeProof execute this rendered product on the local kind environment, or
> must it stop after static analysis?

It is independent from the company profile. A company may allow a capability
that KubeProof still refuses to execute locally. Conversely, KubeProof may safely
execute a product that violates company policy so it can collect evidence about
that violation.

## 2. Supported threat model

Milestone 1 supports products intentionally selected by the local user from a
known source. It is designed to contain ordinary product defects and accidental
misconfiguration, not adversarial containers or supply-chain compromise.

kind provides a disposable Kubernetes cluster but shares the host kernel through
containerized nodes. It is not a hostile-code sandbox. The evaluation cluster
therefore contains no production secrets, cloud credentials, source repositories
or unrelated workloads.

Unknown submissions, deliberately malicious inputs and products that require
direct host-level execution need future disposable-VM isolation.

## 3. Admission outcomes

Preflight returns one outcome:

| Outcome | Meaning |
|---|---|
| `admit` | The known product may execute under the supported local policy |
| `static_only` | Preserve static observations/findings but do not start product workloads |
| `reject_invalid` | The input cannot be safely acquired, rendered or parsed |

`static_only` is a valid evaluation result, not an infrastructure failure.
Checks requiring runtime evidence become `not_tested` with the matched safety
rule IDs.

There is no general `--force-unsafe-local` flag in v0.1. The user changes product
values to remove the unsafe feature, uses static analysis, or later selects a
stronger isolation provider.

## 4. Input declaration

Runtime execution requires an explicit declaration that the input is a known,
intentionally selected product. Interactive use can request confirmation; CI
must provide a recorded non-interactive acknowledgement.

The declaration does not suppress analysis or upgrade trust automatically. Any
hard rule below still produces `static_only`.

KubeProof records:

- original and resolved chart references;
- chart version and package digest where available;
- Helm values digests;
- rendered-manifest digest;
- referenced image names/tags;
- runtime image IDs/digests after pull, where Kubernetes reports them.

Mutable image tags remain a reproducibility limitation in Milestone 1.

## 5. Acquisition and rendering

Before cluster creation, KubeProof:

- invokes a pinned Helm binary through a typed argument-vector adapter;
- disables arbitrary Helm plugins and post-renderers;
- uses a dedicated temporary Helm/cache/config directory;
- applies acquisition, render-time and output-size timeouts;
- rejects path traversal or resources outside the bounded workspace;
- safely parses all rendered YAML documents;
- enforces internal limits for rendered bytes, document count and workload count;
- inventories normal resources and Helm hooks before any hook runs.

Initial numeric limits are selected after rendering the three reference products
so they reject pathological inputs without accidentally excluding the corpus.
They are KubeProof safety constants, not company-profile settings.

Malformed archives, unparseable output, invalid Kubernetes object structure or
limit violations produce `reject_invalid` with retained diagnostic artifacts.

## 6. Hard local stop rules

Any rendered product workload or Helm hook with one of these features produces
`static_only`:

- privileged containers;
- `hostPID`, `hostIPC` or `hostNetwork`;
- hostPath volumes;
- host device/runtime socket access;
- unmasked proc mounts;
- host-level runtime configuration;
- added high-impact capabilities such as `SYS_ADMIN`, `SYS_MODULE`, `BPF`,
  `PERFMON`, `SYS_PTRACE`, `NET_ADMIN` or `NET_RAW`;
- a resource type not understood by preflight that can plausibly execute code or
  mutate node/control-plane behavior.

The exact capability list is versioned with the policy. A future isolated
environment may support some of these features; local kind v0.1 does not.

Running as UID 0, writable root filesystems, absent requests/limits and
`allowPrivilegeEscalation` not being explicitly false are serious product
findings but do not independently stop a known product from local execution.
They do not directly grant the host access listed above, and blocking all of them
would prevent KubeProof from measuring common compatibility failures.

## 7. RBAC policy

Cluster-scoped RBAC, wildcard roles and bindings to cluster-admin are not an
automatic local stop for an explicitly known product. They are high-severity
static observations and may become company-profile blockers.

This is a deliberate pragmatic exception: products such as GitOps controllers
often request broad authority, and qualifying that authority is part of the
product value. The residual risk is explicit: a broadly privileged controller
could create workloads that were not present in the Helm render.

Mitigations in v0.1:

- accept only explicitly known products;
- use a fresh empty cluster and temporary kubeconfig;
- supply no external Git repositories, cluster secrets or application inputs;
- watch newly created workload specifications during the run;
- terminate the evaluation if a dynamically created workload matches a hard stop
  rule before KubeProof intentionally interacts with it, where admission timing
  permits;
- record that broad RBAC weakens the local safety boundary.

These controls are not equivalent to hostile-code containment. If tests show
that useful products dynamically create unsafe workloads before KubeProof can
intervene, the policy reverses to static-only or requires stronger isolation.

## 8. Helm hooks, CRDs and webhooks

Helm hooks are rendered and evaluated by the same rules as ordinary workloads.
Safe hooks may execute because cert-manager and other real charts rely on them.
A hard-stop feature in any required hook makes the evaluation static-only.

CRDs and in-cluster admission/conversion webhooks are allowed for known products
and recorded. External webhook URLs, unsupported executable extension types or
webhooks that target KubeProof infrastructure trigger static-only until a narrow
rule exists.

Finalizers and webhook failure policies are captured because they can block
cleanup. Cleanup failure does not delete prior evidence; the disposable cluster
is destroyed as the recovery boundary.

## 9. Runtime controls

Before product installation KubeProof creates:

- a fresh uniquely named kind cluster;
- a dedicated product namespace where the chart permits it;
- a temporary kubeconfig isolated from the user's normal kubeconfig;
- fixed experiment and overall evaluation deadlines;
- infrastructure health probes;
- bounded log/event/artifact collection;
- Metrics Server and DNS instrumentation from pinned inputs.

The runner monitors Kubernetes API health, node readiness, Pod/object growth and
node pressure. Unexpected explosive growth, infrastructure degradation or a new
hard-stop workload cancels remaining experiments and proceeds to cleanup.

Milestone 1 does not claim network egress containment. The cluster therefore
receives no sensitive evaluation input. External network behavior is observed
but not safely controlled until a policy-capable network environment is added.

## 10. Reference-product implications

### cert-manager

CRDs, cluster-scoped RBAC, webhooks and safe startup hooks may execute. Any actual
host-level request still stops runtime evaluation.

### Argo CD

Broad RBAC may execute only under the explicit known-product declaration. No Git
repositories, repository credentials or Applications are configured in the
Milestone 1 corpus.

### kube-prometheus-stack

The default rendered chart is always statically evaluated. Components such as
node exporters that request host namespaces or host paths make the default run
`static_only`. A separate documented values file may disable those components
for runtime evaluation. The report must never merge default-configuration static
findings with modified-configuration runtime observations as though they came
from one configuration.

## 11. Validation fixtures

Policy tests include small rendered fixtures for:

- every hard-stop field and capability;
- broad RBAC without direct host access;
- safe and unsafe Helm hooks;
- malformed and oversized render output;
- dynamically created unsafe workloads;
- cleanup blocked by finalizers/webhooks;
- attempted CLI/profile safety override;
- infrastructure failure during admission and cleanup.

Tests assert structured outcome and rule IDs, not only error messages.

## 12. Reversal criteria

The local policy is no longer sufficient when any of these becomes a product
requirement:

- inputs submitted by unknown users;
- deliberate malware/security-product evaluation;
- runtime testing of privileged, node-level, CNI, CSI or host-monitoring products;
- production credentials or sensitive data in the test environment;
- shared or hosted execution;
- reliable enforcement against dynamically created workloads with cluster-admin.

At that point, add a disposable VM or equivalent outer sandbox. Do not make the
kind policy increasingly complicated to approximate a boundary it cannot provide.
