# ADR-005 — Deterministic experiment engine

- Status: Proposed
- Date: 2026-09-21

## Context

Agents must investigate Kubernetes behavior, but arbitrary commands would make
permissions, cleanup and reproducibility impossible to guarantee. Experiment
outcomes must not depend on LLM judgement.

## Decision

Only versioned, registered scenarios may execute. The Executor Agent translates
an investigation request into a scenario and typed parameters. The deterministic
engine validates preconditions and authority, materializes an immutable execution
specification, runs bounded phases, collects raw outputs, seals evidence and
performs cleanup. Product observation and infrastructure disposition are
orthogonal outcomes.

CLI tools such as Helm and kind are invoked by fixed executable, typed argv
adapters without a shell. Agents receive neither those adapters nor arbitrary
filesystem/network access.

## Alternatives considered

- Agent shell access maximizes flexibility but defeats safety and reproducibility.
- Raw kubectl/Helm MCP tools are narrower than shell but still expose excessive
  action space and make authorization context-dependent.
- Hard-coded end-to-end scripts are deterministic but difficult to compose,
  inspect and extend consistently.

## Consequences

Experiments are auditable, testable and permission-scoped. Novel investigations
may terminate as unsupported until a scenario is implemented. That limitation is
preferable to an unsafe fallback.

## Risks

Scenario authors can still write unsafe code, and cleanup may fail. First-party
review, scoped contexts, contract/security tests, safety levels and disposable
environments reduce but do not eliminate this risk.
