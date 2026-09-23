# KubeProof v0.1 Truth Contract

| Field | Value |
|---|---|
| Status | **Accepted** |
| Accepted | 2026-09-21 |
| Scope | Milestone 1 output semantics |

## 1. Global rules

- A finding must reference one or more observations from the evaluation.
- A report may render observations and findings but cannot add facts.
- A passing result is scoped to the named check, environment and observation
  window; it is not a universal production-readiness claim.
- `not_tested` and `inconclusive` are visible and never rendered as pass.
- Documentation, static input and runtime behavior are distinct source classes.
- Infrastructure failure cannot create a negative product finding.
- KubeProof produces no aggregate production-readiness score.

## 2. Outcome axes

Every check has an execution status:

```text
completed | infrastructure_failure | cancelled
```

and an independent assessment:

```text
pass | fail | warning | inconclusive | not_tested | not_applicable
```

A healthy experiment reaching its readiness deadline can be `completed + fail`.
An unreachable Kubernetes API is `infrastructure_failure + not_tested`.

## 3. Installation and readiness

KubeProof may report Helm exit data, created resources, supported workload
readiness, duration, conditions, restarts, Events and hook outcomes.

The check passes only when Helm completes and every required supported workload
becomes Ready within the configured timeout. A successful Helm process alone is
not product readiness.

If the API remains healthy but product workloads do not become Ready, the check
may fail. Root cause remains unknown unless separate evidence supports a more
specific attribution.

## 4. Security and RBAC

KubeProof may report exact RBAC bindings, wildcard/sensitive permissions,
privileged mode, host namespaces/paths, capabilities and security-context fields.

- Explicit `runAsUser: 0` supports “configured to run as UID 0.”
- Missing `runAsNonRoot` supports “non-root execution is not enforced.”
- An absent field does not support “the container runs as root.”

Runtime UID requires separate runtime or sufficiently resolved image/spec
evidence and is not assumed in Milestone 1.

## 5. Resources

KubeProof reports declared requests/limits, pod/container counts, DaemonSet
footprint and CPU/memory samples returned by the Kubernetes Metrics API.

Runtime measurements are labelled with source API, timestamp, metric window,
collection window and sampling interval. Aggregates use wording such as:

> Maximum observed memory sample during the five-minute collection window.

They are never called a guaranteed startup peak. No observed threshold breach
means only that no collected sample exceeded the threshold.

### 5.1 Accepted Metrics API decision

- Install a pinned Metrics Server before the product.
- Wait for its APIService and initial samples before beginning measurement.
- Configure a 15-second metric resolution explicitly.
- Poll no faster than the underlying useful resolution.
- Deduplicate repeated results using resource identity plus metric timestamp.
- Record Pod UID and container name so recreated Pods are not conflated.
- Collect during installation/readiness and a defined post-readiness window.
- Keep Metrics Server resource usage in the environment baseline.
- If the Metrics API is unavailable, static resource checks continue and runtime
  resource assessment becomes `infrastructure_failure + not_tested`.

Metrics Server supplies basic CPU and memory data for Kubernetes autoscaling. It
is not a monitoring backend and is not sufficient for sub-second/transient peak
analysis. The reversal criterion is a product requirement for higher-resolution
startup peaks, historical queries or richer metrics; that would justify a direct
Kubelet resource-metrics collector or a dedicated monitoring pipeline.

## 6. Pod termination and recovery

Milestone 1 measures controller replacement readiness, not application-level
availability.

Initial scope:

- Deployment-managed Pods only;
- healthy Ready baseline required;
- one Pod deleted per selected Deployment;
- sequential execution;
- maximum five targets by default;
- profile selection/exclusion supported;
- StatefulSet, Job and DaemonSet recovery excluded.

Valid wording:

> Replacement Pod became Ready 8.3 seconds after deletion.

Invalid without a functional probe:

> The application recovered in 8.3 seconds.

## 7. Network and DNS

Milestone 1 may report endpoints present in input, external DNS queries observed
during the run, DNS errors and relevant connection errors in collected logs or
Events.

An observed query supports:

> Pod X queried api.vendor.com during readiness.

It does not support:

> The product requires public access to api.vendor.com.

Dependency confirmation requires a later baseline → block → observe → restore →
recover experiment. Under a no-public-egress profile, an observed external query
is therefore a potential compatibility warning, not a confirmed blocker.

## 8. Adoption blockers

A finding is an adoption blocker only when:

1. the profile declares a required or forbidden constraint;
2. the relevant check completed;
3. supporting observations exist;
4. a deterministic rule proves the violation;
5. infrastructure uncertainty does not invalidate the conclusion.

Examples include a forbidden cluster-admin binding, required memory limit being
absent, or measured replacement readiness exceeding the configured threshold.
Untested behavior and potential external dependency remain explicit gaps rather
than blockers.
