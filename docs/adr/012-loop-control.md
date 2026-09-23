# ADR-012 — Deterministic agent loop control

- Status: Proposed
- Date: 2026-09-21

## Context

Peer agents can recursively delegate, repeat experiments and consume unbounded
time/tokens. Preventing loops cannot depend on another supervisor LLM without
reintroducing the original bottleneck.

## Decision

Enforce monotonic budgets for calls, experiments, wall time, tokens, retries,
concurrency and causal depth. Persist causal call/message graphs and task history.
Use canonical task/hypothesis/experiment fingerprints for duplicate detection.
Stop a branch after bounded consecutive attempts without deterministic
information gain: new evidence, changed hypothesis state, changed input revision
or materially different experiment fingerprint.

Duplicates link to existing work unless the request explicitly declares a
repetition purpose. Terminal hypothesis states distinguish confirmed, disproven,
inconclusive, blocked, unsupported and manual investigation.

## Alternatives considered

- A supervisor LLM can semantically judge progress but adds cost and is itself
  capable of looping.
- Recursion limits alone miss repeated work across asynchronous branches.
- Timeouts alone stop late and provide poor explanations.
- Exact string deduplication is cheap but misses normalized typed equivalence.

## Consequences

Termination is explainable and testable. Some valuable repeated tests require an
explicit purpose instead of happening accidentally. Semantic duplicates not
captured by typed fingerprints may remain.

## Risks

Over-aggressive convergence rules can end investigations early; loose rules waste
budget. Record every stop reason, evaluate against labelled loops and allow
profile-specific limits without allowing agents to increase their own budgets.
