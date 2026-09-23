# ADR-003 — No strategic central supervisor

- Status: Proposed
- Date: 2026-09-21

## Context

A supervisor LLM on every transition would add latency, cost, context loss and a
reasoning bottleneck. Agents still need consistent routing, permissions, budgets
and recovery.

## Decision

Agents decide goals, delegation and follow-up investigations through typed
peer-to-peer messages. A deterministic dispatcher routes addressed or
capability-targeted messages and a runtime guard performs admission. Neither asks
an LLM what should happen next. The Planner is invoked at start or for explicit
full-replan reasons, not in ordinary investigation loops.

## Alternatives considered

- A supervisor agent simplifies conceptual control flow but centralizes context
  and creates an expensive single point of reasoning failure.
- Direct object-to-object agent calls appear peer-to-peer but bypass durable
  messaging, policy and audit boundaries.
- A static workflow DAG is predictable but cannot naturally express a living
  investigation plan.

## Consequences

Analysis can ask Executor or domain peers directly, and local discoveries do not
require Planner mediation. The system still has a deterministic control plane;
“no supervisor” does not mean no scheduler or authority boundary.

## Risks

Peer delegation can cycle, duplicate work or race on state. ADR-012 budgets and
cycle controls, ADR-002 transactions and explicit capability routing are required
parts of this decision.
