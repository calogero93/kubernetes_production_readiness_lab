# ADR-006 — Selective MCP tool architecture

- Status: Proposed
- Date: 2026-09-21

## Context

MCP can standardize external tool and knowledge integrations, but adding a
protocol boundary to internal calls creates latency, serialization and security
surface without necessarily improving the design.

## Decision

Do not use MCP in the first vertical slice. Stabilize internal typed ports first,
then introduce MCP only where a tool is genuinely out of process, independently
owned or useful to other clients. Expose narrow operations, never shell or a
general kubectl proxy. Mutation-capable MCP tools are called only by the
Experiment Engine under runtime policy.

Treat server annotations and outputs as untrusted hints/data. Enforce a local
server allowlist, scopes, timeouts, size limits, protocol pinning and audit IDs.

## Alternatives considered

- MCP everywhere provides uniformity but turns a modular monolith into a network
  of RPC boundaries prematurely.
- No MCP keeps the system smaller but reduces interoperability for external
  scanners, knowledge services and future clients.
- Custom RPC offers full control but recreates a protocol ecosystem.

## Consequences

The core remains understandable and testable without a server. Selected adapters
can later move across the process boundary without changing domain contracts.

## Risks

An MCP server can lie about tool annotations, return injectable content or hold
over-broad credentials. Protocol metadata is never an enforcement boundary;
sandboxing and runtime policy remain mandatory.
