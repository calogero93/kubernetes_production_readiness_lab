# ADR-001 — Python modular monolith and custom runtime

- Status: Proposed
- Date: 2026-09-21

## Context

The product needs a typed Python DSL, Kubernetes and AI integrations, a CLI, a
shared state runtime and deterministic experiment execution. The MVP is local
and single-user; independent service scaling is not yet a requirement.

## Decision

Use Python 3.12+ and a modular monolith organized with domain, application ports
and infrastructure adapters. Build the minimum custom command/dispatch runtime
needed by the shared-state collaboration model. Keep provider-specific LLM SDKs
behind an internal protocol.

## Alternatives considered

- Go would improve static guarantees and deployment, but weaken the Python DSL
  and increase friction around AI integrations.
- TypeScript has strong schema/tooling support, but is a worse fit for the stated
  Python-first AI and data-engineering goals.
- LangGraph, LangChain or an Agents SDK could accelerate orchestration, but their
  control abstractions do not replace the evidence ledger, permission guard or
  typed blackboard.
- Microservices would isolate components but introduce networking, deployment
  and distributed-consistency work before there is a scaling need.

## Consequences

The first release is easy to run and inspect. Domain boundaries must be enforced
by imports and tests rather than deployment. The custom runtime remains small and
product-specific; a checkpoint after Milestone 5 decides whether to keep it.

## Risks

The runtime could grow into an undifferentiated framework, and Python typing can
be bypassed at runtime. Limit the runtime to admitted commands, leases, budgets,
outbox and dispatch; use strict validation and static checks. Reconsider a
durable workflow engine if crash recovery or distributed execution dominates
product work.
