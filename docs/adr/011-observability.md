# ADR-011 — Structured decision observability without chain-of-thought

- Status: Proposed
- Date: 2026-09-21

## Context

Agentic investigations require auditability across messages, model calls, tools,
experiments, evidence and findings. Private reasoning traces are neither required
nor appropriate to store, and raw prompts/outputs may contain secrets or
untrusted content.

## Decision

Emit correlated structured events with evaluation, task, invocation,
correlation/causation, scenario, experiment and evidence IDs. Persist concise
decision summaries, selected capability/task/experiment, evidence references,
validation results, latency, retries, token usage and error classes. Do not store
private chain-of-thought.

Use structlog JSON output first. Carry trace IDs from the start and adopt
OpenTelemetry when multi-process execution or a collector-backed integration
exists. Apply redaction and bounded payload policies before persistence.

## Alternatives considered

- Plain text logs are easy but hard to correlate and evaluate.
- Full prompt/response capture improves debugging but increases privacy,
  injection replay and secret-retention risk.
- A vendor observability SDK accelerates dashboards but creates coupling before
  operational requirements stabilize.

## Consequences

Operators can reconstruct decisions without hidden reasoning, and evals can
measure cost, latency and failure. Some model debugging will require controlled,
opt-in captures outside ordinary production retention.

## Risks

High-cardinality IDs and artifact references can increase volume; redaction can
remove needed diagnostic context. Define retention tiers, payload limits and
protected debug workflows.
