---
owner: platform-docs
doc_type: adr
status: accepted
last_reviewed: 2026-07-24
review_cycle: event-driven
related_services: []
related_rfcs: []
related_adrs: []
---

# ADR-0001: Example decision (fixture)

A minimal, fully compliant decision record used only by the docs-governance checker
self-test. It exercises the frontmatter, owner-directory, controlled-vocabulary,
related-key, and supersession detectors on a document that should pass every control.

## Status

Accepted

## Context

The `docs-governance-guard.yaml` self-CI workflow needs a compliant repository shape to run
`check-docs-governance.py` against end-to-end, so a catalog/checker change that breaks
execution is caught in this repo rather than fleet-wide.

## Decision

Keep one minimal compliant document here. It carries no `related_*` references, so it needs
no Related Docs section, and uses `event-driven` review so it never goes stale.

## Consequences

The checker has a stable positive fixture to validate against.
