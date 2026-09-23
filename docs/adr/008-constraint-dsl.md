# ADR-008 — Explicit Python constraint DSL

- Status: Proposed
- Date: 2026-09-21

## Context

Company policies span typed Kubernetes, security, network, DNS, resource,
availability, scheduling, storage and operational rules. Users need composition,
validation, IDE support and portable snapshots without YAML-first semantics.

## Decision

Use Pydantic-backed typed Python constructors with explicit `compose()` and
`require()` APIs. Reusable profiles are fragments, not an inheritance hierarchy.
Composition fails on conflicts unless an override identifies the constraint and
reason. Canonical JSON is the persisted/exchange format; YAML is optional safe
import/export.

Atomic constraints declare applicability, requirement level, source and
available deterministic evaluators. Constraint evaluation is separate from
enforcement.

## Alternatives considered

- YAML/JSON-first policy is portable but less discoverable and type-safe for the
  primary local engineering workflow.
- Operator `+` composition is concise but hides precedence/conflict behavior.
- Class inheritance reuses defaults but makes multiple-profile precedence
  implicit and fragile.
- A general policy language such as Rego is powerful but raises the authoring bar
  and does not replace domain types.

## Consequences

Local profiles are readable, composable and introspectable. Python profile code
is trusted local code and cannot be accepted as an arbitrary hosted upload;
remote operation will consume canonical data or a restricted declarative layer.

## Risks

The DSL can grow into a second programming language, and constraint keys can
become inconsistent. Add categories and combinators only for real scenarios,
keep composition explicit and property-test conflict/serialization semantics.
