# KubeProof v0.1 Company Profile

| Field | Value |
|---|---|
| Status | **Accepted** |
| Purpose | Express organization constraints supported by Milestone 1 checks |
| Last updated | 2026-09-21 |
| Accepted | 2026-09-21 |

## 1. Boundary

The company profile states what the organization requires or forbids. It does
not describe how KubeProof runs an evaluation.

Examples of company constraints:

- cluster-admin is forbidden;
- memory limits are required;
- Pods must recover within 30 seconds;
- public egress is forbidden.

Examples of execution options that do **not** belong in the profile:

- Helm values files;
- install timeout;
- kind/Kubernetes version;
- output directory;
- recovery target selectors;
- resource collection duration;
- maximum number of destructive checks.

Execution options come from CLI flags and bounded defaults and are recorded in
the evaluation JSON. Keeping the two models separate prevents a test setting
from being mistaken for company policy.

## 2. Proposed YAML

```yaml
schema_version: "0.1"
name: enterprise-strict

constraints:
  security:
    allow_cluster_admin: false
    allow_wildcard_rbac: false
    allow_privileged: false
    allow_host_network: false
    allow_host_pid: false
    allow_host_path: false
    require_run_as_non_root: true
    require_read_only_root_filesystem: true
    allowed_added_capabilities: []

  resources:
    require_requests: true
    require_limits: true
    max_memory_limit_per_pod: 2Gi
    max_cpu_limit_per_pod: "2"
    max_sampled_memory_per_pod: 2Gi
    max_sampled_cpu_per_pod: "2"

  reliability:
    minimum_replicas: 2
    max_pod_replacement_ready_seconds: 30

  networking:
    allow_public_egress: false
    allowed_external_domains: []
```

The quoted CPU values avoid YAML numeric coercion and preserve Kubernetes
quantity syntax. Resource quantities are validated and normalized before use.

## 3. Missing values

Omission means “this profile does not define a constraint,” not an implicit
allow or deny. KubeProof can still show the underlying observation as a built-in
warning or information item, but it cannot create a profile-violation blocker.

For example:

```yaml
constraints:
  resources:
    require_limits: true
```

This makes missing limits a blocker. It says nothing about requests or maximum
observed memory.

There are no hidden strict defaults. A shipped `enterprise-strict.yaml` example
can be opinionated, but its values are visible.

## 4. Constraint semantics

### 4.1 Security

| Field | Deterministic Milestone 1 interpretation |
|---|---|
| `allow_cluster_admin` | A product identity must not be bound to the built-in cluster-admin role when false |
| `allow_wildcard_rbac` | Roles must not contain wildcard verbs, resources or API groups when false |
| `allow_privileged` | No rendered product container may request privileged mode when false |
| `allow_host_network` | Product Pods may not request the host network namespace when false |
| `allow_host_pid` | Product Pods may not request the host PID namespace when false |
| `allow_host_path` | Product Pods may not declare hostPath volumes when false |
| `require_run_as_non_root` | Every product container must be covered by an effective non-root Pod/container security context when true |
| `require_read_only_root_filesystem` | Every product container must explicitly use a read-only root filesystem when true |
| `allowed_added_capabilities` | Any capability added outside this set violates the constraint |

The checks operate on effective Pod/container configuration where Kubernetes
override semantics are deterministic. They do not infer the runtime UID from an
absent field.

### 4.2 Resources

Requests/limits are evaluated per regular and init container, with init-container
semantics reported separately when aggregation differs from steady-state Pods.
Pod limit thresholds use the deterministic effective Pod resource calculation
defined by the check version.

Sampled thresholds compare the sum of available per-container Metrics API
samples for a Pod at the same metric timestamp. Missing containers make that Pod
sample incomplete; an incomplete sample cannot prove compliance.

A sampled value over the threshold can prove an observed violation. Samples
under the threshold prove only that the threshold was not crossed in the
recorded window.

### 4.3 Reliability

`minimum_replicas` applies to supported Deployment workloads. Workload-specific
exceptions are not part of the profile schema in v0.1.

`max_pod_replacement_ready_seconds` compares against the accepted controller
replacement measurement. It does not represent application availability.

### 4.4 Networking

`allow_public_egress: false` records the organization constraint. Milestone 1
does not yet perform a blocked-egress experiment, so an external DNS observation
creates a potential-compatibility warning and an untested question, not a
confirmed profile violation.

`allowed_external_domains` helps classify observed/static external endpoints.
It does not imply that an unobserved domain is unused or that an observed domain
is required.

## 5. Validation and compatibility

- Unknown fields are validation errors rather than silently ignored.
- `schema_version` is mandatory.
- Profile names are identifiers shown in reports, not database keys.
- Quantities must use valid Kubernetes quantity syntax and be non-negative.
- Domain entries are normalized DNS names; URLs and arbitrary regex are not
  accepted in v0.1.
- Contradictory constraints fail before chart acquisition or cluster creation.
- The original profile and normalized canonical representation are stored in the
  bundle.

v0.1 does not support inheritance, imports, composition, templating, embedded
Python or organization-specific rule plugins.

## 6. Execution options

The first CLI may expose:

```text
--values <file>
--set <key=value>
--kubernetes-version <version>
--install-timeout <duration>
--steady-state-window <duration>
--recovery-selector <label-selector>
--max-recovery-targets <number>
--output <directory>
```

All resolved values, including defaults, are persisted in `evaluation.json`.
If the option set becomes difficult to reproduce or share, a separate versioned
evaluation-spec file may be introduced. That is the reversal criterion; it is
not part of the initial profile.

## 7. Safety policy is not profile policy

The user cannot weaken KubeProof's local execution safety by setting
`allow_privileged: true` or `allow_host_path: true`. Such values mean only that
the organization accepts the feature; the independent preflight admission policy
may still stop runtime execution and produce a static-only report.

This distinction is essential:

```text
Company policy: Is this acceptable for adoption?
KubeProof safety: Is this safe enough to execute on this machine?
```

## 8. Reference-product use

- cert-manager exercises CRD, webhook, workload-security and cluster RBAC rules.
- Argo CD exercises multi-component resource/RBAC/recovery reporting.
- kube-prometheus-stack exercises high resource cardinality and host-level
  components. Its default rendered configuration remains part of the static
  evaluation even when local safety policy requires a modified values file for
  runtime checks.
