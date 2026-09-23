# ADR-004 — Evidence-gated findings and claim separation

- Status: Proposed
- Date: 2026-09-21

## Context

The product's credibility depends on preventing documentation, model output or
infrastructure failures from being presented as observed product behavior.
Evidence may later be challenged without erasing history.

## Decision

Keep raw observations, sealed evidence, documentation claims, hypotheses and
findings as separate domain concepts. Evidence is immutable; reliability changes
are new assessment records. Accepted evidence-based findings pass a deterministic
gate that validates evidence existence, relevance, trust class and outcome
classification. Report generation cannot create new findings or evidence.

Confidence is a documented categorical derivation from directness, reliability,
repeatability, independence and representativeness, not an arbitrary LLM score.

## Alternatives considered

- Storing citations inside narrative text is simple but cannot enforce referential
  or semantic validity.
- Treating documentation as low-confidence evidence blurs the central behavioral
  distinction.
- Letting a report model generate findings produces readable reports but no
  auditable source of truth.

## Consequences

Reports can expose exactly why a conclusion exists and can revalidate findings
when evidence is invalidated. More domain records and validation rules are
required. Documentation-only, not-tested and manual-review items remain visible
without pretending to be runtime findings.

## Risks

A structural gate cannot prove that an interpretation is scientifically correct.
Scenario quality, human review, repeated evidence and agent evals remain
necessary. Poor evidence schemas could give a false impression of rigor.
