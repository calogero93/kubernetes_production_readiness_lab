# Architecture Decision Records

> These records belong to the earlier full-platform exploration. Their status
> does not make them an implementation commitment for KubeProof v0.1. Current
> decisions and open reversal criteria are tracked in
> [`docs/v0.1/decisions.md`](../v0.1/decisions.md).

The records in this directory are decision proposals for Milestone 0. They are
not implementation instructions until their status becomes `Accepted`.

Status lifecycle:

```text
Proposed → Accepted → Superseded
                  ↘ Deprecated
```

A rejected proposal remains in history with status `Rejected` and a short
reason. Material changes create a new ADR that supersedes the old one; accepted
history is not silently rewritten.

| ADR | Title | Status |
|---|---|---|
| [ADR-001](001-language-runtime.md) | Python modular monolith | Proposed |
| [ADR-002](002-shared-state.md) | Durable shared-state blackboard | Proposed |
| [ADR-003](003-no-central-supervisor.md) | No strategic central supervisor | Proposed |
| [ADR-004](004-evidence-findings.md) | Evidence-gated findings | Proposed |
| [ADR-005](005-experiment-engine.md) | Deterministic experiment engine | Proposed |
| [ADR-006](006-mcp-tools.md) | Selective MCP tool architecture | Proposed |
| [ADR-007](007-kubernetes-isolation.md) | kind and explicit isolation tiers | Accepted |
| [ADR-008](008-constraint-dsl.md) | Explicit Python constraint DSL | Proposed |
| [ADR-009](009-rag.md) | Version-aware knowledge architecture | Proposed |
| [ADR-010](010-scenario-plugins.md) | Trusted scenario plugins | Proposed |
| [ADR-011](011-observability.md) | Structured decision observability | Proposed |
| [ADR-012](012-loop-control.md) | Deterministic agent loop control | Proposed |
| [ADR-013](013-composable-environments.md) | Composable environment blueprints and controlled variants | Proposed |
