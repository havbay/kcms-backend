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

## Before Ending Any Task

Run the active part's backend and live integration gates, then **update the
canonical state files in the same task**, before reporting back:

- `../kcms-planning/agent-memory/backend-state.md` — endpoints, migrations,
  environment variables, and operational behaviour that surprised you.
- `../kcms-planning/agent-memory/integration-state.md` — when the OpenAPI
  artifact or a cross-repository behaviour changed.
- `../kcms-planning/agent-memory/next-actions.md` — when what comes next changed.

Delete claims the task invalidated. A stale state file is worse than none,
because the next person trusts it.

Check gate results by exit code. Piping a command to `tail` reports the exit code
of `tail`, so a failing suite can look green.

Before believing a security guard works, delete it and confirm a test fails, then
restore it. A passing test is not evidence that a behaviour is protected.
