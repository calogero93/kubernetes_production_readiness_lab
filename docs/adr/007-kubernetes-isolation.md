# ADR-007 — kind first and explicit isolation tiers

- Status: Accepted
- Date: 2026-09-21
- Accepted: 2026-09-21 after architecture review

## Context

The MVP needs reproducible ephemeral Kubernetes clusters. Evaluated charts and
images may request host access, broad RBAC, webhooks or malicious behavior.
Docker-based local clusters do not provide a strong hostile-code sandbox.

## Decision

Use kind as the first `EnvironmentProvider` because upstream Kubernetes fidelity
is a better neutral baseline than K3s for product compatibility. Define explicit
isolation tiers: static-only, local known-input lab, disposable VM sandbox and
external managed cluster. The MVP supports the first two and must reject or
escalate high-risk inputs rather than claim strong local isolation.

Unknown/adversarial workloads require a future dedicated disposable VM (or
equivalent host sandbox) containing the container runtime and kind. Preflight
rendering classifies hooks, privileges, host access, RBAC, webhooks and resource
risk before mutation.

## Alternatives considered

- k3d is lighter and convenient but introduces K3s-specific packaging and
  behavior.
- minikube supports multiple drivers but adds a broader configuration matrix.
- Existing clusters are realistic but unsafe as the default and difficult to
  reproduce.
- Namespace-only isolation is insufficient for cluster-scoped resources and
  hostile containers.

## Consequences

Local evaluations are easy to reproduce, and environment providers can later add
K3s/cloud targets. Reports must disclose tier and residual risk. VM sandboxing is
an early design requirement even though it is outside the first slice.

## Risks

Users may ignore warnings and run hostile code locally. kind requires powerful
container-runtime access and shares a kernel. Hard policy defaults, explicit
acknowledgement, no host secrets and documented threat boundaries are required.

## Review note

The accepted decision covers the trust-tier direction and use of kind as the
first environment. Exact preflight admission rules remain versioned security
policy and are proposed in `docs/threat-model.md`; changing those rules does not
silently change this architectural boundary.
