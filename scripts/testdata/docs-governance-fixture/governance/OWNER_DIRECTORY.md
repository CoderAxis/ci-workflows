---
owner: platform-docs
doc_type: governance-standard
status: active
last_reviewed: 2026-07-24
review_cycle: event-driven
related_services: []
related_rfcs: []
related_adrs: []
owner_registry:
  - slug: platform-docs
    scope: Fixture owner used only by the docs-governance checker self-test.
    escalation: platform-docs-oncall
---

# Owner Directory (fixture)

Minimal owner directory for the `check-docs-governance.py` end-to-end self-test. Not a real
governed repository — it exists only so `docs-governance-guard.yaml` can prove the checker
runs clean against a compliant repository shape.

| Slug | Scope |
| ---- | ----- |
| `platform-docs` | Fixture owner used only by the docs-governance checker self-test. |
