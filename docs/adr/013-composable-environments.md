# ADR-013 — Composable environment blueprints and controlled variants

- Status: Proposed
- Date: 2026-09-21

## Context

Company compatibility depends on more than Kubernetes version. A real platform
includes CNI, DNS, storage, service mesh, policy, ingress and observability
configuration. The same product may install successfully on a minimal cluster
and fail—or incur different latency, resource or operational cost—under the
company baseline.

Hard-coding a few named CNI combinations is too narrow. Allowing arbitrary
unstructured add-ons is unsafe and produces environments that cannot be
reproduced or compared.

## Decision

Introduce a typed, versioned `EnvironmentBlueprint` distinct from
`CompanyProfile`. A blueprint composes the cluster specification with registered
environment components. Components declare pinned artifacts, configuration,
dependencies/conflicts, lifecycle order, required and provided capabilities,
health/conformance probes, collectors, cleanup and baseline resource attribution.

Persist an immutable `EnvironmentSnapshot` after provisioning. Evidence refers
to the snapshot, not only the requested blueprint. A failed capability probe
removes that capability and can block dependent scenarios.

Represent a controlled change as an `EnvironmentVariant`. Run steady-state
comparisons as an `EvaluationCampaign` with a fresh cluster and factual ledger
per variant. Share immutable product/profile/scenario inputs by digest. Use a
separate transition experiment when the question is the effect of changing a
running environment.

Environment components are trusted operator-selected infrastructure. If a
component is itself under evaluation, it becomes the product and cannot provide
its own containment.

## Alternatives considered

- Encode the environment entirely in `CompanyProfile`. This conflates desired
  policy with provisioned reality and cannot represent failed installation or
  capability verification.
- Hard-code Cilium/Calico environment providers. This addresses CNI selection but
  not service mesh, DNS, policy and observability composition.
- Accept arbitrary Helm add-ons. This is flexible but has no typed capabilities,
  dependency validation, trust boundary or reproducible lifecycle.
- Mutate one cluster through all variants. This measures migration but leaves
  state that invalidates steady-state causal comparison.
- Build every Cartesian product automatically. This is comprehensive in theory
  but operationally expensive and statistically difficult to interpret.

## Consequences

The system can model “our company runs Cilium with this configuration and Istio
ambient” and evaluate a product in that environment. It can also compare a
baseline with a ztunnel-enabled variant while attributing evidence to exact
component versions and configuration.

The domain gains component lifecycle, capability negotiation, dependency graph,
baseline measurements and campaign comparison. Implement this incrementally:
one minimal blueprint first, then one component and one controlled variant.

## Risks

An overly generic component model could become a package manager or Terraform
replacement. Limit it to evaluation environments, registered adapters and
capabilities required by scenarios. Do not promise arbitrary platform
provisioning.

Cross-component compatibility rules change over time. Pin artifacts, keep rules
versioned and verify capabilities at runtime rather than trusting a static
compatibility table. Variant matrices can still explode; default to one-factor-
at-a-time changes driven by a hypothesis.
