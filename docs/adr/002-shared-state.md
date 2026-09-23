# ADR-002 — Durable shared-state blackboard

- Status: Proposed
- Date: 2026-09-21

## Context

Agents need common product, plan, experiment and evidence context without
copying large prompts. Coordination changes over time, while observations and
evidence must retain history and provenance.

## Decision

Represent the blackboard as normalized durable records. Mutable coordination is
a versioned projection under optimistic concurrency; experiments, observations,
evidence and audit events are append-only ledgers. `EvaluationState` is a bounded
read view, not a single mutable serialized object. Agents propose typed commands
through a state API and never receive a mutable repository.

Use SQLite/WAL initially with an atomic outbox and content-addressed artifact
store. Use SQLAlchemy Core and Alembic at the persistence milestone.

## Alternatives considered

- A shared in-memory dictionary is simple but neither recoverable nor safe under
  concurrent agent writes.
- One JSON document is easy to serialize but creates write contention, coarse
  conflicts and unbounded context.
- Full event sourcing offers complete replay but adds projection and migration
  complexity beyond current needs.
- PostgreSQL is stronger for concurrency, but requires a service for a local MVP.

## Consequences

State transitions and provenance are auditable, messages survive crashes and
agents query only relevant slices. Mapping and projection code is an accepted
cost. PostgreSQL can replace SQLite through the persistence port when concurrent
writers justify it.

## Risks

Projection drift can disagree with the ledger, and SQLite can bottleneck writes.
Transactional updates, reconciliation tests, bounded writer concurrency and
metrics mitigate these risks.
