# KCMS V2 Backend Agent Instructions

Before any task, read the canonical sibling planning repository:

1. `../kcms-planning/00-product-specification.md`
2. `../kcms-planning/04-implementation-roadmap.md`
3. `../kcms-planning/agent-memory/current-state.md`
4. `../kcms-planning/02-backend-redesign-plan.md`
5. `../kcms-planning/03-api-contract.md`
6. Applicable accepted records under `../kcms-planning/adr/`

Work only on the active part. Enforce authorization and product invariants in the
backend regardless of frontend behavior. Update the backend-owned OpenAPI document
before the frontend depends on a contract change. Keep scripted local adapters
separate from production integrations.

Before ending, run the active part's backend and live integration gates and update
the canonical backend and integration state files.
