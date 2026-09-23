# ADR-010 — Trusted versioned scenario plugins

- Status: Proposed
- Date: 2026-09-21

## Context

Experiments must expand across networking, DNS, security, reliability and
upgrades without allowing models to generate arbitrary executable code.
Historical results must identify the exact experiment semantics used.

## Decision

Scenarios are trusted, versioned implementations with typed parameters,
preconditions, environment/tool capabilities, evidence schemas, time/resource
limits, safety level and cleanup contract. Register first-party scenarios
explicitly at startup. Capture semantic version and implementation digest in
every run.

Installed Python entry-point plugins may be added later only through an operator
allowlist. Remote/generated executors, runtime eval and automatic marketplace
loading are forbidden.

## Alternatives considered

- Hard-code all experiments in the engine, which simplifies trust but creates a
  monolith of conditionals and weak lifecycle reuse.
- Dynamic LLM-generated scripts maximize coverage but violate safety and
  reproducibility.
- Containerized arbitrary plugins improve process isolation but still require
  authority, provenance and host/runtime controls; defer until demanded.

## Consequences

New capabilities have an explicit review/test path and historical runs remain
interpretable. Plugin authoring is more structured and cannot instantly cover an
unknown case.

## Risks

Trusted plugin code can still be defective or malicious. Scoped contexts,
package/digest pinning, contract tests and isolation tiers are mandatory; plugin
status does not bypass experiment admission.
